# Bare-metal and IRD networking

## Choose the process placement

| Deployment | Runners and d-gen workers | KV managers | Network work |
|---|---|---|---|
| Native bare metal | Same host/shared-memory view | Native processes or host-networked KVM containers | Bind/advertise reachable host IPs; check existing firewall rules |
| New host-networked IRD, if the reservation supports it | Keep each worker with its runner and shared memory | Native or host-networked KVM containers | No Docker port publishing; arrange SSH to avoid host port conflicts |
| Existing bridged IRD (verified flow) | Inside each existing IRD | Additional host-networked KVM containers | Bare-metal relays to the IRD's fixed worker ports |

Host-networked KVM containers are also a convenient bare-metal deployment: they
package the native libraries and DMK image without requiring model runners to
move into Docker. Use the selected model recipe for compatible native KVM build instructions;
see the [Mistral example](<../../../../mistrall 4/docs/SERVING.md>).

## Identify each namespace and access path

SSH to the reservation port lands **inside IRD**, not on the host. In the
verified reservation, port 22 reached bare metal and port 41338 reached IRD;
discover the actual reservation mapping on a new host. Verify with `hostname`.
On bare metal inspect:

```bash
docker ps --format '{{.ID}} {{.Names}} {{.Ports}}'
docker inspect -f '{{.Config.Hostname}} {{.HostConfig.NetworkMode}} {{.HostConfig.IpcMode}}' "$IRD"
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$IRD"
ip -brief address
ss -lnt
```

Use `sudo -n docker` if permitted and required. Docker access does not imply
unrestricted host sudo: in the verified session Docker administration was
allowed while direct `sudo nsenter` was denied. Normal user processes can bind
the high-numbered relay ports; host namespace or firewall modifications are
not required for the verified approach.

Two different hosts can each have a container at `172.17.0.2`. Never advertise
that duplicate private address across hosts. Both workers' peer configs use
the respective **host** IP; local host-side relays target the inspected IRD IP.
Test from the actual peer namespace, not only from the service's own host.

## Preserve the runner's shared memory

Before building inside a new IRD, verify that the reservation exposes all
assigned `/dev/tenstorrent/*` devices with usable permissions, its configured hugepage
mounts and memory-lock limits, and enough writable local storage. Check that
checkpoint/cache paths resolve inside the IRD and that host-side KVM launchers
can access exported tables through the host's bind-mount paths. Verify the
actual checkpoint, cache, home, and hugepage mounts for the reservation. Reuse the
reservation's device setup; do not install a different host KMD from inside an
IRD or assume that the model image alone provisions hardware access.

The runner/worker connection is through shared-memory descriptors and channels,
not through an HTTP port. A worker on the bare-metal host does not automatically
see an IRD's `/dev/shm`, even when the same checkout is bind-mounted.

Run each d-gen worker inside its runner's IRD. `docker exec -d -u USER IRD ...`
is sufficient; redirect output inside the IRD to a known mounted log path.
Inspect `IpcMode` before considering a separate worker container: an IRD with
private IPC cannot donate its IPC namespace via `--ipc container:IRD`.
Do not rely on Docker-internal rootfs mount paths as a shared-memory interface.

An existing IRD does not need to be recreated or switched to host networking.
Model runners can keep their local MPI networking; only d-gen coordination and
KV managers need cross-host reachability.

## Bridged-IRD arrangement

This arrangement was verified with the [Mistral recipe](<../../../../mistrall 4/README.md>).
Recheck transport behavior against the chosen software pins for another recipe.

Use NATS for **both Dynamo request and event planes**. This centralizes their
broker connections. Dynamo response streams can still use TCP: configure
`DYN_TCP_RESPONSE_STREAM_HOST` to the role's host IP and
`DYN_TCP_RESPONSE_STREAM_PORT=19101`, and forward that port too. NATS does not
eliminate every TCP listener.

Run KVM containers with `--network host`. The pinned Mooncake implementation
assigns RPC/data ports dynamically and publishes them via etcd. Its old
`KV_MANAGER_TRANSFER_ENGINE_RPC_PORT` setting is deprecated and not read.
Publishing only 18650/9093 would leave the transfer path incomplete.

| TCP port | Placement/reachability | Purpose |
|---|---|---|
| 19071 | Each host → own IRD:19071; reachable from its peer | d-gen KV rendezvous |
| 19101 | Each host → own IRD:19101; reachable from its peer | Configured Dynamo response stream |
| 9093 | Prefill host; reachable from both workers | Prefill KVM ZMQ command ingress |
| 18650 | Both hosts; reachable from the other KVM | KVM peer coordination |
| Dynamic | Both KVM host-network namespaces | Mooncake RPC and payload TCP |
| 18081 | Both KVM hosts; operator access | Health/metrics; not a KV transfer port |
| 2379 | Shared etcd host; all services can connect | Discovery and Mooncake metadata |
| 4222 | NATS host; frontend and workers can connect | Dynamo broker |
| 8000 | Frontend host; clients can connect | OpenAI-compatible API |
| 18082 | Each worker locally | Worker system/metrics server; publish only if needed |

These are the recipe's chosen ports, not Tenstorrent-wide requirements. Adjust
all references consistently if occupied. The fleet example uses a separate
12379 etcd for Dynamo; the verified run shares one 2379 etcd using distinct key
spaces. Do not wipe an existing etcd database or use a `--fresh` launcher on a
shared deployment. Restrict access to the intended serving network using the
site's existing network controls; these example services have no API auth.

For each relay, run on bare metal, using the actual values:

```bash
# Set SKILL to the repo's .agents/skills/setup-disagg directory.
# HOST_IP and IRD_IP belong to this host; PORT is the chosen worker port.
setsid python3 "$SKILL/scripts/tcp_forward.py" \
  --listen "$HOST_IP:$PORT" --target "$IRD_IP:$PORT" \
  > "$RUN/logs/relay-$ROLE-$PORT.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/relay-$ROLE-$PORT.pid"
```

Launch only missing relays; inspect an existing listener before reusing it.
No root access is needed for these ports. This is host-side forwarding, not a
change to Docker's published-port metadata. Dockerfile `EXPOSE` alone would
not forward traffic. Keep the relay alive independently of the SSH session.

## Model metadata and filesystem visibility

The default worker metadata server may advertise `http://172.17.0.2:18082/...`.
On the other bare-metal host that can reach the **wrong local IRD**, causing
404s and an empty model list despite both workers logging `Serving`.

With verified shared home/model-metadata storage, set
`DYN_SELF_HOST_METADATA=0` on both workers. Dynamo then uses shared filesystem
metadata; verify the frontend can read the resolved metadata paths. Same path
strings on independent local disks are insufficient.

Without shared metadata storage, establish a supported reachable metadata
endpoint or synchronize the exact metadata tree and advertised paths. Inspect
the pinned Dynamo implementation rather than inventing an environment variable
for its advertised host. Do not leave the wrong private URL and interpret a
decode-only model registration as successful disaggregation.

KV tables/maps have a separate requirement: each KVM must see both live tables
and its own matching device map. Mount the common table directory read-only
into the KVMs at the same absolute path used by generated env files. If there is
no shared storage, copy the newly exported files to the intended host paths,
verify checksums, and repeat after each runner restart. Bind-mounting a directory
avoids pinning a stale file inode when a runner replaces an export.

## Client access

Use `http://FRONTEND_HOST:8000/v1`, with the configured served model name.
If only host SSH is reachable, keep this tunnel open in a separate terminal:

```bash
ssh -N -L 18000:127.0.0.1:8000 USER@FRONTEND_HOST -p HOST_SSH_PORT
```

The client base URL is then `http://localhost:18000/v1`. Use the bare-metal SSH
endpoint when the frontend runs on bare metal. An IRD SSH tunnel's `127.0.0.1`
would refer to the IRD, a different namespace.
