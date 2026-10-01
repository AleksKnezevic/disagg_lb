# Model runners and LoudBox preparation

Read this for new hosts, missing builds, or a runner that must be restarted.
For compatible running roles, collect the same evidence without repeating
hardware probes, cache generation, or model launch.

## Inventory and environments

Determine device ownership both on the bare-metal host and inside IRDs. Record
runner/worker/KVM PIDs, Docker containers, current logs, KMD/firmware versions,
and eight visible Tenstorrent devices. On bare metal, `docker ps` is needed
even if the proposed new model process is native: containers can own the cards.

The pinned decode requires a **13×10 worker grid on every participating chip**.
The 12×10 lb17 hardware ran prefill successfully but could not run this decode
layout: its LM head needs 128 cores and socket/router placement uses column 12.
Use existing runtime topology logs or an idle-device mesh probe to establish
the actual grid. Do not run mesh-open/reset diagnostics while another workload
owns the devices. Do not work around incompatible harvesting by editing masks.

On a fresh idle box, the mesh/link check and build procedure are in the repo's
[aggregated decode guide](AGGREGATED_DECODE.md). Successful
links do not prove the model's compute-grid requirements are met.

Set and persist site variables, replacing paths with the actual checkouts.
`RUN`/`CFG`/`KV_TABLE_DIR` below should be shared or explicitly replicated where
needed; weights and build/JIT caches can remain machine-local.

```bash
# Set these paths for your deployment (or take them from the resolved site JSON).
: "${ASSETS:?set the disagg_lb checkout}"
: "${DGEN:?set the pinned tt-d-gen checkout}"
: "${PF_METAL:?set the pinned prefill tt-metal checkout}"
: "${MODEL:?set the checkpoint directory}"
: "${PF_CACHE:?set the prefill cache root}"
: "${DECODE_CACHE:?set the decode cache root}"
: "${PF_JIT:?set the prefill JIT cache}"
: "${DECODE_JIT:?set the decode JIT cache}"
: "${RUN:?set the shared runtime directory}"
export ASSETS DGEN PF_METAL MODEL PF_CACHE DECODE_CACHE PF_JIT DECODE_JIT RUN
export SKILL="$ASSETS/.agents/skills/setup-disagg"
export RECIPE="$ASSETS/mistrall 4"
export BLAZE="$DGEN/third_party/tt-blaze"
export CFG="$RUN/config"
export KV_TABLE_DIR="$RUN/kv_tables"
mkdir -p "$RUN/logs" "$KV_TABLE_DIR"
```

Record `PREFILL_IP`, `DECODE_IP`, each runner's literal `hostname`, and any IRD
container name/IP separately. Export `ETCD_ENDPOINT=http://<discovery-host>:2379`
and `NATS_ENDPOINT=nats://<broker-host>:4222` once those locations are chosen.
Do not use shell variable names such as `HOME` to store task-specific paths.

## Pins and builds

| Component | Verified commit / version |
|---|---|
| Separate prefill tt-metal | `2ff8a0ca3d1cfef84dbe3e9e90631166ef0861bd` |
| tt-d-gen | `ba4c33e33752ee19223b2d219c319dd4ed703b8d` |
| Blaze, normally d-gen's `third_party/tt-blaze` | `1e6fce22043c98a194f1b3bfc23928e40136c4e0` |
| Blaze's nested tt-metal | `cc834df1b8baa458c4b40559c9c2242edec541bb` |
| Dynamo adapter runtime | `ai-dynamo==1.5.0`, Python ≥3.12 |
| Checkpoint used in the verified deployment | HF revision `a11f36bebf709121056b1dbcc943d1c6afbe494d` |

Reuse compatible clean checkouts. For missing ones, clone the named repositories,
fetch/checkout these commits and initialize their recursive submodules. Do not
overwrite someone else's working tree or substitute a branch's latest HEAD.

Prefill build, in its own checkout:

```bash
cd "$PF_METAL"
./build_metal.sh --enable-ccache
./create_venv.sh
```

Blaze and d-gen build, in **each worker's execution environment**:

```bash
cd "$BLAZE"
UV_LINK_MODE=copy ./install.sh
cd "$DGEN"
./adapters/dynamo/setup_router_env.sh --no-etcd
source adapters/dynamo/.venv/bin/activate
./build_dgen.sh --dynamo --blaze --skip-blaze-build --build-dir build-bindings \
  --target _tt_engine --target _tt_dynamo_kv --jobs 16
```

Builds need native system dependencies (including libzmq development files),
network access for dependencies, and the appropriate compiler/MPI toolchain.
Use the pinned build script's dependency checks. `--skip-blaze-build` assumes
the preceding Blaze build succeeded. Do not use `SKIP_SYSDEPS=1` to hide missing
dependencies; it is appropriate for an already-installed custom prefix.

Keep prefill's `python_env` distinct from the adapter's `.venv`. The adapter
links the Blaze build of tt-metal, even on the prefill host. A top-level Blaze
checkout can run the ring if pinned identically, but d-gen's build script uses
its own nested submodule. Check both instead of assuming they are the same.
On IRD, a host-side frontend may run the bind-mounted Python environment only
after verifying its interpreter and libraries are available on the host.

Check the MPI launcher and shared-library paths independently. The verified
decode reservation needed OpenMPI 5.0.7 with ULFM; its `mpirun` wrapper selected
the installed prefix, exported matching `OPAL_PREFIX`/`LD_LIBRARY_PATH`, and
added `--with-ft ulfm`. Inspect `command -v mpirun`, `mpirun --version`, the
wrapper selected by Blaze's `env.sh`, and linked libraries. Repair an actually
missing/mismatched launcher using the installed toolchain; do not copy another
reservation's absolute MPI path or replace a working system MPI globally.

If home-directory UV installs fail, place `UV_CACHE_DIR` and
`UV_PYTHON_INSTALL_DIR` under writable machine-local storage and use
`UV_LINK_MODE=copy`. Preserve those paths when reusing the virtualenv.

## Checkpoint and caches

Verify `config.json`, tokenizer assets, index, and all referenced safetensor
shards. The verified checkpoint has three shards totaling about 121 GB; plan
additional disk for both geometry-specific caches and builds. Check symlink
targets, not just directory entries.

The complete prefill cache is `mistral_small_4_bh_8dev/2x4`, 2,269 files,
about 64 GiB. A `4x2` cache is not interchangeable. Follow the repo's
[prefill handoff](LOUDBOX_PREFILL_HANDOFF.md) for full cache
generation and optional embedding diagnosis. The actual builder invocation is:

```bash
cd "$PF_METAL"
source python_env/bin/activate
TT_METAL_SLOW_DISPATCH_MODE=1 TT_METAL_CACHE="$PF_JIT" \
  python "$RECIPE/scripts/build_prefill_cache_loudbox_2x4.py" \
  --model "$MODEL" --cache-root "$PF_CACHE" \
  --mesh-descriptor "$PF_METAL/tt_metal/fabric/mesh_graph_descriptors/p150_x8_mesh_graph_descriptor.textproto"
```

Expect `CACHE COMPLETE`. Preserve decode's weight and JIT caches too. A cold
decode launch previously took about 1h52m; watch compilation/capture progress
instead of treating a quiet interval as readiness or resetting mid-compile.

## Generate the migration configuration

After recording each runner's hostname (native hostname or IRD hostname), run:

```bash
python3 "$RECIPE/scripts/render_configs.py" \
  --dgen "$DGEN" --prefill-ip "$PREFILL_IP" --decode-ip "$DECODE_IP" \
  --prefill-runner-hostname "$PREFILL_RUNNER_HOSTNAME" \
  --decode-runner-hostname "$DECODE_RUNNER_HOSTNAME" \
  --table-dir "$KV_TABLE_DIR" --etcd-endpoint "$ETCD_ENDPOINT" --output "$CFG"
```

Use a fresh output directory. Review `deployment.json`, both worker JSON files,
both KVM env files, and `runner-prefill.yaml`. The script loads the pinned d-gen
templates and the repo's working LoudBox prefill manifest; it starts nothing.

## Launch prefill in its assigned host or IRD

The manifest is SP=2/TP=4 on 2×4 devices, chunk 5120, capacity 10240, one user,
2-D fabric, trace off, `GPT_DEVICE` gate fallback, capacity factor 8. Preserve
`TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0`: it avoids the observed hang while
pinning the large embedding upload. The KV ring must be larger than one chunk;
do not lower it to decode's 8192-token capacity.

```bash
cd "$PF_METAL"
source python_env/bin/activate
export MISTRAL4_HF_MODEL="$MODEL" PREFILL_TTNN_CACHE="$PF_CACHE"
export PP_TT_METAL_CACHE="$PF_JIT"
setsid models/demos/common/prefill/runners/run_pipeline_prefill.sh \
  "$CFG/runner-prefill.yaml" localhost:1 lo \
  > "$RUN/logs/prefill-runner.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/prefill-runner.pid"
```

`lo` is correct for this single-machine MPI launch; it is not the KV transfer
interface. The manifest, not merely the parent shell, must contain
`PREFILL_ENABLE_MIGRATION=1` and `PREFILL_MIGRATION_EXPORT_TO_FILE=1` plus the
table/map output paths. File export does not wait for a legacy worker handshake.

Wait for all 36 layers, warmup, table/map export, and
`setup complete, entering request loop`. Verify
`/dev/shm/tt_h2d_stream_service_ds_prefill.bin` and the exported table/map files.
A standalone producer/36-ack smoke is optional before d-gen owns the channel;
it verifies execution, not numerical accuracy.

## Launch decode in its assigned host or IRD

Use the repository wrapper: it selects the LoudBox descriptor and disables
model use of Blackhole DRAM-programmable cores so KVM can use their DRISC
data movers. Do not import Galaxy defaults (eight stages, 32 devices).

```bash
cd "$DGEN"
export TT_MISTRAL_MODEL_PATH="$MODEL" TT_MISTRAL_WEIGHT_CACHE="$DECODE_CACHE"
export BLAZE TT_METAL_CACHE="$DECODE_JIT"
export N_STAGES=2 LAYERS_PER_VISIT=2 N_SLOTS=1 CACHE_TOKENS=8192 ROUTED_EXPERT_TP=1
export PREFIX=mistral4 TABLE="$KV_TABLE_DIR/mistral4_decode_kv_table.pb"
export DEVMAP="$KV_TABLE_DIR/mistral4_decode_device_map.txt"
export STOP="$RUN/mistral4_decode.stop" LOG="$RUN/logs/decode-ring.log"
export TT_METAL_PINNED_MEMORY_CACHE_LIMIT_BYTES=0
setsid scripts/mistral4/serve_mistral4_ring.sh \
  > "$RUN/logs/decode-launch.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/decode-ring.pid"
```

Keep the pinned-memory-cache limit at zero on both model runners for this
LoudBox recipe. During live lifecycle validation, decode stalled on its final
capture without this setting; the retry with it completed and passed migrated
requests. Verify the variable reaches both MPI ranks, not only the launcher.

The wrapper deletes stale stop/table/socket files; run it only after confirming
no active ring owns them. Wait for
`KVM ready: layers=0-35, slots=1, cache=8192, sockets='mistral4'` and confirm the
table/map files were exported by this launch. The compatible existing ring can
be reused instead. Before a d-gen worker attaches, an exclusive `ring_driver.sh`
generation smoke is available in the aggregated decode guide.

Once runners are ready, proceed to [networking.md](../../.agents/skills/setup-disagg/references/networking.md) and
[serving.md](SERVING.md). No request is disaggregated until the workers and
real KV managers are connected and validation passes.
