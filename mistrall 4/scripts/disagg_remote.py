"""SSH transport: use existing keys/agent/control sockets, never passwords."""

import json
from pathlib import Path
import shlex
import subprocess


def rpc(site, role, scope, action, **payload):
    node = site["nodes"][role]
    runner = node["runner"]
    ssh = node["ssh"]
    source = Path(__file__).with_name("disagg_agent.py").read_text()
    command = ["python3", "-c", source]
    if scope == "runner" and runner["mode"] == "ird":
        command = (
            site["docker_argv"]
            + ["exec", "-i", "-u", runner["user"], runner["container"]]
            + command
        )
    hostname = runner["hostname"] if scope == "runner" else node["hostname"]
    request = dict(action=action, expected_hostname=hostname, **payload)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(ssh["port"]),
    ] + ssh.get("options", [])
    argv += [ssh["user"] + "@" + ssh["host"], shlex.join(command)]
    result = subprocess.run(
        argv,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=max(180, payload.get("timeout", 0) + 30),
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(
            f"{role}/{scope}: SSH/RPC failed: {result.stderr[-1500:]}"
        ) from None
    if result.returncode or "error" in data:
        raise RuntimeError(
            f"{role}/{scope}: {data.get('error', result.stderr[-1500:])}"
        )
    return data
