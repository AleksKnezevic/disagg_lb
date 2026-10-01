#!/usr/bin/env python3
"""Render, inspect, launch, and validate the pinned two-LoudBox deployment."""

import argparse
import fcntl
import json
from pathlib import Path
import sys
import time

from disagg_config import digest, load_site, locations, plan, render_site, urls
from disagg_remote import rpc


def call(site, spec, action, **kwargs):
    return rpc(site, spec["role"], spec["scope"], action, spec=spec, **kwargs)


def runner_tokens(states):
    result = {}
    for name in ("prefill-runner", "decode-runner"):
        value = states[name]
        record = value.get("record")
        result[name] = (
            {k: record[k] for k in ("pid", "start", "boot", "hostname")}
            if record and value["state"] == "running"
            else None
        )
    return result


def statuses(site, specs):
    states = {s["name"]: call(site, s, "ready") for s in specs}
    tokens = runner_tokens(states)
    for spec in specs:
        value = states[spec["name"]]
        if value["state"] == "running":
            value["config_changed"] = value["record"]["spec_hash"] != digest(spec)
            if spec.get("inputs"):
                value["stale_runners"] = (
                    None in tokens.values()
                    or value["record"].get("dependency_tokens") != tokens
                )
            if (
                value.get("stale_inputs")
                or value.get("stale_runners")
                or value.get("config_changed")
            ):
                value["ready"] = False
    return states


def wait_ready(site, specs):
    seconds = site["timeouts"][
        "runner_start" if specs[0]["phase"] == 1 else "service_start"
    ]
    deadline = time.monotonic() + seconds
    pending = {s["name"]: s for s in specs}
    last_update = 0
    while pending:
        for name, spec in list(pending.items()):
            value = call(site, spec, "ready")
            if value["state"] != "running":
                raise RuntimeError(f'{name} exited; inspect {spec["log"]}')
            if value["ready"]:
                print(f"{name}: ready", flush=True)
                del pending[name]
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("readiness timeout: " + ", ".join(pending))
        if time.monotonic() - last_update > 30:
            print("Waiting for " + ", ".join(pending), flush=True)
            last_update = time.monotonic()
        time.sleep(2)


def lifecycle(site, action):
    run, cfg, _ = locations(site)
    # Require the original site even for stop, so a changed SSH target cannot
    # accidentally select a different reservation with similarly named files.
    saved = json.loads((cfg / "site.resolved.json").read_text())
    if saved != site:
        raise ValueError(
            "site differs from rendered deployment; use its original site to stop it"
        )
    specs = plan(site)
    if json.loads((cfg / "plan.json").read_text()) != specs:
        raise ValueError(
            "launch plan changed since render; use the original tooling to stop first"
        )
    (run / "control").mkdir(parents=True, exist_ok=True)
    with (run / "control/orchestration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "stop":
            for spec in reversed(specs):
                result = call(site, spec, "stop", timeout=site["timeouts"]["stop"])
                print(f'{spec["name"]}: {result["state"]}', flush=True)
            return
        states = statuses(site, specs)
        for name, value in states.items():
            if value["state"] == "running" and (
                value.get("stale_inputs")
                or value.get("stale_runners")
                or value.get("config_changed")
            ):
                raise RuntimeError(
                    f"{name}: stale inputs/runner generation/config; drain and stop first"
                )
            if value["state"] in (
                "orphaned_container",
                "orphaned_process_group",
                "wrong_namespace",
            ):
                raise RuntimeError(
                    f'{name}: {value["state"]}; inspect ownership before recovery'
                )
        from disagg_preflight import preflight

        report = preflight(site)
        failures = [x for x in report["checks"] if x["level"] == "FAIL"]
        if failures:
            raise RuntimeError(
                "preflight failed: " + "; ".join(x["check"] for x in failures)
            )
        for phase in sorted({s["phase"] for s in specs}):
            group = [s for s in specs if s["phase"] == phase]
            tokens = runner_tokens(states) if phase == 2 else {}
            # Both managers must start before waiting for peer discovery.
            for spec in group:
                result = call(
                    site,
                    spec,
                    "start",
                    spec_hash=digest(spec),
                    dependency_tokens=tokens if spec.get("inputs") else {},
                )
                print(f'{spec["name"]}: {result["state"]}', flush=True)
            wait_ready(site, group)
            if phase == 1:
                states = statuses(site, specs)
        print("API: " + urls(site)["api"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "plan", "start", "status", "stop"):
        sub.add_parser(name)
    p = sub.add_parser("preflight")
    p.add_argument("--report", type=Path)
    p.add_argument(
        "--serving", action="store_true", help="require live network endpoints"
    )
    p = sub.add_parser("smoke")
    p.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        site = load_site(args.site)
        if args.command == "render":
            print("Generated " + str(render_site(site)) + "; no services started.")
        elif args.command == "plan":
            print(json.dumps(plan(site), indent=2))
        elif args.command == "status":
            print(json.dumps(statuses(site, plan(site)), indent=2))
        elif args.command == "preflight":
            from disagg_preflight import preflight

            report = preflight(site, args.serving)
            for item in report["checks"]:
                print(f'{item["level"]}: {item["check"]}: {item["detail"]}')
            if args.report:
                with args.report.open("x") as f:
                    json.dump(report, f, indent=2)
            return 1 if any(x["level"] == "FAIL" for x in report["checks"]) else 0
        elif args.command == "smoke":
            from disagg_smoke import smoke

            smoke(site, args.report_dir)
        else:
            lifecycle(site, args.command)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.command == "start":
            print(
                "Any services already launched remain recorded. Inspect status/logs; "
                "use stop for ordered cleanup.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
