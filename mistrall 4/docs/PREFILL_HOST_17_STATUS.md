# Prefill running on bh-lb-17

**Current state, 2026-10-01:** prefill is now connected to decode on `bh-lb-08`.
Migration and KV-table export are enabled. All three disaggregated smoke
requests passed, including a 6,291-token prompt across two prefill chunks.
Use the [disaggregated runbook](DISAGG_LB17_PREFILL_LB08_DECODE.md) for current
configuration, logs, and restart instructions. Current PIDs are recorded in
`/localdev/aknezevic/runtime-mistral4-prefill/status.json` and `runner.pid`.

The historical standalone validation below documents the initial bring-up;
its recorded PIDs and migration-disabled configuration have been superseded.

## Initial standalone validation

Verified 2026-09-30 at 21:43 UTC on
`bh-lb-17-special-aknezevic-for-reservation-238101`.

The standalone Mistral Small 4 prefill runner is active. All 36 layers loaded,
the 5,120-token warmup completed, and the smoke request returned exactly
36 layer acknowledgements. No runtime errors were found. This verifies request
execution, not numerical PCC or cross-host KV migration.

## Configuration

- Eight P150b devices; this host exposes a 12x10 compute grid.
- Mesh 2x4, SP=2, TP=4, one user.
- Chunk size 5,120; KV capacity 10,240 tokens.
- Service ID `ds_prefill`.
- Trace and migration disabled; local MPI interface `lo`.
- `TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0`.
- tt-metal pin `2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd`, clean source tree.

The decode grid incompatibility documented in `DECODE_HOST_17_STATUS.md` does
not prevent this prefill configuration from running.

## Processes and paths

Recorded launcher PID/process group: `77996`. Recorded runner PID: `78013`.
PIDs can change after a restart; confirm command lines before acting on them.

```text
tt-metal:     /localdev/aknezevic/tt-metal
checkpoint:   /localdev/aknezevic/models/Mistral-Small-4-119B-2603
weight cache: /localdev/aknezevic/.cache/mistral4-prefill-ttnn/mistral_small_4_bh_8dev/2x4
JIT cache:    /var/tmp/aknezevic-ttcache-m4-prefill-sp2tp4
runtime:      /localdev/aknezevic/runtime-mistral4-prefill
H2D service:  /dev/shm/tt_h2d_stream_service_ds_prefill.bin
```

The weight cache contains 2,269 tensor files totaling 63.68 GiB. Generation
took about 16 minutes and ended with `CACHE COMPLETE`. Cold runner warmup
reported `runtime.compile() = 44988.91 ms`; readiness followed at 21:42:52 UTC.

Runtime artifacts:

- `runner.log`: live runner log.
- `smoke.log`: producer push/DONE and `layer_acks=36/36`.
- `cache-build.log`, `build.log`: successful cache and build results.
- `status.json`, `runner.pid`: recorded status and launcher PID.
- `env.sh`, `start-runner.sh`, `build-cache.sh`, `smoke.sh`: local commands.

## Current local runner launcher

Inspect:

```bash
tail -f /localdev/aknezevic/runtime-mistral4-prefill/runner-disagg.log
pgrep -af 'prefill_runner|ttrun.py|mpirun.*prefill'
```

The local `start-runner.sh` now invokes the migration-enabled manifest in
`/home/aknezevic/disagg_lb_runtime/current/runner_prefill.yaml`.
`start-runner-standalone.sh` preserves the old standalone launch.
Drain the d-gen workers and stop the KV managers before restarting this runner,
then follow the disaggregated runbook to reload its newly exported addresses.
After confirming the old runner's MPI children exited, launch with:

```bash
cd /localdev/aknezevic
setsid runtime-mistral4-prefill/start-runner.sh \
  > runtime-mistral4-prefill/runner-disagg.log 2>&1 < /dev/null &
echo "$!" > runtime-mistral4-prefill/runner.pid
```

Read the current launcher PID from `runner.pid` and verify its command before
stopping its process group. Never reset devices while a runner or KV manager
is active.

The smoke script consumes the standalone layer-ack channel. Do not run it
once d-gen or a migration manager owns that channel.

## Connected decode

The live endpoint is `http://10.250.36.68:8000/v1`, model `mistral4`.
The setup uses host-networked KV managers, NATS for Dynamo requests/events,
and host-side forwarding for worker coordination. See the disaggregated
runbook for the complete topology.
