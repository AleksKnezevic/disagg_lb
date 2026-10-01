"""Offline regression tests; no accelerator, SSH, or Docker access."""

import json
import importlib.util
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import disagg
import disagg_agent as agent
import disagg_config as config
import disagg_smoke as smoke
import disagg_preflight as preflight


def example():
    variables = {
        f"DISAGG_{key.upper()}": "/example/" + key
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
        )
    }
    with patch.dict(os.environ, variables):
        return config.load_site(ROOT / "configs/deployment.example.json")


class ConfigTests(unittest.TestCase):
    def test_frontend_uses_native_auto_interface_discovery(self):
        frontend = next(s for s in config.plan(example()) if s["name"] == "frontend")
        self.assertNotIn("DYN_TCP_RESPONSE_STREAM_HOST", frontend["env"])
        self.assertIn("DYN_TCP_RESPONSE_STREAM_HOST", frontend["unset_env"])

    def test_unset_site_path_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "unset environment"):
                config.load_site(ROOT / "configs/deployment.example.json")

    def test_separate_decode_checkout_is_used(self):
        site = example()
        site["paths"]["decode_blaze"] = "/example/separate-blaze"
        runner = next(s for s in config.plan(site) if s["name"] == "decode-runner")
        self.assertEqual(runner["env"]["BLAZE"], "/example/separate-blaze")
        self.assertEqual(runner["env"]["TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES"], "0")

    def test_decode_grid_and_duplicate_hosts_rejected(self):
        for mutation in ("grid", "host", "port"):
            site = example()
            if mutation == "grid":
                site["nodes"]["decode"]["runner"]["grid"] = [12, 10]
            elif mutation == "host":
                site["nodes"]["decode"]["ip"] = site["nodes"]["prefill"]["ip"]
            else:
                site["ports"]["response"] = site["ports"]["kv_control"]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "site.json"
                path.write_text(json.dumps(site))
                with self.assertRaises(ValueError):
                    config.load_site(path)

    def test_native_omits_relays_and_ird_preserves_namespace(self):
        site = example()
        specs = config.plan(site)
        self.assertEqual(sum(s["name"].endswith("-relay") for s in specs), 4)
        self.assertEqual([s["phase"] for s in specs], sorted(s["phase"] for s in specs))
        self.assertTrue(
            all(s["scope"] == "runner" for s in specs if s["name"].endswith("-worker"))
        )
        self.assertTrue(
            all(s["phase"] == 2 for s in specs if s["name"].endswith("-kvm"))
        )
        for node in site["nodes"].values():
            node["runner"]["mode"] = "native"
            node["runner"]["hostname"] = node["hostname"]
        self.assertFalse(any(s["name"].endswith("-relay") for s in config.plan(site)))

    def test_render_propagates_custom_ports_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = example()
            site["paths"]["runtime"] = tmp + "/runtime"
            site["paths"]["dgen"] = tmp + "/dgen"
            site["ports"] = {k: v + 10000 for k, v in site["ports"].items()}
            templates = Path(site["paths"]["dgen"]) / "models/mistral-small-4"
            templates.mkdir(parents=True)
            for role in ("prefill", "decode"):
                template = dict(
                    runtime=dict(role=role, kv_manager="kvm"),
                    device=dict(workload={}, prefill={}, decode={}),
                )
                (templates / f"dynamo.kvm.{role}.json").write_text(json.dumps(template))
            out = config.render_site(site)
            for role in ("prefill", "decode"):
                worker = json.loads((out / f"dynamo.{role}.json").read_text())
                self.assertEqual(worker["runtime"]["kv_control_port"], 29071)
                self.assertTrue(worker["runtime"]["kv_endpoint"].endswith(":19093"))
                self.assertIn(
                    "CONTROL_PORT=28650", (out / f"kvm.{role}.env").read_text()
                )
                self.assertIn(":12379", (out / f"kvm.{role}.env").read_text())
            with self.assertRaises(ValueError):
                config.render_site(site)


class RelayTests(unittest.TestCase):
    def test_large_response_after_client_half_close(self):
        spec = importlib.util.spec_from_file_location(
            "relay", ROOT.parent / ".agents/skills/setup-disagg/scripts/tcp_forward.py"
        )
        relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relay)

        class ReplyAfterEOF(socketserver.BaseRequestHandler):
            def handle(self):
                chunks = []
                while data := self.request.recv(16384):
                    chunks.append(data)
                self.request.sendall(b"".join(chunks)[::-1])

        with socketserver.TCPServer(("127.0.0.1", 0), ReplyAfterEOF) as upstream:
            with relay.Server(("127.0.0.1", 0), upstream.server_address) as proxy:
                threads = [
                    threading.Thread(target=s.serve_forever, daemon=True)
                    for s in (upstream, proxy)
                ]
                for t in threads:
                    t.start()
                try:
                    payload = os.urandom(133000)
                    with socket.create_connection(
                        proxy.server_address, timeout=5
                    ) as client:
                        client.sendall(payload)
                        client.shutdown(socket.SHUT_WR)
                        chunks = []
                        while data := client.recv(16384):
                            chunks.append(data)
                    self.assertEqual(b"".join(chunks), payload[::-1])
                finally:
                    proxy.shutdown()
                    upstream.shutdown()
                    for t in threads:
                        t.join(timeout=5)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spec = dict(
            name="test",
            argv=[sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(self.root),
            state=str(self.root / "state.json"),
            log=str(self.root / "log"),
            ready={},
        )
        self.records = []

    def tearDown(self):
        for record in self.records:
            if agent.alive(record):
                os.killpg(record["pid"], signal.SIGKILL)
            try:
                os.waitpid(record["pid"], 0)
            except ChildProcessError:
                pass
        self.tmp.cleanup()

    def launch(self):
        # Agent intentionally detaches its child; this test owns/reaps it in tearDown.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            value = agent.start(self.spec, config.digest(self.spec), {})
        self.records.append(value["record"])
        return value["record"]

    def test_idempotent_start_and_graceful_stop(self):
        record = self.launch()
        again = agent.start(self.spec, config.digest(self.spec), {})
        self.assertEqual(again["record"]["pid"], record["pid"])
        self.assertEqual(agent.stop(self.spec, 3)["state"], "stopped")

    def test_modified_table_blocks_reuse(self):
        table = self.root / "table"
        table.write_text("old addresses")
        self.spec["inputs"] = [str(table)]
        self.launch()
        table.write_text("new addresses")
        self.assertEqual(agent.status(self.spec)["stale_inputs"], [str(table)])
        with self.assertRaisesRegex(RuntimeError, "stale KV"):
            agent.start(self.spec, config.digest(self.spec), {})

    def test_pid_reuse_is_not_signaled(self):
        record = self.launch()
        forged = dict(record, start="0")
        Path(self.spec["state"]).write_text(json.dumps(forged))
        # A group still exists: require manual diagnosis instead of signaling.
        with self.assertRaisesRegex(RuntimeError, "orphaned_process_group"):
            agent.stop(self.spec, 1)
        self.assertTrue(agent.alive(record))

    def test_occupied_port_does_not_start_or_adopt(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            self.spec["ready"] = {"tcp": list(sock.getsockname())}
            with self.assertRaisesRegex(RuntimeError, "cannot reserve"):
                self.launch()
            self.assertFalse(Path(self.spec["state"]).exists())

    def test_children_exit_before_dependencies_can_stop(self):
        child_file = self.root / "child.pid"
        self.spec["argv"] = [
            sys.executable,
            "-c",
            "import subprocess,time; from pathlib import Path; "
            f'p=subprocess.Popen([{sys.executable!r},"-c","import time; time.sleep(60)"]); '
            f"Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)",
        ]
        record = self.launch()
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_file.exists())
        agent.stop(self.spec, 3)
        self.assertFalse(agent.group_alive(record))

    def test_wrong_namespace_rpc_rejected(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/disagg_agent.py")],
            input=json.dumps(
                dict(action="start", expected_hostname="not-this-host", spec=self.spec)
            ),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostname differs", result.stdout)
        self.assertFalse(Path(self.spec["state"]).exists())

    def test_detached_process_survives_agent_and_is_stoppable(self):
        def invoke(action, **kwargs):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/disagg_agent.py")],
                input=json.dumps(
                    dict(
                        action=action,
                        spec=self.spec,
                        expected_hostname=socket.gethostname(),
                        **kwargs,
                    )
                ),
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)

        value = invoke(
            "start", spec_hash=config.digest(self.spec), dependency_tokens={}
        )
        self.records.append(value["record"])
        self.assertEqual(invoke("status")["state"], "running")
        self.assertEqual(invoke("stop", timeout=3)["state"], "stopped")

    def test_old_table_cannot_satisfy_runner_readiness(self):
        table = self.root / "old-table"
        table.write_text("old")
        os.utime(table, (1, 1))
        self.spec["ready"] = dict(files=[str(table)])
        record = self.launch()
        self.assertFalse(agent.ready(self.spec, record))

    def test_initializing_runner_uses_sigterm_instead_of_unread_stop_file(self):
        self.spec["stop_file"] = str(self.root / "stop")
        self.spec["ready"] = {"log": "not yet ready"}
        self.launch()
        self.assertEqual(agent.stop(self.spec, 3)["state"], "stopped")
        self.assertFalse(Path(self.spec["stop_file"]).exists())

    def test_launch_can_clear_inherited_transport_environment(self):
        self.spec["argv"] = [
            sys.executable,
            "-c",
            'import os,time; print(os.getenv("DISAGG_TEST_INHERITED", "cleared"), flush=True); time.sleep(60)',
        ]
        self.spec["unset_env"] = ["DISAGG_TEST_INHERITED"]
        self.spec["ready"] = {"log": "cleared"}
        with patch.dict(os.environ, {"DISAGG_TEST_INHERITED": "wrong-interface"}):
            record = self.launch()
        deadline = time.monotonic() + 3
        while not agent.ready(self.spec, record) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(agent.ready(self.spec, record))

    def test_docker_access_failure_is_not_an_absent_container(self):
        spec = dict(container_name="example", docker_argv=["docker"])
        result = subprocess.CompletedProcess([], 1, "", "permission denied")
        with patch.object(agent.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "inspect Docker ownership"):
                agent.container(spec)

    def test_docker_missing_container_is_case_insensitive(self):
        spec = dict(container_name="example", docker_argv=["docker"])
        for message in (
            "Error: No such object: example",
            "error: no such object: example",
        ):
            result = subprocess.CompletedProcess([], 1, "", message)
            with patch.object(agent.subprocess, "run", return_value=result):
                self.assertIsNone(agent.container(spec))


def metric_text(role, count, failure=0):
    common = f'kv_manager_dp_rdma_batch_failures_total{{kind="data",reason="failed"}} {failure}\n'
    if role == "prefill":
        return common + (
            f'kv_manager_engine_commands_total{{kind="migrate",result="ok"}} {count}\n'
            f'kv_manager_bytes_moved_total{{leg="wire"}} {count * 100}\n'
            f'kv_manager_dp_device_bytes_total{{direction="d2h"}} {count * 100}\n'
        )
    return (
        common + f'kv_manager_dp_device_bytes_total{{direction="h2d"}} {count * 200}\n'
    )


class SmokeTests(unittest.TestCase):
    def run_smoke(self, move=True, correct=True, fail=False):
        site = example()
        count = 0
        requests = 0

        def fake(url, timeout, body=None):
            nonlocal count, requests
            if url.endswith("/health"):
                return '{"status":"healthy"}'
            if url.endswith("/models"):
                return json.dumps({"data": [{"id": site["model_name"]}]})
            if url.endswith("/metrics"):
                role = "prefill" if site["nodes"]["prefill"]["ip"] in url else "decode"
                return metric_text(role, count, int(fail and requests > 0))
            requests += 1
            count += (36 if requests == 1 else 72) if move else 0
            code = (
                body["messages"][0]["content"].split("access code: ")[1].split(".")[0]
            )
            return json.dumps(
                dict(
                    id="test",
                    choices=[
                        dict(
                            finish_reason="stop",
                            message=dict(content=code if correct else "wrong"),
                        )
                    ],
                    usage=dict(prompt_tokens=1286 if requests == 1 else 6291),
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            if move and correct and not fail:
                result = smoke.smoke(site, out, get=fake)
                self.assertTrue(result["passed"])
                self.assertEqual(result["tests"][1]["destination_bytes"], 14400)
            else:
                with self.assertRaises(ValueError):
                    smoke.smoke(site, out, get=fake)
                self.assertFalse(
                    json.loads((out / "summary.json").read_text())["passed"]
                )
                self.assertTrue((out / "fresh-response.json").exists())

    def test_migration_with_destination_replication(self):
        self.run_smoke()

    def test_http_success_without_migration_fails(self):
        self.run_smoke(move=False)

    def test_wrong_answer_fails(self):
        self.run_smoke(correct=False)

    def test_new_failure_counter_fails(self):
        self.run_smoke(fail=True)

    def test_counter_reset_fails(self):
        with self.assertRaisesRegex(ValueError, "reset"):
            smoke.deltas(
                smoke.metrics(metric_text("prefill", 10)),
                smoke.metrics(metric_text("prefill", 1)),
            )

    def test_missing_metric_fails(self):
        before = {r: metric_text(r, 0) for r in ("prefill", "decode")}
        after = dict(prefill=metric_text("prefill", 36), decode="")
        with self.assertRaises(ValueError):
            smoke.migration_evidence(before, after, 1)


class OrchestrationTests(unittest.TestCase):
    def fixture(self, tmp):
        site = example()
        site["paths"]["runtime"] = tmp
        cfg = Path(tmp) / "generated"
        cfg.mkdir()
        (cfg / "site.resolved.json").write_text(json.dumps(site))
        (cfg / "plan.json").write_text(json.dumps(config.plan(site)))
        return site

    def test_start_waits_for_both_peers_and_stop_reverses_order(self):
        events, records = [], {}

        def fake(site, spec, action, **kwargs):
            name = spec["name"]
            if action in ("start", "stop") or (action == "ready" and name in records):
                events.append((action, name))
            if action == "start":
                records[name] = dict(
                    pid=len(records) + 100,
                    start="start",
                    boot="boot",
                    hostname=name,
                    spec_hash=kwargs["spec_hash"],
                    dependency_tokens=kwargs["dependency_tokens"],
                )
            if action == "stop":
                records.pop(name, None)
            return dict(
                state="running" if name in records else "stopped",
                record=records.get(name),
                ready=True,
                # Old table fingerprints in a stopped manager must not prevent
                # restarting it with newly exported tables.
                stale_inputs=[] if name in records else ["old-table"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            site = self.fixture(tmp)
            with patch.object(disagg, "call", side_effect=fake), patch.object(
                preflight, "preflight", return_value={"checks": []}
            ):
                disagg.lifecycle(site, "start")
                disagg.lifecycle(site, "stop")
        self.assertLess(
            events.index(("start", "decode-kvm")),
            events.index(("ready", "prefill-kvm")),
        )
        start = [name for action, name in events if action == "start"]
        stop = [name for action, name in events if action == "stop"]
        self.assertEqual(stop, list(reversed(start)))

    def test_stop_timeout_preserves_lower_dependencies(self):
        stopped = []

        def fake(site, spec, action, **kwargs):
            stopped.append(spec["name"])
            if spec["name"] == "decode-worker":
                raise RuntimeError("graceful stop timed out")
            return dict(state="stopped")

        with tempfile.TemporaryDirectory() as tmp:
            site = self.fixture(tmp)
            with patch.object(disagg, "call", side_effect=fake):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    disagg.lifecycle(site, "stop")
        self.assertEqual(stopped, ["frontend", "decode-worker"])

    def test_runner_restart_invalidates_managers(self):
        site = example()

        def fake(site, spec, action):
            record = dict(
                pid=100,
                start="new",
                boot="boot",
                hostname="host",
                spec_hash=config.digest(spec),
                dependency_tokens={},
            )
            return dict(state="running", record=record, stale_inputs=[])

        with patch.object(disagg, "call", side_effect=fake):
            states = disagg.statuses(site, config.plan(site))
        self.assertTrue(states["prefill-kvm"]["stale_runners"])
        self.assertTrue(states["decode-kvm"]["stale_runners"])

    def test_preflight_only_uses_probe_and_access_failures_fail(self):
        actions = []

        def fake(site, role, scope, action, **kwargs):
            actions.append(action)
            raise RuntimeError("test inaccessible reservation")

        with patch.object(preflight, "rpc", side_effect=fake):
            result = preflight.preflight(example())
        self.assertEqual(actions, ["probe"] * 4)
        self.assertEqual(sum(x["level"] == "FAIL" for x in result["checks"]), 5)


if __name__ == "__main__":
    unittest.main()
