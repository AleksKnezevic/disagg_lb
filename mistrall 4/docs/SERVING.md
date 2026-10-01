# Migration managers, workers, and endpoint

Use after the [model runners](MODEL_RUNNERS.md) have exported live tables and the
[network plan](../../.agents/skills/setup-disagg/references/networking.md) is established. The commands below use site
variables from those references. Save resolved commands as local launchers;
do not depend on variables surviving a different SSH session or `docker exec`.

## Which migration layer

Use d-gen `runtime.kv_manager="kvm"`: one real KV Manager per host, Mooncake
TCP between them, and the prefill KVM's ZMQ ingress for both d-gen workers.
The old `kv_manager="migration"` backend uses a different shared-memory
worker/handshake protocol. Do not launch it alongside this recipe or wait for
its queues: the runners here **export to files without a worker handshake**.
Mooncake Store/master is not needed for prefill-to-decode transfer.

## Discovery and broker

Reuse healthy services if they belong to this deployment. The verified
arrangement uses one etcd on 2379 for Dynamo and KVM, plus NATS with JetStream
on 4222 for Dynamo. Select an unused Dynamo namespace (for example `m4disagg`),
and use the same namespace, model name, request plane, and event plane on both
workers and frontend.

If services are absent, launch these on the designated control bare-metal host,
using a writable persistent data directory and the host's actual IP:

```bash
# Native etcd example; ETCD_BIN is an installed binary, e.g. the one staged by
# adapters/dynamo/setup_router_env.sh when called without --no-etcd.
mkdir -p "$RUN/etcd-data"
setsid "$ETCD_BIN" --name disagg --data-dir "$RUN/etcd-data" \
  --listen-client-urls "http://$CONTROL_IP:2379" \
  --advertise-client-urls "http://$CONTROL_IP:2379" \
  --listen-peer-urls http://127.0.0.1:2380 \
  --initial-advertise-peer-urls http://127.0.0.1:2380 \
  --initial-cluster disagg=http://127.0.0.1:2380 \
  > "$RUN/logs/etcd.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/etcd.pid"

# Containerized NATS, on bare metal; choose a name unique to this deployment.
docker run -d --name disagg-nats --network host \
  nats:2.10-alpine --addr "$CONTROL_IP" --port 4222 \
  --jetstream --store_dir /data
```

A native `nats-server` can instead run with the same server arguments, using a
real host data directory. Persist the NATS data volume if container recreation
must preserve broker state. Record the actual image digest/version used.
Do not start another etcd/NATS on occupied ports or delete their data to make
startup succeed. Check `curl -fsS "$ETCD_ENDPOINT/health"` and the broker logs
from the network namespaces that will connect.

## KV Manager build or image

On the bare-metal host, inspect an existing image first:

```bash
docker image inspect "$KVM_IMAGE" \
  --format 'revision={{index .Config.Labels "org.opencontainers.image.revision"}} metal={{index .Config.Labels "tt-d-gen.metal"}}'
```

The verified image was `kv-manager:m4-lb-disagg`, ID `7a4aa96ee27c`, built at
d-gen `ba4c33e…` with metal. That is evidence, not a universally available tag.
Check the full revision and image contents. A valid image contains the real
`kv_manager`, its tt-metal/Mooncake libraries, and `dmk.elf`.

To build one, on a host with Docker/buildx and the pinned source available:

```bash
cd "$DGEN"
git submodule update --init --recursive kv_manager/third_party/mooncake
KV_MANAGER_IMAGE="$KVM_IMAGE" JOBS=16 \
  ./kv_manager/scripts/build_kv_manager_image.sh
```

Use an already-built image on both hosts or transfer it with `docker image
save`/`load`; verify the resulting image IDs. Building from a donor requires a
full built tt-metal tree, not a stripped KVM runtime image. The wrapper's
`--from-image` option supports that case. An IRD usually has no Docker socket:
run image management on bare metal, not inside the reservation by assumption.

For **native bare-metal KVMs**, build the real backend:

```bash
cd "$DGEN"
git submodule update --init --recursive kv_manager/third_party/mooncake
# Install the packages specified by kv_manager/apt-packages-build.txt and the
# Go version in mooncake-common/etcd/go.mod, then install yalantinglibs:
YLT_BUILD=$(mktemp -d)
cmake -S kv_manager/third_party/mooncake/extern/yalantinglibs \
  -B "$YLT_BUILD" -DBUILD_BENCHMARK=OFF -DBUILD_EXAMPLES=OFF -DBUILD_UNIT_TESTS=OFF
cmake --build "$YLT_BUILD"
sudo cmake --install "$YLT_BUILD"
./kv_manager/build.sh --mooncake --skip-blaze-build --install-sysdeps --jobs 16
```

Blaze must already be built before using `--skip-blaze-build`. The pinned Go
version was 1.25.10; distro Go can be too old. Read the pinned KVM README's
“With the Mooncake transfer engine” section for dependency errors. RDMA
libraries are build dependencies even when runtime transport is TCP.
Do not use `--no-blaze`, omit Mooncake, or select mock device I/O to bypass a
failure. Native processes need device permissions, locked-memory resources,
the compiled DMK image, and their runtime libraries, just like the image.

## Confirm table ownership before launch

Each KVM needs both tables and its role's own map:

```bash
python3 "$DGEN/kv_manager/scripts/fleet/extract_table_hosts.py" \
  "$KV_TABLE_DIR/mistral4_prefill_kv_table.pb"
python3 "$DGEN/kv_manager/scripts/fleet/extract_table_hosts.py" \
  "$KV_TABLE_DIR/mistral4_decode_kv_table.pb"
```

Compare with `deployment.json`'s `table_host` values. The pinned identity rule
is `host-%08x` of `crc32(runner_hostname) & 0x7fffffff`.
`KV_MANAGER_TABLE_HOST` is the **literal runner hostname**, not the hashed
value, KVM ID, IP, or arbitrary bare-metal name. A different identity can leave
the manager waiting forever for a host or with no chunks to serve.

The generator's environment files contain role-specific identities and maps,
8 visible chips, `KV_MANAGER_DEVICE_IO=dmk`, TCP transfer, and the shared etcd
endpoint. Both KVMs are group leaders in this one-prefill/one-decode topology.
Only the prefill leader exposes the common ZMQ command ingress on 9093.

## Launch the KV managers

Run on each **bare-metal host**, setting `ROLE=prefill` or `ROLE=decode`:

```bash
docker run -d --name "disagg-kvm-$ROLE" --network host --ipc host \
  --ulimit memlock=-1 --cap-add IPC_LOCK --device /dev/tenstorrent \
  --mount type=bind,src=/dev/shm,dst=/dev/shm \
  --mount type=bind,src=/dev/hugepages-1G,dst=/dev/hugepages-1G \
  --mount "type=bind,src=$KV_TABLE_DIR,dst=$KV_TABLE_DIR,readonly" \
  --env-file "$CFG/kvm.$ROLE.env" "$KVM_IMAGE"
```

Check that the hugepage mount exists; use the host's configured memory setup
and the pinned fleet launcher/DMK requirements if it differs. These KVMs read
device memory through DMK and the exported maps; they do not attach to the
model runner's private ack channel. The example follows the verified fleet's
host IPC/device/memory flags. Do not reset devices during KVM startup.

For a native KVM, load the same env file without shell evaluation. Set
`KVM_BIN` to the built executable, configure `TT_METAL_HOME`,
`TT_METAL_RUNTIME_ROOT`, library paths and any required
`KV_MANAGER_DMK_ELF_PATH` for the build, then use this in a saved launcher:

```bash
python3 - "$CFG/kvm.$ROLE.env" "$KVM_BIN" <<'PY'
import os, sys
from pathlib import Path
env = os.environ.copy()
for line in Path(sys.argv[1]).read_text().splitlines():
    if line and not line.startswith('#'):
        key, value = line.split('=', 1)
        env[key] = value
os.execve(sys.argv[2], [sys.argv[2]], env)
PY
```

Keep both managers running and wait for healthy responses at
`http://PREFILL_IP:18081/health` and `http://DECODE_IP:18081/health`.
Check logs for real DMK startup, both tables loaded, peer discovery complete,
and Mooncake's advertised RPC/data addresses. TCP “no RDMA devices” discovery
warnings alone are not a failure. Readiness is necessary but not sufficient:
validation must show actual data transfer.

## Launch d-gen workers

Run each worker in the **same execution/shared-memory environment as its model
runner**. For bridged IRD, start the host relays first. Both workers use the
generated prefill KVM ingress and peer host addresses; keep
`device.workload.manage=false` because the model runners are hand-launched.

In each worker's environment, create a launcher using the following settings.
Set `ROLE`, `HOST_IP`, `SERVED_MODEL`, `NAMESPACE`, paths and discovery endpoints
explicitly in the launcher. Use `COMPONENT=prefill` for prefill and
`COMPONENT=backend` for decode:

```bash
cd "$DGEN"
export ETCD_ENDPOINTS="$ETCD_ENDPOINT" NATS_SERVER="$NATS_ENDPOINT"
export DYN_SYSTEM_PORT=18082 DYN_LOG=info
# This line assumes shared metadata storage was verified (networking.md).
export DYN_SELF_HOST_METADATA=0
export DYN_TCP_RESPONSE_STREAM_HOST="$HOST_IP" DYN_TCP_RESPONSE_STREAM_PORT=19101
export TT_METAL_HOME="$DGEN/third_party/tt-blaze/tt-metal"
export TT_METAL_RUNTIME_ROOT="$TT_METAL_HOME"
export PYTHONPATH="$DGEN/adapters/dynamo:$DGEN/bindings/python${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$TT_METAL_HOME/build/lib${MPI_LIB:+:$MPI_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec adapters/dynamo/.venv/bin/python -u -m tt_dynamo.main \
  --config "$CFG/dynamo.$ROLE.json" --model-path "$MODEL" \
  --served-model-name "$SERVED_MODEL" --namespace "$NAMESPACE" \
  --component "$COMPONENT" --request-plane nats --event-plane nats
```

`MPI_LIB` is the installed MPI library directory if not already discoverable.
Set it from the reservation's MPI installation (the CLI uses `paths.mpi_lib`).
Check `ldd` and imports in the actual worker environment.
`DYN_SYSTEM_ENABLED` is deprecated in this pinned Dynamo; the port enables the
system server. Native workers with reachable self-hosted metadata may use
`DYN_SELF_HOST_METADATA=1` instead, after verifying the advertised URLs.

Keep the workers' fixed response-stream host/port settings out of the native
frontend environment. In the pinned Dynamo, a host setting with an automatic
port can be interpreted as an interface name; a literal IP then fails with
`Interface not found`. Clear both `DYN_TCP_RESPONSE_STREAM_HOST` and
`DYN_TCP_RESPONSE_STREAM_PORT` for the native frontend so it discovers its
interface and selects its own response port. The CLI does this automatically.

Detach saved launchers with `setsid ... > LOG 2>&1 < /dev/null &` and record PIDs,
or use `docker exec -d -u USER IRD bash /mounted/path/to/launcher.sh` with logging
handled inside the launcher. Avoid two workers sharing one runner/socket prefix.
Expect logs showing the correct runtime role, geometry, KVM endpoint, and
`Serving MODEL on NAMESPACE.prefill.generate` / `NAMESPACE.backend.generate`.

## Launch frontend and confirm registration

On the frontend's host/environment, with a usable Dynamo virtualenv:

```bash
export ETCD_ENDPOINTS="$ETCD_ENDPOINT" NATS_SERVER="$NATS_ENDPOINT"
unset DYN_TCP_RESPONSE_STREAM_HOST DYN_TCP_RESPONSE_STREAM_PORT
exec "$DGEN/adapters/dynamo/.venv/bin/python" -u -m dynamo.frontend \
  --http-host 0.0.0.0 --http-port 8000 --namespace "$NAMESPACE" \
  --discovery-backend etcd --request-plane nats --event-plane nats \
  --router-mode kv --router-kv-events --enforce-disagg
```

Use a saved, detached launcher with a log/PID file. Verify the autodetected
response-stream address from both workers, especially on a multihomed host.
Do not copy the workers' IP-only host override into this automatic-port
frontend configuration. Host networking avoids Docker forwarding for the
frontend's response listener.

Wait for prefill-router activation and `/v1/models` listing the served model.
The generator uses one slot, prefill capacity 10240, decode capacity 8192,
36 layers, 64-token KV blocks, and matching `min_disagg_tokens=256`. Prefill's
`sp_factor=2`, `chunk_aligned_start=true`, and decode's
`pipeline_inflight_cap=1` are required by this recipe. Do not copy the Galaxy
template's SP=8, 32 devices, large context, or two slots unchanged.

Proceed to [validation-and-recovery.md](VALIDATION_AND_RECOVERY.md); a populated
model list alone is not proof of correct disaggregation.
