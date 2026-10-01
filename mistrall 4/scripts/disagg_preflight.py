"""Read-only checks; no device opens, resets, builds, or service launches."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from disagg_config import locations
from disagg_remote import rpc


def preflight(site, serving=False):
    checks = []
    evidence = {}
    run, cfg, _ = locations(site)
    paths, ports = site["paths"], site["ports"]
    dgen = Path(paths["dgen"])
    py = str(dgen / "adapters/dynamo/.venv/bin/python")
    # Inspect only fields used by checks; never collect container environments.
    image_format = (
        '[{"Id":{{json .Id}},"Config":{"Labels":{{json (index .Config "Labels")}}}}]'
    )
    ird_format = (
        '[{"State":{"Running":{{json .State.Running}}},'
        '"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}]'
    )

    def check(level, name, detail):
        checks.append(dict(level=level, check=name, detail=detail))

    generated = sorted(p for p in cfg.glob("*") if p.is_file())
    if not (cfg / "site.resolved.json").is_file():
        check("FAIL", "rendered site", "run render before deployment preflight")
    elif json.loads((cfg / "site.resolved.json").read_text()) != site:
        check("FAIL", "rendered site", "site changed since render")
    requests = []
    for role in ("prefill", "decode"):
        node = site["nodes"][role]
        for scope in ("host", "runner"):
            files = [str(p) for p in generated]
            commands, pins, tcp = {}, {}, []
            if scope == "host":
                files += ["/dev/hugepages-1G", str(dgen)]
                commands["kvm-image"] = site["docker_argv"] + [
                    "image",
                    "inspect",
                    "--format",
                    image_format,
                    site["images"]["kvm"],
                ]
                if role == "decode":
                    files += [str(dgen / ".dynamo_local/etcd/etcd")]
                    commands["nats-image"] = site["docker_argv"] + [
                        "image",
                        "inspect",
                        "--format",
                        image_format,
                        site["images"]["nats"],
                    ]
                    commands["frontend-python"] = [py, "-c", "import dynamo.frontend"]
                if node["runner"]["mode"] == "ird":
                    commands["ird"] = site["docker_argv"] + [
                        "inspect",
                        "--format",
                        ird_format,
                        node["runner"]["container"],
                    ]
            else:
                blaze = (
                    Path(paths.get("decode_blaze", str(dgen / "third_party/tt-blaze")))
                    if role == "decode"
                    else dgen / "third_party/tt-blaze"
                )
                pins = dict(
                    dgen=str(dgen),
                    blaze=str(blaze),
                    decode_metal=str(blaze / "tt-metal"),
                )
                if role == "prefill":
                    pins["prefill_metal"] = paths["prefill_metal"]
                    commands["prefill-python"] = [
                        str(Path(paths["prefill_metal"]) / "python_env/bin/python"),
                        "-c",
                        "import importlib.util; assert importlib.util.find_spec('ttnn'); assert importlib.util.find_spec('torch')",
                    ]
                files += [
                    paths["checkpoint"] + "/" + name
                    for name in (
                        "config.json",
                        "tokenizer.json",
                        "model.safetensors.index.json",
                    )
                ]
                files += [paths[role + "_cache"]]
                commands["adapter-python"] = [
                    py,
                    "-c",
                    "import dynamo; import zmq",
                ]
                peer = site["nodes"]["decode" if role == "prefill" else "prefill"]["ip"]
                tcp = [
                    [site["nodes"]["decode"]["ip"], ports[k]] for k in ("etcd", "nats")
                ]
                tcp += [
                    [peer, ports[k]] for k in ("kv_control", "response", "kvm_control")
                ]
                tcp += [[site["nodes"]["prefill"]["ip"], ports["kv_ingress"]]]
            requests.append(
                (
                    role,
                    scope,
                    dict(
                        files=files,
                        commands=commands,
                        pins=pins,
                        tcp=tcp,
                        **(
                            {"checkpoint": paths["checkpoint"]}
                            if scope == "runner"
                            else {}
                        ),
                    ),
                )
            )

    def inspect(item):
        role, scope, request = item
        try:
            return role, scope, rpc(site, role, scope, "probe", **request), None
        except Exception as exc:
            return role, scope, None, str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for role, scope, result, error in pool.map(inspect, requests):
            label = f"{role}/{scope}"
            if error:
                check("FAIL", label + " access", error)
                continue
            evidence[label] = result
            check("PASS", label + " access", result["hostname"])
            count = len(result["devices"])
            check(
                "PASS" if count == 8 else "FAIL",
                label + " device visibility",
                f"{count} device entries; expected 8",
            )
            for path, value in result["files"].items():
                good = value is not None
                local = next((p for p in generated if str(p) == path), None)
                if local:
                    good = (
                        good
                        and value.get("sha256")
                        == hashlib.sha256(local.read_bytes()).hexdigest()
                    )
                check(
                    "PASS" if good else "FAIL",
                    label + " file " + path,
                    (
                        "present/matching"
                        if good
                        else "missing or differs from controller"
                    ),
                )
            for name, value in result["pins"].items():
                check(
                    "PASS" if value == site["pins"][name] else "FAIL",
                    label + " pin " + name,
                    value,
                )
            for name, value in result["commands"].items():
                good = value["code"] == 0
                detail = "available" if good else value["stderr"]
                if good and name == "kvm-image":
                    image = json.loads(value["stdout"])[0]
                    labels = image["Config"].get("Labels") or {}
                    good = labels.get("org.opencontainers.image.revision") == site[
                        "pins"
                    ]["dgen"] and labels.get("tt-d-gen.metal") in (
                        "with-metal-built",
                        "with-metal-injected",
                    )
                    detail = {"id": image["Id"], "labels": labels}
                if good and name == "ird":
                    image = json.loads(value["stdout"])[0]
                    ips = [
                        x["IPAddress"]
                        for x in image["NetworkSettings"]["Networks"].values()
                    ]
                    good = (
                        image["State"]["Running"]
                        and site["nodes"][role]["runner"]["ip"] in ips
                    )
                    detail = {"running": image["State"]["Running"], "addresses": ips}
                check("PASS" if good else "FAIL", label + " " + name, detail)
            for endpoint, good in result["tcp"].items():
                check(
                    "PASS" if good else "FAIL" if serving else "WARN",
                    label + " TCP " + endpoint,
                    (
                        "reachable"
                        if good
                        else "not reachable; expected before launch, required when serving"
                    ),
                )
            if scope == "runner":
                checkpoint = result.get("checkpoint", {})
                good = (
                    bool(checkpoint.get("shards"))
                    and not checkpoint.get("bad_shards")
                    and not checkpoint.get("error")
                )
                check(
                    "PASS" if good else "FAIL",
                    label + " checkpoint shard headers",
                    checkpoint,
                )
                check(
                    "WARN",
                    label + " hardware grid",
                    f"declared {site['nodes'][role]['runner']['grid']}; confirm actual grid from prior hardware probe; preflight does not open devices",
                )
            if result["owners"]:
                check("WARN", label + " existing device consumers", result["owners"])
    check(
        "WARN",
        "shared storage",
        "matching files do not prove a shared mount: verify live KV exports and Dynamo HOME metadata are shared across both hosts/IRDs",
    )
    check(
        "WARN",
        "payload networking",
        "static TCP checks do not test dynamic Mooncake RPC/data ports; smoke must demonstrate migrated bytes",
    )
    return dict(checks=checks, evidence=evidence)
