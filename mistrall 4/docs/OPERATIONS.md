# Mistral Small 4 deployment tools

Run these commands from the `mistrall 4/` folder. Keep `paths.repo` and
`DISAGG_REPO` set to the repository root, not this recipe folder.

Use `python3 scripts/disagg.py --site SITE COMMAND` for the pinned Mistral Small
4 recipe: two eight-device LoudBoxes, one slot, prefill chunks of 5120 tokens,
and at most 8192 total prompt/output tokens at decode. Only Python 3.10+ and its
standard library are needed on the controller.

The tools support native model runners and bridged IRDs. Both use host-network
Docker KV managers, Docker NATS, and native etcd/frontend on the decode host.
For native KV-manager binaries or reuse of hand-launched services, follow the
[setup skill](../../.agents/skills/setup-disagg/SKILL.md). Lifecycle commands do
not adopt existing processes or containers.

## Prepare a site

```bash
cp configs/deployment.example.json deployment.local.json
# Edit every placeholder for this reservation, then:
python3 scripts/disagg.py --site deployment.local.json plan
python3 scripts/disagg.py --site deployment.local.json render
python3 scripts/disagg.py --site deployment.local.json preflight --report preflight.json
```

`deployment.local.json` is ignored by Git. Never put passwords/tokens in it.
The template's `${DISAGG_*}` path values are environment variables: export
them before rendering, or replace them with your site's paths. Unset variables
are rejected. All paths are resolved into the generated site inventory; the
reusable code contains no reservation-specific filesystem locations. Set the
optional `paths.decode_blaze` when the built decode checkout is outside
`DGEN/third_party/tt-blaze`, and `paths.mpi_lib` when MPI needs a library path.
SSH uses batch mode with existing keys, an SSH agent, or an authenticated
ControlMaster. `ssh.options` is an argv list, e.g.
`["-S", "/tmp/my-host-control"]`. Target the **bare-metal SSH endpoint**
(normally port 22); IRD port 41338 is not a host-management endpoint. Host
Docker access defaults to `sudo -n docker`. IRD actions use `docker exec -u
<runner.user>` and verify the actual hostname before acting.

| Setting | Meaning |
|---|---|
| `name`, `namespace`, `model_name` | Unique deployment/container prefix, Dynamo namespace, advertised API model |
| `nodes.*.ip`, `hostname`, `ssh` | Routable bare-metal IPv4, literal host hostname, SSH connection |
| `nodes.*.runner` | `ird` or `native`; literal runner hostname and declared grid; IRDs also need current container name, user, bridge IP |
| `paths` | Pinned checkouts, checkpoint, distinct weight caches, JIT directories, shared runtime, optional MPI library directory |
| `ports` | Matching ports across configs, relays, launch plan, and smoke URLs |
| `images`, `pins` | Prebuilt KVM/NATS images on the hosts and exact source commits |
| `timeouts` | Model startup, service startup, graceful stop, and individual HTTP request seconds |

For native runners set `runner.mode` to `native` and `runner.hostname` to the
bare-metal hostname; omit that runner's `container`, `user`, and `ip`. The plan
omits the four IRD relays. The pinned decode requires an actual 13×10 grid;
prefill was verified on 12×10. JSON declares the grid; it does not measure it.
Confirm all eight chips from prior hardware probe output.

Prerequisites:

- Follow the [model/build guide](MODEL_RUNNERS.md):
  stage the checkpoint, build both environments, and prepare geometry-specific
  caches. The controller does not install or compile software.
- Stage the adapter venv at `DGEN/adapters/dynamo/.venv` and etcd at
  `DGEN/.dynamo_local/etcd/etcd` on the decode host. The pinned
  `adapters/dynamo/setup_router_env.sh` provisions this environment; omit
  `--no-etcd` if etcd is missing. Stage real metal/DMK/Mooncake KVM and NATS
  images using the [serving guide](SERVING.md).
- Paths must resolve at the same absolute locations in their consuming
  host/IRD. The runtime must be shared by the controller, both hosts, and both
  runners, including **live** KV exports. Matching static files does not prove
  shared storage. The recipe also uses shared HOME metadata with
  `DYN_SELF_HOST_METADATA=0`; verify that across frontend/workers. The manual
  networking guide covers alternatives for separate storage.
- Reserve the devices and ports. Do not run two stacks against the same model
  shared-memory channels. IRD workers stay inside their model container;
  private IPC cannot be donated to a sibling container.
- Allow host↔host connectivity for fixed ports and Mooncake's dynamic RPC/data
  ports. See [architecture](../../docs/ARCHITECTURE.md). The tools launch user-space IRD
  relays; they do not edit firewall rules or recreate reservation containers.

`render` refuses a nonempty `runtime/generated`. It produces model/worker
configs, KVM env files, a prefill launcher, TCP relay, resolved site, and launch
plan. The recipe's `scripts/render_configs.py --site deployment.local.json` accepts the
same file. Its original individual flags remain available for manual setups.

## Read-only inspection

`preflight` checks access/hostnames, device visibility, pins, Python imports,
generated-file agreement, checkpoint shard presence/header bounds, cache paths,
image revision/metal labels, IRD IPs, and TCP reachability from runner namespaces.
It reports existing model/worker consumers. It does not open accelerators,
reset devices, build, or launch services. Only the optional report writes a
local file; it refuses overwrite.

Before launch, unavailable service ports are warnings. With `--serving` they
are errors. Passing preflight does not establish cache completeness, actual
grid compatibility, shared-mount semantics, image binary correctness, or real
KV transfer. Use the model checks and smoke test as well.

```bash
python3 scripts/disagg.py --site deployment.local.json status
python3 scripts/disagg.py --site deployment.local.json preflight --serving
```

`status` reads ownership records and reports running/stopped/unmanaged state,
separate readiness checks (a live process may still be initializing),
changed configs/table fingerprints, and managers whose runner generation no
longer matches. `unmanaged` means no controller record, not an empty host.
Inspect preflight/live processes for any hand-launched stacks. The current
lb17/lb08 deployment uses its own site and ownership records; see
[its handoff](VALIDATION.md#current-deployment-handoff).

## Start and stop

```bash
python3 scripts/disagg.py --site deployment.local.json start
# Inspect status/logs, then validate before admitting client traffic:
python3 scripts/disagg.py --site deployment.local.json smoke --report-dir smoke-001
# Stop client traffic and let active requests finish before teardown:
python3 scripts/disagg.py --site deployment.local.json stop
```

Startup phases: etcd/NATS → both runners → both KV managers → IRD relays →
workers → frontend. Both managers start before waiting for peer health. Model
readiness requires fresh exports and a readiness log marker. The frontend must
advertise the model. Cold launch can take hours; logs are in `runtime/logs`.

State is under `runtime/control`. Repeating `start` reuses only matching owned
services, configs/table fingerprints, and runner generations. New launches
refuse occupied listeners, foreign containers, and competing model/worker
consumers in the runner namespace. A shared runtime lock serializes lifecycle
operations.

Shutdown reverses dependency order: frontend, workers, relays, managers,
runners, discovery. It checks hostname, boot ID, PID start time, process group,
and Docker ownership labels. It sends SIGTERM (including to owned containers),
or the ready decode ring's stop file, then waits for exit. An initializing
decode runner receives SIGTERM because it cannot consume the stop file yet.
It never issues a device reset, broad `pkill`, or SIGKILL. A timeout,
orphaned group/container, or changed
owner halts teardown before lower dependencies stop. Diagnose that state and
its logs explicitly.

After failed startup, launched services remain recorded; there is no automatic
rollback. Preserve the site and tooling revision for ordered cleanup. Stop
before editing configs, changing IRD identity, or restarting a model. Model
restart invalidates both managers' live-address tables. Use a fresh runtime
for a changed site; keep caches and logs. Processes survive logout; reboot
supervision and automatic recovery are outside this CLI.

## Prove migration

Run `smoke` on an otherwise idle stack so other traffic cannot account for the
deltas. It sends two sequential fresh-code requests: one single-chunk and one
crossing the 5120-token boundary. It requires:

- Exact code answers, `stop` finish reason, and API token counts above the
  disaggregation threshold and within decode capacity.
- At least 36 successful layer migrations per expected chunk.
- Positive source D2H, wire, and destination H2D byte deltas.
- No new transfer/engine error counters. Missing required metrics,
  disappearing counters, and counter resets fail validation.

Destination replication can double H2D bytes; equality is not required. The
new report directory contains requests, responses, raw before/after metrics,
advertised models, and `summary.json`, including failure details. Failure exits
nonzero without restarting or retrying the stack. Review logs for layer/chunk
coverage and fallback behavior using
[validation and recovery](VALIDATION_AND_RECOVERY.md).

## Verify the tools

```bash
python3 -m unittest discover -s tests -v
```

Tests use disposable local processes and synthetic API/metrics fixtures. They
cover ownership, PID reuse, child exit, occupied listeners, config generation,
runner-generation invalidation, and false smoke successes. Live IRD deployment
validation, fixes, and artifact coverage are recorded in
[VALIDATION.md](VALIDATION.md). The original manual procedure is retained in the
[worked deployment](DISAGG_LB17_PREFILL_LB08_DECODE.md).
