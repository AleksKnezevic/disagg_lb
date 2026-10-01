#!/usr/bin/env python3
"""Render the pinned Mistral Small 4 two-LoudBox deployment; never launch it."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
import zlib


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise argparse.ArgumentTypeError(
            "use a unicast host address reachable from the peer"
        )
    return str(address)


def hostname(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise argparse.ArgumentTypeError(
            "use the runner's literal hostname, without shell syntax"
        )
    return value


def absolute_path(value):
    if not Path(value).is_absolute() or any(c in value for c in "\r\n"):
        raise argparse.ArgumentTypeError("use an absolute path without line breaks")
    return Path(value)


def render(args):
    if args.prefill_ip == args.decode_ip:
        raise ValueError("this recipe requires two distinct host addresses")
    if args.prefill_runner_hostname == args.decode_runner_hostname:
        raise ValueError(
            "runner hostnames must differ so the KV tables have distinct owners"
        )
    endpoint = args.etcd_endpoint or f"http://{args.decode_ip}:2379"
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(c in endpoint for c in "\r\n")
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "etcd endpoint must be an http://host:port URL without credentials"
        )
    if parsed.port is None:
        raise ValueError("include the etcd port explicitly")

    assets = Path(__file__).resolve().parents[1]
    manifest = (
        assets / "configs/prefill/runner_1rank_loudbox_prefill_2x4.yaml"
    ).read_text()
    for key in ("PREFILL_ENABLE_MIGRATION", "PREFILL_MIGRATION_EXPORT_TO_FILE"):
        old = f'{key}: "0"'
        if manifest.count(old) != 1:
            raise ValueError(f"standalone manifest changed: expected exactly one {old}")
        manifest = manifest.replace(old, f'{key}: "1"')
    manifest = manifest.replace(
        "# Mistral Small 4 standalone prefill on one 2x4 Blackhole LoudBox.",
        "# Mistral Small 4 migration-enabled prefill on one 2x4 Blackhole LoudBox.",
    )
    manifest += (
        "\n"
        + "\n".join(
            f"  {key}: {json.dumps(str(args.table_dir / filename))}"
            for key, filename in (
                ("PREFILL_MIGRATION_TABLE_PATH", "mistral4_prefill_kv_table.pb"),
                (
                    "PREFILL_MIGRATION_DEVICE_MAP_PATH",
                    "mistral4_prefill_device_map.txt",
                ),
            )
        )
        + "\n"
    )
    files = {"runner-prefill.yaml": manifest}
    inventory = {
        "recipe": "mistral4-loudbox",
        "etcd_endpoint": endpoint,
        "table_dir": str(args.table_dir),
        "roles": {},
    }
    for role, ip, peer, runner_host, endpoint_id, capacity in (
        (
            "prefill",
            args.prefill_ip,
            args.decode_ip,
            args.prefill_runner_hostname,
            0,
            10240,
        ),
        (
            "decode",
            args.decode_ip,
            args.prefill_ip,
            args.decode_runner_hostname,
            1,
            8192,
        ),
    ):
        template = args.dgen / "models/mistral-small-4" / f"dynamo.kvm.{role}.json"
        config = json.loads(template.read_text())
        config.pop("_comment", None)
        runtime = config["runtime"]
        if runtime["role"] != role or runtime["kv_manager"] != "kvm":
            raise ValueError(f"unexpected role/backend in {template}")
        runtime.update(
            max_slots=1,
            max_seq_len=capacity,
            kv_block_size=64,
            kv_endpoint=f"tcp://{args.prefill_ip}:9093",
            kv_endpoint_id=endpoint_id,
            kv_control_port=19071,
            kv_num_layers=36,
            min_disagg_tokens=256,
        )
        runtime["kv_peers"] = [
            dict(
                host=peer,
                port=19071,
                endpoint_id=1 - endpoint_id,
                service="pd-kvm-ep0-ep1",
            )
        ]
        config["device"]["workload"]["manage"] = False
        if role == "prefill":
            runtime.update(
                layers_per_chunk=36,
                chunk_size=5120,
                chunk_aligned_start=True,
                kv_bootstrap_host=ip,
                min_copy_tokens=256,
            )
            config["device"]["prefill"].update(
                service_id="ds_prefill",
                ack_shm_name="/tt_prefill_layer_acks_ds_prefill",
                sp_factor=2,
            )
        else:
            runtime.update(
                decode_backend="base",
                chunk_size=8,
                pipeline_inflight_cap=1,
                min_copy_tokens=64,
            )
            config["device"]["decode"].update(
                wire_format="blaze", h2d_socket_id="mistral4", d2h_socket_id="mistral4"
            )
        files[f"dynamo.{role}.json"] = json.dumps(config, indent=2) + "\n"
        kvm_id = "prefill-0" if role == "prefill" else "decode-1"
        env = dict(
            KVM_ID=kvm_id,
            ROLE="leader",
            KV_MANAGER_TABLE_HOST=runner_host,
            KV_MANAGER_DISCOVERY_BACKEND="etcd",
            KV_MANAGER_ETCD_ENDPOINT=endpoint,
            KV_MANAGER_ADVERTISE_HOST=ip,
            MC_TCP_BIND_ADDRESS=ip,
            CONTROL_PORT="18650",
            HEALTH_PORT="18081",
            DEVICE_MAP=str(args.table_dir / f"mistral4_{role}_device_map.txt"),
            TT_VISIBLE_DEVICES="0,1,2,3,4,5,6,7",
            KV_MANAGER_DEVICE_IDS="0,1,2,3,4,5,6,7",
            TT_LOG_LEVEL="info",
            KV_MANAGER_DEVICE_IO="dmk",
            KV_MANAGER_TRANSFER_ENGINE_PROTOCOL="tcp",
            PREFILL_TABLE=str(args.table_dir / "mistral4_prefill_kv_table.pb"),
            DECODE_TABLE=str(args.table_dir / "mistral4_decode_kv_table.pb"),
        )
        if role == "prefill":
            env.update(
                KV_MANAGER_TRANSPORT_KIND="zmq",
                KV_MANAGER_TRANSPORT_ENDPOINT=f"tcp://{ip}:9093",
            )
        else:
            env["KV_MANAGER_TRANSPORT_KIND"] = "http"
        files[f"kvm.{role}.env"] = "".join(f"{k}={v}\n" for k, v in env.items())
        inventory["roles"][role] = dict(
            ip=ip,
            runner_hostname=runner_host,
            kvm_id=kvm_id,
            table_host="host-%08x" % (zlib.crc32(runner_host.encode()) & 0x7FFFFFFF),
            endpoint_id=endpoint_id,
            slots=1,
            capacity_tokens=capacity,
        )
    files["deployment.json"] = json.dumps(inventory, indent=2) + "\n"
    return files


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--site", type=Path, help="portable deployment JSON; includes launch plan"
    )
    p.add_argument("--dgen", type=absolute_path, help="pinned tt-d-gen checkout")
    p.add_argument("--prefill-ip", type=ipv4)
    p.add_argument("--decode-ip", type=ipv4)
    p.add_argument("--prefill-runner-hostname", type=hostname)
    p.add_argument("--decode-runner-hostname", type=hostname)
    p.add_argument(
        "--table-dir",
        type=absolute_path,
        help="same absolute table/map path visible to runners and KVMs",
    )
    p.add_argument("--etcd-endpoint", help="default: http://<decode-ip>:2379")
    p.add_argument("--output", type=absolute_path)
    args = p.parse_args()
    try:
        legacy = (
            "dgen",
            "prefill_ip",
            "decode_ip",
            "prefill_runner_hostname",
            "decode_runner_hostname",
            "table_dir",
            "output",
        )
        if args.site:
            if any(getattr(args, k) is not None for k in (*legacy, "etcd_endpoint")):
                raise ValueError(
                    "--site cannot be combined with individual deployment flags"
                )
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from disagg_config import load_site, render_site

            print(
                f"Generated {render_site(load_site(args.site))}; no services started."
            )
            return
        if any(getattr(args, k) is None for k in legacy):
            raise ValueError("provide --site or all individual deployment flags")
        if args.output.exists() and (
            not args.output.is_dir() or any(args.output.iterdir())
        ):
            raise ValueError(
                "output must be absent or empty; existing deployment will not be overwritten"
            )
        files = render(args)
        args.output.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            with (args.output / name).open("x") as f:
                f.write(text)
    except (OSError, ValueError, KeyError) as exc:
        p.exit(2, f"error: {exc}\n")
    print(f"Wrote {len(files)} files to {args.output}; no services were started.")


if __name__ == "__main__":
    main()
