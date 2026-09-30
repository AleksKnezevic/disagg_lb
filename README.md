# Mistral Small 4 disaggregation on a LoudBox

Reproducible bring-up assets for Mistral Small 4 on an eight-device Blackhole
P150 LoudBox. This repository records the two independently verified roles:

- an aggregated decode-only worker driven by d-gen; and
- a standalone prefill-only worker on a `2x4` mesh (`SP=2`, `TP=4`).

The prefill worker is the known-good checkpoint for a future two-LoudBox
disaggregated deployment. Cross-host KV migration has **not** been validated yet.

## Repository contents

| Path | Purpose |
|---|---|
| `docs/AGGREGATED_DECODE.md` | One-LoudBox aggregated decode build and launch notes |
| `docs/LOUDBOX_PREFILL_HANDOFF.md` | End-to-end second-machine prefill procedure |
| `configs/prefill/runner_1rank_loudbox_prefill_2x4.yaml` | Verified one-rank LoudBox prefill configuration |
| `scripts/build_prefill_cache_loudbox_2x4.py` | Builds and validates the geometry-specific TTNN weight cache |
| `scripts/test_embedding_load.py` | Fast diagnostic for the LoudBox pinned-memory issue |
| `fixtures/prefill-smoke-trace/metadata.json` | Minimal standalone producer smoke request |
| `configs/decode/dynamo.aggregated.decode.json` | Previously verified aggregated decode-only d-gen configuration |

## Exact source pins

| Repository | Branch | Commit |
|---|---|---|
| `tenstorrent/tt-metal` | detached pin | `2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd` |
| `tenstorrent/tt-blaze` | `sshon/wip-mistral-ring-pdd-rebased-260929` | `1e6fce22043c98a194f1b3bfc23928e40136c4e0` |
| `tenstorrent/tt-d-gen` | `sshon/mistral4-disagg-rebased-260929` | `ba4c33e33752ee19223b2d219c319dd4ed703b8d` |

No source changes to tt-metal, Blaze, or d-gen were required for the verified
standalone roles. The additions here are configuration, cache generation, a
diagnostic, and operational documentation.

## Prefill geometry

- Model: `Mistral-Small-4-119B-2603`, 36 layers
- Hardware: one eight-device Blackhole P150 LoudBox
- Mesh: `2x4`; sequence parallel 2, tensor parallel 4
- Prefill chunk: 5,120 tokens
- KV capacity: 10,240 tokens for one user
- Fabric: 2-D line/line
- Trace and migration: disabled

The two critical LoudBox details are:

1. `TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0` avoids a KMD 2.9.0 hang while
   pinning the roughly 1 GiB file-backed embedding upload.
2. `PREFILL_MAX_SEQ_LEN` must be strictly larger than `PREFILL_CHUNK_SIZE` for
   chunked SDPA; the verified pair is 10,240 and 5,120.

Start with the [aggregated decode notes](docs/AGGREGATED_DECODE.md) or the
[prefill handoff](docs/LOUDBOX_PREFILL_HANDOFF.md). The repository does not
contain checkpoints, TTNN caches, build products, or runtime logs.
