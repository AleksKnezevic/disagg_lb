# Mistral Small 4 disaggregation on a LoudBox

Reproducible bring-up assets for Mistral Small 4 on an eight-device Blackhole
P150 LoudBox. This recipe records the two roles:

- an aggregated decode-only worker driven by d-gen; and
- a standalone prefill-only worker on a `2x4` mesh (`SP=2`, `TP=4`).

Cross-host disaggregated serving was verified on 2026-10-01 with prefill on
`bh-lb-17` and decode on `bh-lb-08`, including fresh requests spanning two
prefill chunks. See [deployment validation](docs/VALIDATION.md) for the
managed deployment, endpoint, and lifecycle test evidence.

## Recipe contents

Paths below are relative to `mistrall 4/`. Run recipe commands from this folder.
Keep `paths.repo` / `DISAGG_REPO` pointing to the top-level `disagg_lb` checkout.

| Path | Purpose |
|---|---|
| `../.agents/skills/setup-disagg/SKILL.md` | Agent skill for bare-metal/IRD disaggregated setup, launch, networking, migration, and recovery |
| `configs/deployment.example.json` | Portable hosts, IRDs, paths, ports, images, and pins |
| `scripts/disagg.py` | Render, read-only preflight, owned-process start/status/stop, and migration smoke tests |
| `docs/OPERATIONS.md` | CLI workflow, prerequisites, ownership, and validation evidence |
| `docs/VALIDATION.md` | Live teardown/bring-up results, script coverage, and fixes found on hardware |
| `../docs/ARCHITECTURE.md` | Request/KV paths, IRD boundaries, and port map |
| `tests/test_disagg_tools.py` | Offline lifecycle/config/validation regression tests |
| `docs/DISAGG_LB17_PREFILL_LB08_DECODE.md` | Historical manual deployment and its validation evidence |
| `docs/AGGREGATED_DECODE.md` | One-LoudBox aggregated decode build and launch notes |
| `docs/DECODE_HOST_17_STATUS.md` | bh-lb-17 build results and the 12x10 compute-grid blocker |
| `docs/PREFILL_HOST_17_STATUS.md` | Running bh-lb-17 prefill worker, smoke result, and local paths |
| `docs/LOUDBOX_PREFILL_HANDOFF.md` | End-to-end second-machine prefill procedure |
| `configs/prefill/runner_1rank_loudbox_prefill_2x4.yaml` | Verified one-rank LoudBox prefill configuration |
| `scripts/build_prefill_cache_loudbox_2x4.py` | Builds and validates the geometry-specific TTNN weight cache |
| `scripts/test_embedding_load.py` | Fast diagnostic for the LoudBox pinned-memory issue |
| `fixtures/prefill-smoke-trace/metadata.json` | Minimal standalone producer smoke request |
| `configs/decode/dynamo.aggregated.decode.json` | Previously verified aggregated decode-only d-gen configuration |

## Agent workflow

Use the repo-local [setup-disagg skill](../.agents/skills/setup-disagg/SKILL.md):

```text
Use $setup-disagg to set up disaggregated serving on my prefill and decode hosts.
```

Agents without automatic skill discovery can read that `SKILL.md` directly.
It covers native bare metal and IRD reservation containers, the verified
Mistral Small 4 LoudBox geometry, model builds/launch, host networking and
forwarding, real KV managers, Dynamo serving, validation, and recovery. The recipe
includes `scripts/render_configs.py`; the skill provides a shared TCP relay
helper. Generating configs does not launch services or change the current
deployment.

For a new deployment, enter `mistrall 4/`, then copy `configs/deployment.example.json` to
`deployment.local.json`, fill in the reservation, and use
`python3 scripts/disagg.py --site deployment.local.json render`, then
`preflight`. The CLI also provides `plan`, `start`, `status`, `smoke`, and `stop`.
Read [operations](docs/OPERATIONS.md) before launch; lifecycle commands manage
only their own processes and do not adopt hand-launched services. The
[architecture diagram](../docs/ARCHITECTURE.md) shows the required connections.

Detailed manual procedures: [model builds and runners](docs/MODEL_RUNNERS.md),
[serving](docs/SERVING.md), and [validation/recovery](docs/VALIDATION_AND_RECOVERY.md).

## Exact source pins

| Repository | Branch | Commit |
|---|---|---|
| `tenstorrent/tt-metal` | detached pin | `2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd` |
| `tenstorrent/tt-blaze` | `sshon/wip-mistral-ring-pdd-rebased-260929` | `1e6fce22043c98a194f1b3bfc23928e40136c4e0` |
| `tenstorrent/tt-d-gen` | `sshon/mistral4-disagg-rebased-260929` | `ba4c33e33752ee19223b2d219c319dd4ed703b8d` |

No source changes to tt-metal, Blaze, or d-gen were required for the verified
standalone or disaggregated roles. The additions here are configuration,
cache generation, lifecycle and validation tools, and operational documentation.

## Prefill geometry

- Model: `Mistral-Small-4-119B-2603`, 36 layers
- Hardware: one eight-device Blackhole P150 LoudBox
- Mesh: `2x4`; sequence parallel 2, tensor parallel 4
- Prefill chunk: 5,120 tokens
- KV capacity: 10,240 tokens for one user
- Fabric: 2-D line/line
- Trace: disabled; migration disabled in the standalone manifest and enabled in the live disaggregated manifest

The two critical LoudBox details are:

1. `TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0` avoids a KMD 2.9.0 hang while
   pinning the roughly 1 GiB file-backed embedding upload.
2. `PREFILL_MAX_SEQ_LEN` must be strictly larger than `PREFILL_CHUNK_SIZE` for
   chunked SDPA; the verified pair is 10,240 and 5,120.

Start with the [aggregated decode notes](docs/AGGREGATED_DECODE.md) or the
[prefill handoff](docs/LOUDBOX_PREFILL_HANDOFF.md). The repository does not
contain checkpoints, TTNN caches, build products, or runtime logs.
