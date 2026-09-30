# Aggregated decode on one LoudBox

Last verified: 2026-09-30 UTC

This is the one-machine, decode-only baseline. The decode ring handles the
prompt itself, so this mode has no prefill worker, KV Manager, or KV migration.
It uses two `2x2` stages on the eight-device LoudBox with replicated routed
experts, one request slot, and an 8,192-token context.

## Source pins

```text
tt-d-gen  sshon/mistral4-disagg-rebased-260929
          ba4c33e33752ee19223b2d219c319dd4ed703b8d
tt-blaze  sshon/wip-mistral-ring-pdd-rebased-260929
          1e6fce22043c98a194f1b3bfc23928e40136c4e0
```

The Blaze commit is the `third_party/tt-blaze` submodule pin at the d-gen
commit. No source patches were needed.

## Build

```bash
export DGEN=$PWD/tt-d-gen
export ASSETS=$PWD/disagg_lb
export WEIGHTS=/path/to/Mistral-Small-4-119B-2603
export BLAZE_WEIGHT_CACHE=/path/to/mistral4-weight-cache
export SERVED_MODEL=mistralai/Mistral-Small-4-119B-2603

git clone --recurse-submodules --branch sshon/mistral4-disagg-rebased-260929 \
  git@github.com:tenstorrent/tt-d-gen.git "$DGEN"
git -C "$DGEN" checkout ba4c33e33752ee19223b2d219c319dd4ed703b8d
git -C "$DGEN" submodule update --init --recursive
test "$(git -C "$DGEN/third_party/tt-blaze" rev-parse HEAD)" = \
  1e6fce22043c98a194f1b3bfc23928e40136c4e0

cd "$DGEN/third_party/tt-blaze"
UV_LINK_MODE=copy ./install.sh

cd "$DGEN"
./adapters/dynamo/setup_router_env.sh
source adapters/dynamo/.venv/bin/activate
```

If system `libzmq` is unavailable, follow the d-gen runbook to build libzmq
4.3.5 into a user prefix, then build the bindings with that prefix:

```bash
PKG_CONFIG_PATH=/path/to/libzmq/lib/pkgconfig SKIP_SYSDEPS=1 \
  ./build_dgen.sh --dynamo --blaze --skip-blaze-build \
  --build-dir build-bindings --target _tt_engine --target _tt_dynamo_kv --jobs 16
```

Confirm both checkouts and the model files before touching the hardware:

```bash
git -C "$DGEN" rev-parse HEAD
git -C "$DGEN/third_party/tt-blaze" rev-parse HEAD
test -f "$WEIGHTS/config.json"
test -f "$WEIGHTS/tokenizer.json"
```

## Reset and link check

Only reset when no other workload owns the box:

```bash
tt-smi -r
sleep 75

cd "$DGEN/third_party/tt-blaze"
source env.sh
unset TT_MESH_GRAPH_DESC_PATH
TT_METAL_SLOW_DISPATCH_MODE=1 ./tt-metal/python_env/bin/python -c \
  'import ttnn; d=ttnn.open_mesh_device(ttnn.MeshShape(4,2)); ttnn.close_mesh_device(d); print("links ok")'
```

## Start the Dynamo frontend

Choose the routable address of this host. Do not copy an address from another
machine's logs.

```bash
export FRONTEND_IP=<routable-host-ip>
cd "$DGEN"
ETCD_HOST="$FRONTEND_IP" \
  ./adapters/dynamo/launch_frontend.sh --fresh --router-mode kv
```

The default aggregated setup uses etcd on port 2379 and the OpenAI-compatible
frontend on port 8000.

## Start the decode ring

Run the ring in its own terminal or process group. `setsid` is important for a
tool-managed background process because a plain `nohup ... &` can still die
with its parent's process group.

```bash
cd "$DGEN"
mkdir -p "$BLAZE_WEIGHT_CACHE" /var/tmp/$USER-ttcache-m4

TT_MISTRAL_MODEL_PATH="$WEIGHTS" \
TT_MISTRAL_WEIGHT_CACHE="$BLAZE_WEIGHT_CACHE" \
N_STAGES=2 LAYERS_PER_VISIT=2 \
N_SLOTS=1 CACHE_TOKENS=8192 ROUTED_EXPERT_TP=1 \
TT_METAL_CACHE=/var/tmp/$USER-ttcache-m4 \
LOG=/dev/stdout \
  setsid scripts/mistral4/serve_mistral4_ring.sh 2>&1 | tee ring.log
```

Wait for:

```text
[mistral4/reload] KVM ready: layers=0-35, slots=1, cache=8192, sockets='mistral4'
```

The first validated launch took about 1h52m while compiling and populating
caches. Startup time is cache-sensitive. Progress should continue through 19
captured images; watch the log rather than assuming a quiet compile is healthy.

Before starting the worker, an exclusive direct-ring smoke test is useful:

```bash
TT_MISTRAL_MODEL_PATH="$WEIGHTS" \
  scripts/mistral4/ring_driver.sh --prefix mistral4 --slot 0 \
  --prompt "The capital of France is" --generate 12 --show-d2h 2
```

Expect a Paris answer and token IDs below 131,072. The direct driver and d-gen
worker must not own the sockets at the same time.

## Start the d-gen worker

```bash
cd "$DGEN"
source adapters/dynamo/.venv/bin/activate

export DYNAMO_PY=$DGEN/adapters/dynamo/.venv/bin/python
export LIBZMQ_LIB=/path/to/libzmq/lib   # omit LD_LIBRARY_PATH if system libzmq is used

ETCD_ENDPOINTS="http://$FRONTEND_IP:2379" \
PYTHONPATH="$DGEN/adapters/dynamo:$DGEN/bindings/python" \
LD_LIBRARY_PATH="$LIBZMQ_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
TT_METAL_HOME="$DGEN/third_party/tt-blaze/tt-metal" \
TT_METAL_RUNTIME_ROOT="$DGEN/third_party/tt-blaze/tt-metal" \
DYN_SYSTEM_PORT=20020 \
DYN_HEALTH_CHECK_ENABLED=true \
"$DYNAMO_PY" -m tt_dynamo.main \
  --config "$ASSETS/configs/decode/dynamo.aggregated.decode.json" \
  --model-path "$WEIGHTS" \
  --served-model-name "$SERVED_MODEL"
```

The JSON and ring geometry must agree: one slot and 8,192 tokens. The worker
configuration deliberately has `manage: false`, because the ring was started
separately and owns the device lifecycle.

## Verify

```bash
curl -sS "http://$FRONTEND_IP:20020/health" | python -m json.tool
curl -sS "http://$FRONTEND_IP:8000/v1/models" | python -m json.tool

curl -sS "http://$FRONTEND_IP:8000/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$SERVED_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a short essay on the dangers of artificial intelligence.\"}],\"max_tokens\":256,\"temperature\":0,\"stream\":false}" \
  | python -m json.tool
```

The verified latency shape is one request at a time with roughly 2.32–2.36 ms
per output token. Prompts must leave room for output inside the 8,192-token
context.

## Teardown

Stop the frontend, worker, and ring gracefully, then verify that no d-gen,
reload-ring, MPI, or tt-run children remain. Only after those checks, use
`tt-smi -r` if the box needs a clean reset. Do not use broad `pkill` patterns on
a shared machine; retain the launcher PIDs/process groups when starting each
long-running component.
