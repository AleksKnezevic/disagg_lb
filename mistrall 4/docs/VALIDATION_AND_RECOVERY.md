# Validation, diagnosis, and restart

## Demonstrate real disaggregation

Check both KVM `/health` endpoints, both workers, the frontend's activated
prefill router, and `/v1/models`. Then capture pre-test KV-manager `/metrics`
snapshots and send requests through the frontend, not directly to the ring.

Use a prompt comfortably above `min_disagg_tokens=256`, below the 8192-token
decode limit including output. A short prompt can exercise a local fallback
instead of migration. `--enforce-disagg` helps ensure frontend routing, but
actual per-layer KV-manager work and bytes are the decisive evidence.

Minimal API shape (replace `FRONTEND_HOST` and model as configured):

```bash
curl -fsS http://FRONTEND_HOST:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data-binary @request.json > response.json
```

Create two useful fixtures with standard Python; the first is fresh content,
the second crosses the prefill chunk boundary in the verified tokenizer:

```python
import json
from pathlib import Path
for name, repetitions, code in [
    ("fresh", 65, "MARIGOLD-7826"),
    ("multichunk", 520, "COBALT-4931"),
]:
    prompt = (
        f"Remember this exact access code: {code}.\n"
        + "This paragraph is padding and does not change the code.\n" * repetitions
        + "\nWhat access code did I give at the beginning? Reply only with that code."
    )
    body = {
        "model": "mistral4", "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64, "temperature": 0.0, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    Path(f"{name}-request.json").write_text(json.dumps(body) + "\n")
```

Change the codes between independent validation runs so old decode KV cannot
accidentally satisfy the check. Confirm actual token counts in API usage and
prefill logs rather than assuming repeated text always tokenizes identically.
The verified run produced 1286 and 6291 prompt tokens respectively, and exact
code answers. The latter executed `[0,5120)` then `[5120,6291)`.

For each test, collect:

- Request ID, prompt/output tokens, returned text, and finish reason. Inspect
  content: HTTP 200 alone does not rule out empty, repetitive, or wrong output.
- Prefill `CHUNK_START` and completion of all layers. Do not manually consume
  the ack channel while d-gen owns it.
- KVM migration completion for every layer/chunk and nonzero payload bytes
  from source device → network → destination device.
- Metric deltas such as `kv_manager_engine_commands_total{kind="migrate",result="ok"}`,
  `kv_manager_bytes_moved_total{leg="wire"}`, device `d2h`/`h2d` bytes, and zero
  transfer-batch failures. Destination replication can make drain bytes exceed
  wire bytes; do not require equal totals without considering the table layout.
- No fallback that bypasses the intended prefill/KV path, and no new fatal,
  timeout, or failed-migration errors in the tested interval.

A previously verified standalone prompt is a useful first smoke but insufficient
alone: resident decode KV might already contain it. The fresh-code and
multi-chunk tests cover new content and the main ring boundary. Full numerical
accuracy, throughput, concurrent slots, and other models need their own tests.
If the repository's test harness produces a turncheck outdir, follow its
`AGENTS.md` decode-review instructions; do not replace content review with an
exit-code check.

## Diagnose the layer that failed

| Symptom | Check / response |
|---|---|
| Decode `Invalid shard grid`, column 12, 120 L1 banks | Actual 12×10 hardware is incompatible with this pinned decode; choose compatible decode hardware or treat a kernel port as separate work |
| Prefill stalls at embedding upload | Confirm pinned-memory cache limit 0 reached the MPI rank; inspect the isolated embedding diagnostic |
| Prefill Q/KV length assertion | Use chunk 5120 and capacity 10240; one-chunk capacity is invalid |
| Decode startup still compiling | Inspect progress through captured images and cache paths; cold launch can take hours |
| H2D/D2H or ack channel attach timeout | Worker must be in the runner's shared-memory environment; check service/socket IDs and competing consumers |
| KVM cannot serve tables / waits for missing host | Compare exported table owners, literal runner hostname, env identity, device map, and export freshness |
| KVM reports mock, missing DMK/metal/Mooncake | Use the real built image/native backend; do not downgrade the test to a CPU stub |
| KVM peer discovery works, payload transfers fail | Inspect Mooncake's actual published RPC/data addresses and reachability; static coordination ports alone are insufficient |
| Worker rendezvous disconnects repeatedly | Check port 19071 relay target, peer endpoint IDs, and both workers' readiness; a one-time startup disconnect can recover |
| Frontend sees no model / metadata 404 at `172.17.0.2` | Verify shared metadata with self-hosting disabled, or fix reachable metadata URLs; duplicate private addresses can hit the wrong IRD |
| NATS connects but responses hang | Inspect advertised TCP response-stream host/port and relay 19101; response traffic may still use TCP |
| Slot/admission or decode stalls | Check one-slot geometry, `pipeline_inflight_cap=1`, unconsumed pages, and failed requests before admitting another |
| KVM “queue has no chunks … shard stays stale” | Check whether that SP shard actually owns tokens in the requested range; an empty non-owning shard is possible, but missing required coverage must be resolved before claiming success |

A health endpoint can remain available while a transfer or model request fails.
If an attempt fails, preserve the request and logs, identify the failing layer,
and correct it before retrying. If the remaining issue is inaccessible host
networking, incompatible hardware, missing checkpoint/access, or unavailable
required dependencies, report that exact blocker and the state of any running
components instead of repeatedly resetting or broadening permissions.

## Restart and teardown order

Record process groups and verify their current command lines before signaling
them; PIDs from the worked example are historical.

1. Stop admitting new client requests; drain and stop d-gen workers with SIGTERM.
   Prefill drain took about 30 seconds in the verified setup. Verify worker
   exit before another worker attaches to the same channels.
2. Stop the KV managers before stopping/restarting model runners. Do not reset
   cards while KVM data movers are active.
3. Stop a prefill launcher's verified process group and confirm all MPI/rank
   children exit. Decode's hand-launched ring stays alive after worker detach;
   use its configured stop file only after workers/KVMs are gone, then wait for
   the ring's drain/exit. Before the ring reaches readiness, that file is not
   consumed; cancel initialization by signaling its verified launcher/process
   group and confirm every rank exits. A quiet log or expired observation
   window alone is not a reason to restart: inspect the live processes and
   capture progress first. Resort to recovery/reset only when required and all
   device owners are accounted for.
4. Restart affected runners, wait for warmup/readiness, and regenerate fresh
   KV tables/maps. Distribute exports if storage is not shared. Recheck hostname
   identity if an IRD was recreated.
5. Restart/reload both KV managers against the current exports and wait for
   healthy discovery. Existing stopped Docker containers can be started again
   when their environment/mounts remain correct; env changes require updating
   their container configuration through the normal deployment lifecycle.
6. Recheck IRD IPs and relay targets. Start workers, then ensure frontend
   registration/prefill routing is active. Re-run a fresh migrated request.

Preserve weight/JIT caches and logs. `setsid`/detached Docker processes survive
the initiating tool/SSH session, but the recipe does not establish reboot
autostart. Add service supervision only if requested. Keep a handoff in the
repo's deployment docs with the current API/model, role mapping, scripts, PIDs,
ports, image/source pins, and tested limits; do not store credentials there.
