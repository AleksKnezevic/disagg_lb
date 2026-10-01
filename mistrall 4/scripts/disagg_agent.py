"""Small stdlib-only remote process agent. Its source is sent over SSH stdin RPC."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from urllib.parse import urlparse


def proc(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        return {"start": fields[19], "ppid": int(fields[1]), "pgid": int(fields[2])}
    except (OSError, ValueError):
        return None


def boot():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def identity(pid):
    info = proc(pid)
    return {"pid": pid, "start": info["start"], "boot": boot()} if info else None


def alive(record):
    return bool(
        record
        and identity(record["pid"]) == {k: record[k] for k in ("pid", "start", "boot")}
    )


def group_alive(record):
    if record["boot"] != boot():
        return False
    return any(
        (info := proc(int(p.name))) and info["pgid"] == record["pid"]
        for p in Path("/proc").glob("[0-9]*")
    )


def fingerprint(path):
    p = Path(path)
    st = p.stat()
    return dict(
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
    )


def owners():
    result = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            argv = (p / "cmdline").read_bytes().decode(errors="replace").split("\0")
            if "-c" in argv[:3]:
                continue
            if any(
                x in argv
                for x in (
                    "tools.mistral4_reload",
                    "models.demos.common.prefill.runners.prefill_runner",
                    "tt_dynamo.main",
                )
            ) or any(
                Path(x).name
                in ("build_prefill_cache_loudbox_2x4.py", "test_embedding_load.py")
                for x in argv
            ):
                result.append({"pid": int(p.name), "argv": argv[:8]})
        except OSError:
            pass
    return result


def container(spec):
    if not spec.get("container_name"):
        return None
    r = subprocess.run(
        spec["docker_argv"] + ["inspect", spec["container_name"]],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode:
        if (
            "no such object" in r.stderr.lower()
            or "no such container" in r.stderr.lower()
        ):
            return None
        raise RuntimeError("cannot inspect Docker ownership: " + r.stderr[-1000:])
    x = json.loads(r.stdout)[0]
    return {
        "id": x["Id"],
        "owner": (x["Config"].get("Labels") or {}).get("disagg.owner"),
        "running": x["State"]["Running"],
    }


def read_state(spec):
    p = Path(spec["state"])
    return json.loads(p.read_text()) if p.exists() else None


def status(spec):
    record = read_state(spec)
    if not record:
        return {"state": "unmanaged", "record": None}
    if record.get("hostname") != socket.gethostname():
        return {"state": "wrong_namespace", "record": record}
    running = alive(record)
    stale = []
    for path, value in record.get("inputs", {}).items():
        try:
            if fingerprint(path) != value:
                stale.append(path)
        except OSError:
            stale.append(path)
    c = container(record["spec"])
    orphan = not running and c and c["running"]
    return {
        "state": (
            "orphaned_container"
            if orphan
            else (
                "running"
                if running
                else "orphaned_process_group" if group_alive(record) else "stopped"
            )
        ),
        "record": record,
        "stale_inputs": stale,
        "container": c,
    }


def ready(spec, record):
    rule = spec.get("ready", {})
    for path in rule.get("files", []):
        p = Path(path)
        if not p.is_file() or not p.stat().st_size:
            return False
        if p.stat().st_mtime < record["launched_at"] - 2:
            return False
    if rule.get("log"):
        p = Path(rule.get("log_path", spec["log"]))
        if not p.exists():
            return False
        with p.open("rb") as f:
            f.seek(max(0, p.stat().st_size - 2 * 1024 * 1024))
            tail = f.read().decode(errors="replace")
        if rule["log"] not in tail:
            return False
    if rule.get("tcp"):
        with socket.create_connection(tuple(rule["tcp"]), timeout=2):
            pass
    url = rule.get("http") or rule.get("models")
    if url:
        with urllib.request.urlopen(url, timeout=3) as f:
            data = json.load(f)
        if rule.get("models") and not any(
            m["id"] == rule["model"] for m in data.get("data", [])
        ):
            return False
        if rule.get("http") and not (
            data.get("status") == "healthy" or data.get("health") in (True, "true")
        ):
            return False
    return True


def start(spec, spec_hash, dependency_tokens):
    current = status(spec)
    if current["state"] == "running":
        if current["record"]["spec_hash"] != spec_hash:
            raise RuntimeError("running service has different config; stop it first")
        if current["stale_inputs"]:
            raise RuntimeError(
                "running service has stale KV inputs; drain and restart stack"
            )
        return current
    if current["state"] not in ("unmanaged", "stopped"):
        raise RuntimeError(current["state"])
    if container(spec):
        raise RuntimeError(
            "container name already exists; refusing adoption or replacement"
        )
    if spec.get("guard_models") and owners():
        raise RuntimeError(
            "existing model/worker processes own this namespace; refusing duplicate runner"
        )
    # A responding foreign service is not evidence that our launch succeeded.
    rule = spec.get("ready", {})
    listeners = list(spec.get("listen", []))
    if rule.get("tcp"):
        listeners.append(rule["tcp"])
    if rule.get("http") or rule.get("models"):
        address = urlparse(rule.get("http") or rule["models"])
        listeners.append([address.hostname, address.port])
    for host, port in listeners:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError as e:
                raise RuntimeError(f"cannot reserve {host}:{port}: {e}") from e
    inputs = {
        p: fingerprint(p)
        for p in spec.get("inputs", []) + spec.get("config_inputs", [])
    }
    for path in spec.get("mkdir", []):
        Path(path).mkdir(parents=True, exist_ok=True)
    log = Path(spec["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    for path in {str(log), spec.get("ready", {}).get("log_path", str(log))}:
        p = Path(path)
        if p.exists():
            p.rename(str(p) + "." + str(time.time_ns()))
    owner = uuid.uuid4().hex
    argv = [v.replace("{owner}", owner) for v in spec["argv"]]
    env = os.environ.copy()
    for key in spec.get("unset_env", []):
        env.pop(key, None)
    env.update(spec.get("env", {}))
    launched = time.time()
    with log.open("ab") as output:
        child = subprocess.Popen(
            argv,
            cwd=spec["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = identity(child.pid)
    if not record:
        raise RuntimeError(f"service exited immediately; inspect {log}")
    record.update(
        hostname=socket.gethostname(),
        owner=owner,
        spec=spec,
        spec_hash=spec_hash,
        launched_at=launched,
        inputs=inputs,
        dependency_tokens=dependency_tokens,
    )
    state = Path(spec["state"])
    temp = state.with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=2))
    temp.chmod(0o600)
    temp.replace(state)
    return {"state": "started", "record": record}


def stop(spec, timeout):
    current = status(spec)
    record = current["record"]
    if current["state"] in ("unmanaged", "stopped"):
        return current
    if current["state"] != "running":
        raise RuntimeError(f"refusing stop: {current['state']}")
    old = record["spec"]
    c = current.get("container")
    if c and c["owner"] != record["owner"]:
        raise RuntimeError("container ownership changed; refusing stop")
    info = proc(record["pid"])
    if not info or info["pgid"] != record["pid"]:
        raise RuntimeError("process group changed; refusing signal")
    if c:
        result = subprocess.run(
            old["docker_argv"] + ["kill", "--signal", "SIGTERM", c["id"]],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError("owned container stop failed: " + result.stderr[-1000:])
    elif old.get("stop_file") and ready(old, record):
        Path(old["stop_file"]).touch()
    else:
        os.killpg(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while group_alive(record) and time.monotonic() < deadline:
        time.sleep(0.2)
    if group_alive(record):
        raise RuntimeError(
            "graceful stop timed out; lower dependencies were left running"
        )
    c = container(old)
    if c and c["running"]:
        raise RuntimeError(
            "container still running; lower dependencies were left running"
        )
    if old.get("guard_models") and owners():
        raise RuntimeError(
            "runner consumers remain after launcher exit; lower dependencies were left running"
        )
    return status(spec)


def probe(request):
    result = {
        "hostname": socket.gethostname(),
        "devices": sorted(
            str(p)
            for p in Path("/dev/tenstorrent").glob("*")
            if p.name.isdigit() and p.is_char_device()
        ),
        "owners": owners(),
        "files": {},
        "pins": {},
        "tcp": {},
        "commands": {},
    }
    for path in request.get("files", []):
        p = Path(path)
        try:
            result["files"][path] = (
                fingerprint(path)
                if p.is_file() and p.stat().st_size < 8 * 1024 * 1024
                else {"exists": p.exists(), "size": p.stat().st_size}
            )
        except OSError:
            result["files"][path] = None
    for name, path in request.get("pins", {}).items():
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        result["pins"][name] = r.stdout.strip() if r.returncode == 0 else None
    for host, port in request.get("tcp", []):
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
            result["tcp"][f"{host}:{port}"] = True
        except OSError:
            result["tcp"][f"{host}:{port}"] = False
    for name, argv in request.get("commands", {}).items():
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=25)
            result["commands"][name] = {
                "code": r.returncode,
                "stdout": r.stdout[:20000],
                "stderr": r.stderr[:2000],
            }
        except (OSError, subprocess.TimeoutExpired) as e:
            result["commands"][name] = {"code": 1, "stdout": "", "stderr": str(e)}
    if request.get("checkpoint"):
        root = Path(request["checkpoint"])
        bad = []
        try:
            index = json.loads((root / "model.safetensors.index.json").read_text())
            for name in set(index["weight_map"].values()):
                file = root / name
                if not file.is_file() or file.stat().st_size <= 8:
                    bad.append(name)
                    continue
                with file.open("rb") as f:
                    length = int.from_bytes(f.read(8), "little")
                    if (
                        not 0 < length < 100 * 1024 * 1024
                        or length + 8 >= file.stat().st_size
                    ):
                        bad.append(name)
                        continue
                    header = json.loads(f.read(length))
                    for tensor in header.values():
                        if (
                            isinstance(tensor, dict)
                            and "data_offsets" in tensor
                            and max(tensor["data_offsets"]) + 8 + length
                            > file.stat().st_size
                        ):
                            bad.append(name)
                            break
            result["checkpoint"] = {
                "bad_shards": bad,
                "shards": len(set(index["weight_map"].values())),
            }
        except (OSError, ValueError, KeyError) as e:
            result["checkpoint"] = {"error": str(e)}
    return result


def main():
    request = json.load(sys.stdin)
    action = request["action"]
    try:
        if (
            request.get("expected_hostname", socket.gethostname())
            != socket.gethostname()
        ):
            raise RuntimeError(
                "SSH/container hostname differs from configured reservation"
            )
        if action == "probe":
            result = probe(request)
        else:
            spec = request["spec"]
            if action in ("status", "ready"):
                result = status(spec)
                if action == "ready":
                    try:
                        result["ready"] = result["state"] == "running" and ready(
                            spec, result["record"]
                        )
                    except (OSError, ValueError):
                        result["ready"] = False
            else:
                path = Path(spec["state"])
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.with_suffix(".lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if action == "start":
                        result = start(
                            spec,
                            request["spec_hash"],
                            request.get("dependency_tokens", {}),
                        )
                    elif action == "stop":
                        result = stop(spec, request["timeout"])
                    else:
                        raise ValueError("unknown action")
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
