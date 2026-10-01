# Decode bring-up on bh-lb-17

Verified 2026-09-30 UTC on
`bh-lb-17-special-aknezevic-for-reservation-238101`.

This records the earlier decode attempt. The host was subsequently assigned
to prefill; see [the active prefill status](PREFILL_HOST_17_STATUS.md). The
top-level tt-metal checkout is now built at the prefill pin, and the prefill
runner owns the devices.

## Result

The decode runner is **not running**. Startup failed before weight loading
because all eight P150b devices expose a `12x10` compute grid. The pinned
Mistral implementation requires `13x10`.

The first failure was:

```text
TT_FATAL: Invalid shard grid: shard core (12, 8) has no L1 bank on this device, which has 120 of them.
```

Both MPI ranks exited. No device owners remained. The temporary local Dynamo
frontend and etcd were stopped after the failed bring-up. No model source
patches were made, and generation was not tested.

## Evidence

Runtime artifacts are under `/localdev/aknezevic/runtime-mistral4`:

- `device-inventory.log`: eight P150b cards.
- `cluster.yaml`: every chip reports Tensix harvesting mask `192` (`0xc0`).
- `link-check.log`: successful eight-device mesh open/close and `links ok`,
  with the compute grid reported as `(12, 10)`.
- `ring.log`: the failed ring launch and complete exception traces.
- `blaze-install.log`, `dgen-build.log`: successful builds.

The pinned Blaze config hardcodes `GRID_COLS = 13`, sender `(12, 9)`,
router cores in column 12, and a 128-core L1 LM-head grid. The pipeline's
default socket pool also uses column 12. The existing DRAM-streaming LM-head
option alone does not resolve the other placements. Supporting this host
requires a coordinated kernel/weight-layout port and generation validation,
or a decode host with a compatible compute grid. Do not bypass device
harvesting validation.

## Prepared environment

```text
tt-d-gen: /localdev/aknezevic/tt-d-gen
  ba4c33e33752ee19223b2d219c319dd4ed703b8d
tt-blaze: /localdev/aknezevic/tt-d-gen/third_party/tt-blaze
  1e6fce22043c98a194f1b3bfc23928e40136c4e0
tt-metal: /localdev/aknezevic/tt-d-gen/third_party/tt-blaze/tt-metal
  cc834df1b8baa458c4b40559c9c2242edec541bb
  (the nested Blaze submodule pin used by the decode build procedure)
```

The separate prefill tt-metal pin `2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd`
was fetched for inspection, not built or checked out. The top-level
`/localdev/aknezevic/tt-metal` and `tt-blaze` checkouts were left unchanged.

The model is staged at
`/localdev/aknezevic/models/Mistral-Small-4-119B-2603`, from Hugging Face
revision `a11f36bebf709121056b1dbcc943d1c6afbe494d`. All 1,485 indexed tensor
headers were validated across the three shards (49,078,433,296;
49,132,710,896; and 22,711,829,744 bytes).

Blaze's Python environment and d-gen's Dynamo Python 3.12 environment are
built. Both native d-gen modules import successfully. System libzmq development
packages and iproute2 were installed. UV data is stored under
`/localdev/aknezevic/.cache` because the home-directory UV location was unusable.

Saved scripts in `runtime-mistral4`:

- `env.sh`: local paths and baseline geometry.
- `start-ring.sh`: two 2x2 stages, two layers per visit, one slot, 8,192 tokens.
- `smoke-ring.sh`: exclusive direct-ring generation check; not yet run.
- `start-worker.sh`: aggregated decode worker; not yet run.

The model weight cache is still empty. Initial fabric firmware compilation
populated `/var/tmp/aknezevic-ttcache-m4`.

Only container address `172.17.0.2` and loopback are visible. Remote prefill
address, routable host networking, KV Manager launch, and cross-host migration
remain unconfigured. The local frontend was verified at `127.0.0.1:8000`
with an empty model list before shutdown; this was not a serving validation.
