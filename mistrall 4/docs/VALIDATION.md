# Deployment and script validation — 2026-10-01

The deployment was rebuilt using the repository CLI on two LoudBoxes:
prefill on `bh-lb-17`, decode on `bh-lb-08`, with both models/workers in their
existing IRDs. KV managers and TCP relays run on the hosts. No reservation
containers were recreated, and no device resets or model SIGKILL were needed.

**Validation passed:** managed bring-up, serving tests, ordered shutdown of all
13 services, full restart, repeated-start idempotency, and post-restart serving
checks. The deployment was left running with all 13 services ready.

The managed namespace is `m4revalidated`, served model `mistral4`. The API uses
`http://10.250.36.68:8000/v1`. Capacity remains one concurrent request and
8192 total prompt/output tokens. This deployment supersedes the manual
`m4disagg` process inventory in the [initial worked example](DISAGG_LB17_PREFILL_LB08_DECODE.md).

## Checks performed

| Script / command | Evidence |
|---|---|
| `render_configs.py`, both `--site` and individual flags | Both generated worker configurations accepted by the pinned `tt_dynamo.config.load` parser |
| `disagg.py render`, `plan` and config helpers | Real site rendered, dependency plan inspected, portable env paths and separate decode checkout exercised |
| `disagg.py preflight` | All four host/runner namespaces checked; no failures after correcting the probe issues below |
| `preflight --serving` | Correctly failed while endpoints were absent; passed on the complete managed stack |
| `disagg_remote.py`, `disagg_agent.py` | Real SSH/Docker-exec RPC, owned process launch, readiness checks, identity verification, and shutdown |
| `disagg.py status` | Distinguished a live initializing decode runner from ready services; reported every service stopped after teardown |
| `disagg.py start` | Started all 13 services, restarted both runners with new process generations, and preserved every PID on a repeated start; refused a competing runner while the cache diagnostic owned the cards |
| `disagg.py stop` | Canceled initializing runners cleanly; later stopped all 13 serving components in reverse dependency order |
| `disagg_smoke.py` / `smoke` | Correct single- and multi-chunk answers with actual KV-manager migration and byte deltas |
| `tcp_forward.py` | Used for live rendezvous/response traffic; also passed a 133,000-byte localhost response-after-half-close regression |
| `test_embedding_load.py` | Loaded the cached embedding on the real eight-device prefill mesh; reported completion in 0.048 seconds |
| `build_prefill_cache_loudbox_2x4.py` | Processed all 36 layers against the existing cache and passed `CACHE COMPLETE` |
| Offline regressions | 29 tests passed, including stale tables/generations, PID reuse, occupied listeners, shutdown failure containment, and false smoke successes |
| Skill and docs | Skill validator, Python compilation, shell/Python example syntax, and local links checked |

## Current deployment handoff

These locations identify this reservation only; reusable scripts and the skill
obtain them from configuration:

- Repository: `/localdev/aknezevic/disagg_lb`.
- Site: `/home/aknezevic/disagg_lb_runtime/revalidated-site.json`.
- Runtime, logs, generated configs, and ownership records:
  `/home/aknezevic/disagg_lb_runtime/revalidated/`.
- Prefill IRD: `bh-lb-17-special-aknezevic-for-reservation-238101`.
- Decode IRD: `bh-lb-08-special-aknezevic-for-reservation-237092`.

From the repository root, enter `mistrall 4/` and use the same site to inspect
or control this deployment. The directory move preserves the generated plan
and running service ownership records:

```bash
cd "mistrall 4"
SITE="$HOME/disagg_lb_runtime/revalidated-site.json"
python3 scripts/disagg.py --site "$SITE" status
python3 scripts/disagg.py --site "$SITE" preflight --serving
# For an intentional lifecycle operation:
python3 scripts/disagg.py --site "$SITE" stop
python3 scripts/disagg.py --site "$SITE" start
```

The site uses authenticated SSH ControlMaster sockets. Re-establish SSH access
or update its `ssh.options` when those sessions expire; the serving processes
continue independently. The processes survive logout but are not configured
for automatic restart after reboot. Preserve `runtime/control` for ownership
checks and shutdown. Decode initialization took approximately 23 minutes with
warm caches; follow capture progress in `logs/decode-model.log`.

Clients on the host network can use `http://10.250.36.68:8000/v1`, model
`mistral4`. If that address is unreachable locally, open a tunnel to the
**bare-metal** endpoint:

```bash
ssh -N -L 18000:127.0.0.1:8000 YOUR_USER@bh-lb-08 -p 22
# In another local terminal:
curl -fsS http://127.0.0.1:18000/v1/models
```

Use `http://127.0.0.1:18000/v1` as the client base URL through that tunnel.
The IRD SSH endpoint does not share the frontend's loopback listener.

## First managed serving result

Each request used a new random code at the beginning of the prompt and required
the exact code back, with a `stop` finish reason. Both passed. There were no new
transfer/engine failure counters during either test.

| Test | Prompt tokens | Successful layer migrations | Source / wire bytes | Destination H2D bytes |
|---|---:|---:|---:|---:|
| Fresh single chunk | 1290 | 36 | 15,667,200 | 31,334,400 |
| Multi-chunk | 6295 | 72 | 70,502,400 | 141,004,800 |

## Final post-restart result

The managed stop left every service stopped. The subsequent start created new
runner process generations and fresh live tables. Repeating `start` changed no
service PIDs. Final serving preflight passed, and status reported all 13
services ready without changed-config or stale-table/runner flags.

Both post-restart requests returned their exact new random codes with a `stop`
finish reason. Both KV managers remained healthy, with zero new transfer or
engine failure counters.

| Test | Prompt tokens | Successful layer migrations | Source / wire bytes | Destination H2D bytes |
|---|---:|---:|---:|---:|
| Fresh single chunk | 1292 | 36 | 15,667,200 | 31,334,400 |
| Multi-chunk | 6295 | 72 | 70,502,400 | 141,004,800 |

Destination replication accounts for the larger H2D totals. These checks prove
functional serving and migration on this deployment; they do not measure full
model accuracy or concurrent throughput. Hardware lifecycle tests used IRDs;
native-runner configuration and relay omission were covered by offline tests,
not a separate native hardware deployment. The cache helper validated the
existing geometry-specific cache; this was not a cold checkpoint-to-cache build.

## Fixes found by live testing

- Device discovery now counts numeric character devices, excluding the
  `by-id` metadata directory.
- Preflight checks the dependencies actually used by each environment. It
  avoids importing prefill TTNN merely to inspect availability, since that
  import can create profiler directories, and does not require Torch in the
  lightweight worker environment.
- Docker image labels may be absent; missing-container errors vary in case.
  Both cases are now handled without treating permission errors as absence.
- `paths.decode_blaze` supports a built decode checkout outside the d-gen
  submodule. This value is propagated to the real runner.
- The decode launcher now disables the pinned-memory cache, as prefill already
  did. A launch without that setting stalled during the last capture with high
  kernel CPU; the retry with it enabled completed all 19 images and served the
  migrated requests above. This is consistent with the known pinned-memory
  issue; a kernel stack identifying the precise cause was not obtained.
- Initializing decode runners receive SIGTERM when stopped. Their stop file
  is consumed only after initialization reaches the serving loop.
- The native frontend clears worker-specific response-stream environment
  settings. With an automatic port, passing an IP as `DYN_TCP_RESPONSE_STREAM_HOST`
  caused `Interface not found`; native interface discovery materialized the
  model and activated the prefill router successfully.
- Status reports readiness separately from process liveness.

## Portability and evidence

Reusable scripts, skill files, and the deployment template were audited for
reservation-specific absolute paths and the supplied credentials. Paths now
come from the site or `${DISAGG_*}` environment variables; unresolved variables
are rejected. Linux `/proc` and `/dev` references describe kernel interfaces,
not a user's checkout or reservation. Passwords are absent from these assets
and from the deployment configuration. Authentication used existing SSH
ControlMaster connections.

Artifacts reside under the configured `paths.runtime`, outside Git:

- `preflight-ready.json`, `preflight-incomplete.json`, `preflight-first.json`,
  `preflight-final.json`
- `smoke-before-teardown/`, `smoke-first/`, `smoke-final/` — requests, responses, raw metrics,
  and summaries
- `teardown-legacy.log`, `stop-initializing.log`, `managed-stop.log`,
  `managed-restart.log`, `idempotent-start.log`
- `status-after-cancel.json`, `status-first-ready.json`, `status-stopped.json`
- `status-second-ready.json`, `status-after-idempotent.json`, `status-final.json`
- `embedding-validation.log`, `cache-validation.log`, `unit-tests.log`
- `decode-pin-env-proof.json`, `decode-pin-env-final.json`, `shared-fresh-table-proof.json`
- `portability-audit.json` — file hashes and audit scope, without credentials
- `source-validation.json`, `validation-summary.json`, `validation-stage.json`

Use the [operations guide](OPERATIONS.md) for portable commands. Keep the
reservation's site JSON outside version control and use that same site for
status and teardown.
