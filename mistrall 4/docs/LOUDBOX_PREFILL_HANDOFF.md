# Mistral Small 4 LoudBox prefill handoff

Last verified: 2026-09-30 UTC

## Scope and known-good state

This procedure reproduces the verified **standalone prefill-only** worker on one
eight-device Blackhole P150 LoudBox. It does not enable PDD, KV migration, or a
cross-host d-gen connection.

The verified configuration is Mistral Small 4 (36 layers), a `2x4` device mesh,
`SP=2`, `TP=4`, a 5,120-token chunk, and a 10,240-token KV ring for one user.
Trace and migration are disabled. No tt-metal source changes were required.

Use these exact source pins:

```text
tt-metal  2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd
tt-blaze  sshon/wip-mistral-ring-pdd-rebased-260929
          1e6fce22043c98a194f1b3bfc23928e40136c4e0
tt-d-gen  sshon/mistral4-disagg-rebased-260929
          ba4c33e33752ee19223b2d219c319dd4ed703b8d
```

The Blaze and d-gen pins are recorded for the eventual decode host. Only the
prefill side is covered below.

## 1. Choose paths

Clone this repository and select machine-local paths:

```bash
export M4_ROOT=/localdev/$USER
export M4_ASSETS="$M4_ROOT/disagg_lb/mistrall 4"
export M4_METAL=$M4_ROOT/tt-metal-mistral4-prefill
export M4_MODEL=$M4_ROOT/models/Mistral-Small-4-119B-2603
export M4_CACHE_ROOT=$M4_ROOT/.cache/mistral4-prefill-ttnn
export M4_RUNTIME_CACHE=/var/tmp/$USER-ttcache-m4-prefill-sp2tp4
```

The checked-in runner uses a descriptor path relative to `TT_METAL_HOME`, so it
does not require editing when the checkout root changes.

## 2. Check hardware and existing workers

Confirm that no worker owns the cards:

```bash
pgrep -af 'prefill_runner|tools.mistral4_reload|tt_dynamo.main|mpirun'
```

If the machine is reserved for this job and the list is empty, reset all eight
devices with `tt-smi -r`. Never reset a LoudBox while another workload owns it.

## 3. Build tt-metal

```bash
git clone git@github.com:tenstorrent/tt-metal.git "$M4_METAL"
cd "$M4_METAL"
git fetch origin 2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd
git checkout --detach 2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd
git submodule update --init --recursive
./build_metal.sh --enable-ccache
./create_venv.sh
```

The observed build took about seven minutes. Verify that `git rev-parse HEAD`
matches the pin and that the checkout is clean.

## 4. Stage the checkpoint

Place the complete checkpoint at `$M4_MODEL`. It needs `config.json`, tokenizer
files, `model.safetensors.index.json`, and all three resolved safetensor shards.
On the verified host, the followed shard sizes were approximately 49.1 GB,
49.1 GB, and 22.7 GB. Verify symlinks before cache generation.

## 5. Validate the checked-in assets

```bash
cd "$M4_METAL"
source python_env/bin/activate
python -m py_compile \
  "$M4_ASSETS/scripts/build_prefill_cache_loudbox_2x4.py" \
  "$M4_ASSETS/scripts/test_embedding_load.py"

python - <<'PY'
import os, yaml
path = os.path.join(os.environ["M4_ASSETS"], "configs/prefill/runner_1rank_loudbox_prefill_2x4.yaml")
with open(path) as handle:
    cfg = yaml.safe_load(handle)
env = cfg["global_env"]
assert env["PREFILL_SP"] == "2" and env["PREFILL_TP"] == "4"
assert env["PREFILL_CHUNK_SIZE"] == "5120"
assert env["PREFILL_MAX_SEQ_LEN"] == "10240"
assert env["TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES"] == "0"
print("runner config valid")
PY
```

## 6. Populate or copy the 2x4 cache

The cache is geometry-specific. Do not use a `4x2` cache with this runner.

The fast path is to copy the complete verified directory:

```text
.cache/mistral4-prefill-ttnn/mistral_small_4_bh_8dev/2x4
```

It contains 2,269 files and occupies about 64 GB. To rebuild it:

```bash
cd "$M4_METAL"
source python_env/bin/activate
mkdir -p "$M4_CACHE_ROOT" "$M4_RUNTIME_CACHE"

TT_METAL_SLOW_DISPATCH_MODE=1 \
TT_METAL_CACHE="$M4_RUNTIME_CACHE" \
python "$M4_ASSETS/scripts/build_prefill_cache_loudbox_2x4.py" \
  --model "$M4_MODEL" \
  --cache-root "$M4_CACHE_ROOT" \
  --mesh-descriptor "$M4_METAL/tt_metal/fabric/mesh_graph_descriptors/p150_x8_mesh_graph_descriptor.textproto"
```

The expected last line begins `CACHE COMPLETE:`. Cache generation took 14m52s
on the verified machine. The builder's 5,120-token sequence only drives weight
serialization; the runtime creates its 10,240-token KV and RoPE state at launch.

## 7. Optional embedding diagnostic

This isolates the pinned-memory failure without constructing the full model:

```bash
cd "$M4_METAL"
source python_env/bin/activate

TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0 \
PREFILL_FABRIC_MODE=2d \
TT_MESH_GRAPH_DESC_PATH="$M4_METAL/tt_metal/fabric/mesh_graph_descriptors/p150_x8_mesh_graph_descriptor.textproto" \
python "$M4_ASSETS/scripts/test_embedding_load.py" \
  --sp 2 --tp 4 \
  --cache-root "$M4_CACHE_ROOT" \
  --model "$M4_MODEL"
```

Expect `EMBED LOAD COMPLETE`. The isolated load took roughly 0.05 seconds. If it
stalls after `EMBED LOAD START`, confirm that the pinned-memory setting reached
the worker environment.

## 8. Launch standalone prefill

```bash
mkdir -p "$M4_RUNTIME_CACHE"

setsid bash -lc "
  cd '$M4_METAL'
  source python_env/bin/activate
  export MISTRAL4_HF_MODEL='$M4_MODEL'
  export PREFILL_TTNN_CACHE='$M4_CACHE_ROOT'
  export PP_TT_METAL_CACHE='$M4_RUNTIME_CACHE'
  exec models/demos/common/prefill/runners/run_pipeline_prefill.sh \
    '$M4_ASSETS/configs/prefill/runner_1rank_loudbox_prefill_2x4.yaml' \
    localhost:1 lo
" > "$M4_ROOT/prefill-runner-sp2tp4.log" 2>&1 < /dev/null &

echo "launcher PID: $!"
tail -f "$M4_ROOT/prefill-runner-sp2tp4.log"
```

Expected milestones include construction of all 36 layers, one 5,120-token
warmup, creation of `/dev/shm/tt_h2d_stream_service_ds_prefill.bin`, and
`setup complete, entering request loop`. A warm launch took about 39 seconds:
roughly five seconds for construction and 25.9 seconds for warmup.

## 9. Send a standalone smoke request

Prefill populates KV state; it does not generate response text. This fixture has
11 real tokens and pads the rest of the 5,120-token chunk.

```bash
cd "$M4_METAL"
source python_env/bin/activate

TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0 \
PREFILL_MODEL=mistral_small_4 \
MISTRAL4_HF_MODEL="$M4_MODEL" \
PREFILL_HF_MODEL="$M4_MODEL" \
PREFILL_SP=2 PREFILL_TP=4 \
PREFILL_CHUNK_SIZE=5120 PREFILL_MAX_SEQ_LEN=10240 \
PREFILL_NUM_LAYERS=36 PREFILL_NUM_USERS=1 \
PREFILL_H2D_SERVICE_ID=ds_prefill \
PREFILL_H2D_CONNECT_TIMEOUT=60 \
PREFILL_PRODUCER_CHUNKS=1 \
PREFILL_PRODUCER_MAX_REQUESTS=1 \
PREFILL_PRODUCER_CHECK_PCC=0 \
PREFILL_PRODUCER_SLOT_TRACES="$M4_ASSETS/fixtures/prefill-smoke-trace" \
python -m models.demos.common.prefill.runners.prefill_producer
```

Expect a producer `push` followed by `DONE`, and a runner `CHUNK_START`. The
runner does not emit `CHUNK_END` with per-chunk synchronization disabled. For a
standalone check, drain exactly 36 layer acknowledgements:

```bash
python - <<'PY'
import time
import ttnn

channel = ttnn.InterProcessCounterChannel.connect(
    "/tt_prefill_layer_acks_ds_prefill", connect_timeout_ms=5000
)
start = time.perf_counter()
count = 0
while count < 36 and time.perf_counter() - start < 30:
    count += channel.try_consume_all()
    if count < 36:
        time.sleep(0.01)
print(f"layer_acks={count}/36 wait_s={time.perf_counter() - start:.3f}")
assert count == 36
PY
```

Do not consume that channel after d-gen or a migration manager owns it.

## 10. Health, shutdown, and recovery

```bash
pgrep -af 'prefill_runner|ttrun.py|mpirun.*prefill'
ls -lh /dev/shm/tt_h2d_stream_service_ds_prefill.bin
rg -n 'Traceback|RuntimeError|TT_FATAL|FATAL|ERROR|Exception' \
  "$M4_ROOT/prefill-runner-sp2tp4.log"
```

Stop the recorded launcher process group with `kill -TERM -- -<launcher-pid>`.
Verify that MPI children exited. After an unclean exit, reset with `tt-smi -r`
only after confirming no other workload owns the cards.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Hangs before `Building layer 0/36` | KMD/IOMMU pinned upload of the embedding | Ensure `TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0` reaches the rank |
| `Q.seq < K.seq` or equal local Q/KV lengths | KV ring is only one chunk | Use max sequence 10,240 with chunk 5,120 |
| Cache path ends in `4x2` | Wrong geometry cache | Build/copy `mistral_small_4_bh_8dev/2x4` |
| Torus topology connectivity failure | Fabric mode fell back to torus | Set `PREFILL_FABRIC_MODE=2d` |
| Producer exits immediately after push | Its barrier only confirms H2D delivery | Check the runner and the 36 layer acknowledgements |

## Next step: two LoudBoxes

Keep this prefill role unchanged as the baseline, bring up the pinned aggregated
decode role on the second host, choose routable data interfaces and IPs, and only
then enable KV migration. Loopback is correct only for this single-host launch.
Cross-host migration-table exchange and the end-to-end d-gen request path remain
unvalidated; treat both standalone smoke tests as prerequisites.
