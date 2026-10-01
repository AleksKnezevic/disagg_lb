"""Deployment loading and deterministic launch-plan generation (no service I/O)."""

import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
PORTS = dict(
    api=8000,
    etcd=2379,
    etcd_peer=2380,
    nats=4222,
    kv_ingress=9093,
    kv_control=19071,
    response=19101,
    kvm_control=18650,
    kvm_health=18081,
    worker_health=18082,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_site(path):
    s = json.loads(Path(path).read_text())
    for key, value in s.get("paths", {}).items():
        s["paths"][key] = os.path.expanduser(os.path.expandvars(value))
        if "$" in s["paths"][key]:
            raise ValueError(f"paths.{key} contains an unset environment variable")
    if s.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    for key in ("name", "namespace"):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", s[key]):
            raise ValueError(f"invalid {key}")
    for key in (
        "repo",
        "dgen",
        "prefill_metal",
        "checkpoint",
        "runtime",
        "prefill_cache",
        "decode_cache",
        "prefill_jit",
        "decode_jit",
    ):
        value = s["paths"][key]
        if not Path(value).is_absolute() or any(c in value for c in "\n\r"):
            raise ValueError(f"paths.{key} must be an absolute single-line path")
    if "," in s["paths"]["runtime"]:
        raise ValueError("runtime path cannot contain commas (Docker mount syntax)")
    for key in ("decode_blaze", "mpi_lib"):
        value = s["paths"].get(key, "")
        if value and (not Path(value).is_absolute() or any(c in value for c in "\n\r")):
            raise ValueError(f"paths.{key} must be an absolute single-line path")
    for role in ("prefill", "decode"):
        n = s["nodes"][role]
        ip = ipaddress.IPv4Address(n["ip"])
        if ip.is_unspecified or ip.is_loopback or ip.is_multicast:
            raise ValueError("node IPs must be reachable unicast host addresses")
        r = n["runner"]
        if r["mode"] not in ("native", "ird"):
            raise ValueError("runner.mode must be native or ird")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", r["hostname"]):
            raise ValueError("invalid runner hostname")
        if r["mode"] == "ird":
            for key in ("container", "user", "ip"):
                if not r.get(key):
                    raise ValueError(f"{role} IRD needs {key}")
            ipaddress.IPv4Address(r["ip"])
        elif r["hostname"] != n["hostname"]:
            raise ValueError("native runner hostname must equal bare-metal hostname")
        if len(r["grid"]) != 2 or any(type(v) is not int or v <= 0 for v in r["grid"]):
            raise ValueError(
                "grid must be [columns, rows]; it is declared evidence, not a hardware probe"
            )
        if role == "decode" and (r["grid"][0] < 13 or r["grid"][1] < 10):
            raise ValueError("pinned LoudBox decode needs at least 13x10 workers")
        ssh = n["ssh"]
        if not ssh["host"] or not 1 <= ssh["port"] <= 65535:
            raise ValueError("invalid SSH target")
        if not all(isinstance(v, str) for v in ssh.get("options", [])):
            raise ValueError(
                "ssh.options must be an argv list; never store passwords here"
            )
        for key in ("host", "user"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", ssh[key]) or ssh[key].startswith(
                "-"
            ):
                raise ValueError("invalid SSH host/user")
    if s["nodes"]["prefill"]["ip"] == s["nodes"]["decode"]["ip"]:
        raise ValueError("the two LoudBox hosts must have distinct addresses")
    if (
        s["nodes"]["prefill"]["runner"]["hostname"]
        == s["nodes"]["decode"]["runner"]["hostname"]
    ):
        raise ValueError("runner hostnames must have distinct table identities")
    s["ports"] = PORTS | s.get("ports", {})
    if set(s["ports"]) != set(PORTS) or any(
        type(v) is not int or not 1024 <= v <= 65535 for v in s["ports"].values()
    ):
        raise ValueError("ports must be named recipe ports in 1024..65535")
    if len(set(s["ports"].values())) != len(s["ports"]):
        raise ValueError("use distinct port numbers for the recipe services")
    s["timeouts"] = dict(
        runner_start=10800, service_start=180, stop=90, request=180
    ) | s.get("timeouts", {})
    if any(not isinstance(v, (int, float)) or v <= 0 for v in s["timeouts"].values()):
        raise ValueError("timeouts must be positive seconds")
    if not s.get("docker_argv") or not all(
        isinstance(v, str) for v in s["docker_argv"]
    ):
        raise ValueError("docker_argv must be a nonempty argv list")
    for key in ("dgen", "blaze", "decode_metal", "prefill_metal"):
        if not re.fullmatch("[a-f0-9]{40}", s["pins"][key]):
            raise ValueError(f"pins.{key} must be a full commit")
    if not s["model_name"] or not all(s["images"].get(k) for k in ("kvm", "nats")):
        raise ValueError("model_name and both images are required")
    return s


def locations(s):
    p = s["paths"]
    run = Path(p["runtime"])
    return run, run / "generated", run / "kv_tables"


def urls(s):
    p = s["ports"]
    ns = s["nodes"]
    return dict(
        api=f"http://{ns['decode']['ip']}:{p['api']}/v1",
        etcd=f"http://{ns['decode']['ip']}:{p['etcd']}",
        nats=f"nats://{ns['decode']['ip']}:{p['nats']}",
        prefill_kvm=f"http://{ns['prefill']['ip']}:{p['kvm_health']}",
        decode_kvm=f"http://{ns['decode']['ip']}:{p['kvm_health']}",
    )


def config_files(s):
    script = Path(__file__).with_name("render_configs.py")
    spec = importlib.util.spec_from_file_location("recipe_renderer", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _, _, tables = locations(s)
    n = s["nodes"]
    p = s["ports"]
    files = mod.render(
        SimpleNamespace(
            dgen=Path(s["paths"]["dgen"]),
            prefill_ip=n["prefill"]["ip"],
            decode_ip=n["decode"]["ip"],
            prefill_runner_hostname=n["prefill"]["runner"]["hostname"],
            decode_runner_hostname=n["decode"]["runner"]["hostname"],
            table_dir=tables,
            etcd_endpoint=urls(s)["etcd"],
        )
    )
    for role in ("prefill", "decode"):
        c = json.loads(files[f"dynamo.{role}.json"])
        r = c["runtime"]
        r["kv_control_port"] = p["kv_control"]
        r["kv_peers"][0]["port"] = p["kv_control"]
        r["kv_endpoint"] = f"tcp://{n['prefill']['ip']}:{p['kv_ingress']}"
        files[f"dynamo.{role}.json"] = json.dumps(c, indent=2) + "\n"
        env = dict(line.split("=", 1) for line in files[f"kvm.{role}.env"].splitlines())
        env.update(CONTROL_PORT=str(p["kvm_control"]), HEALTH_PORT=str(p["kvm_health"]))
        if role == "prefill":
            env["KV_MANAGER_TRANSPORT_ENDPOINT"] = r["kv_endpoint"]
        files[f"kvm.{role}.env"] = "".join(f"{k}={v}\n" for k, v in env.items())
    return files


def plan(s):
    """Services are grouped into phases: start whole phase before waiting on peers."""
    run, cfg, tables = locations(s)
    p = s["paths"]
    ports = s["ports"]
    u = urls(s)
    dgen = Path(p["dgen"])
    metal = dgen / "third_party/tt-blaze/tt-metal"
    py = str(dgen / "adapters/dynamo/.venv/bin/python")
    services = []

    def add(name, role, scope, phase, argv, env=None, ready=None, **extra):
        services.append(
            dict(
                name=name,
                role=role,
                scope=scope,
                phase=phase,
                argv=[str(v) for v in argv],
                env=env or {},
                cwd=p["dgen"],
                state=str(run / "control" / (name + ".json")),
                log=str(run / "logs" / (name + ".log")),
                ready=ready or {},
                **extra,
            )
        )

    def docker(name, role, phase, image, args, flags=(), ready=None, **extra):
        cname = f"{s['name']}-{name}"
        argv = s["docker_argv"] + [
            "run",
            "--rm",
            "--name",
            cname,
            "--label",
            "disagg.owner={owner}",
            "--network",
            "host",
        ]
        add(
            name,
            role,
            "host",
            phase,
            argv + list(flags) + [image] + args,
            ready=ready,
            container_name=cname,
            docker_argv=s["docker_argv"],
            **extra,
        )

    add(
        "etcd",
        "decode",
        "host",
        0,
        [
            str(dgen / ".dynamo_local/etcd/etcd"),
            "--name",
            s["name"],
            "--data-dir",
            str(run / "etcd-data"),
            "--listen-client-urls",
            u["etcd"],
            "--advertise-client-urls",
            u["etcd"],
            "--listen-peer-urls",
            f"http://127.0.0.1:{ports['etcd_peer']}",
            "--initial-advertise-peer-urls",
            f"http://127.0.0.1:{ports['etcd_peer']}",
            "--initial-cluster",
            f"{s['name']}=http://127.0.0.1:{ports['etcd_peer']}",
        ],
        ready={"http": u["etcd"] + "/health"},
    )
    docker(
        "nats",
        "decode",
        0,
        s["images"]["nats"],
        [
            "--addr",
            s["nodes"]["decode"]["ip"],
            "--port",
            str(ports["nats"]),
            "--jetstream",
            "--store_dir",
            "/data",
        ],
        flags=["--mount", f"type=bind,src={run}/nats-data,dst=/data"],
        ready={"tcp": [s["nodes"]["decode"]["ip"], ports["nats"]]},
        mkdir=[str(run / "nats-data")],
    )
    add(
        "prefill-runner",
        "prefill",
        "runner",
        1,
        ["bash", str(cfg / "launch-prefill.sh")],
        ready={
            "log": "setup complete, entering request loop",
            "files": [
                str(tables / "mistral4_prefill_kv_table.pb"),
                str(tables / "mistral4_prefill_device_map.txt"),
            ],
        },
        mkdir=[str(tables), p["prefill_jit"]],
        guard_models=True,
    )
    add(
        "decode-runner",
        "decode",
        "runner",
        1,
        ["bash", str(dgen / "scripts/mistral4/serve_mistral4_ring.sh")],
        env=dict(
            BLAZE=p.get("decode_blaze", str(dgen / "third_party/tt-blaze")),
            TT_MISTRAL_MODEL_PATH=p["checkpoint"],
            TT_MISTRAL_WEIGHT_CACHE=p["decode_cache"],
            TT_METAL_CACHE=p["decode_jit"],
            TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES="0",
            N_STAGES="2",
            LAYERS_PER_VISIT="2",
            N_SLOTS="1",
            CACHE_TOKENS="8192",
            ROUTED_EXPERT_TP="1",
            PREFIX="mistral4",
            TABLE=str(tables / "mistral4_decode_kv_table.pb"),
            DEVMAP=str(tables / "mistral4_decode_device_map.txt"),
            STOP=str(run / "decode.stop"),
            LOG=str(run / "logs/decode-model.log"),
        ),
        ready={
            "log": "KVM ready: layers=0-35",
            "log_path": str(run / "logs/decode-model.log"),
            "files": [
                str(tables / "mistral4_decode_kv_table.pb"),
                str(tables / "mistral4_decode_device_map.txt"),
            ],
        },
        stop_file=str(run / "decode.stop"),
        mkdir=[str(tables), p["decode_jit"], p["decode_cache"]],
        guard_models=True,
    )
    for role in ("prefill", "decode"):
        docker(
            role + "-kvm",
            role,
            2,
            s["images"]["kvm"],
            [],
            flags=[
                "--ipc",
                "host",
                "--ulimit",
                "memlock=-1",
                "--cap-add",
                "IPC_LOCK",
                "--device",
                "/dev/tenstorrent",
                "--mount",
                "type=bind,src=/dev/shm,dst=/dev/shm",
                "--mount",
                "type=bind,src=/dev/hugepages-1G,dst=/dev/hugepages-1G",
                "--mount",
                f"type=bind,src={tables},dst={tables},readonly",
                "--env-file",
                str(cfg / f"kvm.{role}.env"),
            ],
            ready={"http": u[role + "_kvm"] + "/health"},
            inputs=[
                str(tables / f"mistral4_{r}_{f}")
                for r in ("prefill", "decode")
                for f in ("kv_table.pb", "device_map.txt")
            ],
            listen=[[s["nodes"][role]["ip"], ports["kvm_control"]]]
            + (
                [[s["nodes"][role]["ip"], ports["kv_ingress"]]]
                if role == "prefill"
                else []
            ),
        )
        node = s["nodes"][role]
        host_ip = node["ip"]
        if node["runner"]["mode"] == "ird":
            for port_name in ("kv_control", "response"):
                port = ports[port_name]
                add(
                    role + "-" + port_name + "-relay",
                    role,
                    "host",
                    3,
                    [
                        "python3",
                        str(cfg / "tcp_forward.py"),
                        "--listen",
                        f"{host_ip}:{port}",
                        "--target",
                        f"{node['runner']['ip']}:{port}",
                    ],
                    ready={"tcp": [host_ip, port]},
                )
        env = dict(
            ETCD_ENDPOINTS=u["etcd"],
            NATS_SERVER=u["nats"],
            DYN_SYSTEM_PORT=str(ports["worker_health"]),
            DYN_LOG="info",
            DYN_SELF_HOST_METADATA="0",
            DYN_TCP_RESPONSE_STREAM_HOST=host_ip,
            DYN_TCP_RESPONSE_STREAM_PORT=str(ports["response"]),
            TT_METAL_HOME=str(metal),
            TT_METAL_RUNTIME_ROOT=str(metal),
            PYTHONPATH=f"{dgen}/adapters/dynamo:{dgen}/bindings/python",
            LD_LIBRARY_PATH=":".join(
                v for v in (str(metal / "build/lib"), p.get("mpi_lib", "")) if v
            ),
        )
        add(
            role + "-worker",
            role,
            "runner",
            4,
            [
                py,
                "-u",
                "-m",
                "tt_dynamo.main",
                "--config",
                str(cfg / f"dynamo.{role}.json"),
                "--model-path",
                p["checkpoint"],
                "--served-model-name",
                s["model_name"],
                "--namespace",
                s["namespace"],
                "--component",
                "prefill" if role == "prefill" else "backend",
                "--request-plane",
                "nats",
                "--event-plane",
                "nats",
            ],
            env=env,
            ready={
                "log": f"Serving {s['model_name']} on {s['namespace']}.{'prefill' if role=='prefill' else 'backend'}.generate"
            },
            listen=[
                ["0.0.0.0", ports[k]]
                for k in ("worker_health", "kv_control", "response")
            ],
        )
    add(
        "frontend",
        "decode",
        "host",
        5,
        [
            py,
            "-u",
            "-m",
            "dynamo.frontend",
            "--http-host",
            "0.0.0.0",
            "--http-port",
            str(ports["api"]),
            "--namespace",
            s["namespace"],
            "--discovery-backend",
            "etcd",
            "--request-plane",
            "nats",
            "--event-plane",
            "nats",
            "--router-mode",
            "kv",
            "--router-kv-events",
            "--enforce-disagg",
        ],
        env=dict(
            ETCD_ENDPOINTS=u["etcd"],
            NATS_SERVER=u["nats"],
        ),
        unset_env=["DYN_TCP_RESPONSE_STREAM_HOST", "DYN_TCP_RESPONSE_STREAM_PORT"],
        ready={"models": u["api"] + "/models", "model": s["model_name"]},
    )
    for service in services:
        service["config_inputs"] = [str(cfg / "site.resolved.json")]
        name = service["name"]
        if name == "prefill-runner":
            service["config_inputs"] += [
                str(cfg / "launch-prefill.sh"),
                str(cfg / "runner-prefill.yaml"),
            ]
        elif name.endswith("-worker"):
            service["config_inputs"].append(str(cfg / f'dynamo.{service["role"]}.json'))
        elif name.endswith("-kvm"):
            service["config_inputs"].append(str(cfg / f'kvm.{service["role"]}.env'))
        elif name.endswith("-relay"):
            service["config_inputs"].append(str(cfg / "tcp_forward.py"))
    return sorted(services, key=lambda x: x["phase"])


def render_site(s):
    run, cfg, _ = locations(s)
    if cfg.exists() and any(cfg.iterdir()):
        raise ValueError(f"{cfg} is nonempty; choose a fresh runtime directory")
    files = config_files(s)
    p = s["paths"]
    q = shlex.quote
    files["launch-prefill.sh"] = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f'cd {q(p["prefill_metal"])}',
            "source python_env/bin/activate",
            f'export MISTRAL4_HF_MODEL={q(p["checkpoint"])}',
            f'export PREFILL_TTNN_CACHE={q(p["prefill_cache"])}',
            f'export PP_TT_METAL_CACHE={q(p["prefill_jit"])}',
            "exec models/demos/common/prefill/runners/run_pipeline_prefill.sh "
            + q(str(cfg / "runner-prefill.yaml"))
            + " localhost:1 lo",
            "",
        ]
    )
    files["tcp_forward.py"] = (
        REPO / ".agents/skills/setup-disagg/scripts/tcp_forward.py"
    ).read_text()
    files["site.resolved.json"] = json.dumps(s, indent=2) + "\n"
    files["plan.json"] = json.dumps(plan(s), indent=2) + "\n"
    cfg.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        with (cfg / name).open("x") as f:
            f.write(value)
    return cfg
