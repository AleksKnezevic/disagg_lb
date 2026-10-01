---
name: setup-disagg
description: "Set up, connect, validate, or recover Tenstorrent disaggregated prefill/decode serving on bare metal or IRD reservation containers. Select a model recipe, configure networking and real KV migration, and expose a serving endpoint. Includes a verified Mistral Small 4 LoudBox recipe."
---

# Set up disaggregated serving

Establish both paths:

`client → frontend → prefill/decode adapter workers → model runners`

`prefill device KV → KV Manager → transport → KV Manager → decode device KV`

Resolve the repository root as `../../..` from this skill directory. Read the
repo's instructions and inspect live state before launching.

## Select the model recipe

The available hardware-verified recipe is
[Mistral Small 4](<../../../mistrall 4/README.md>) on two eight-device Blackhole
LoudBoxes. Model-specific scripts, manifests, pins, builds, and tests live
under `mistrall 4/`:

- [Model builds and runners](<../../../mistrall 4/docs/MODEL_RUNNERS.md>) for
  hardware geometry, caches, and migration-enabled launches.
- [Serving](<../../../mistrall 4/docs/SERVING.md>) for real KV-manager builds,
  discovery, workers, and the frontend.
- [Operations CLI](<../../../mistrall 4/docs/OPERATIONS.md>) for site generation,
  preflight, owned-process start/status/stop, and migration smoke tests.
- [Validation and recovery](<../../../mistrall 4/docs/VALIDATION_AND_RECOVERY.md>)
  for recipe-specific evidence and failure diagnosis.

For another model, establish its supported source revisions, runner interfaces,
geometry, caches, live-table format, capacity, and migration checks before
launching. Do not apply the Mistral CLI or dimensions merely because hosts and
ports can be configured. Deployment snapshots are evidence, not defaults for a
new reservation.

## Establish deployment facts

Obtain bare-metal hosts, routable addresses, runner placement, assigned devices,
and access to the relevant namespaces. For IRDs, record container identity,
SSH mapping, bridge IP, mounts, and IPC mode. Host-side Docker or relay
administration requires host access; IRD SSH alone is insufficient.

Locate pinned checkouts, checkpoint, caches, Python/MPI environments, and a
runtime directory. Verify how both KV managers see fresh tables/maps and how
workers/frontend share or retrieve model metadata. Inspect device owners,
discovery services, and occupied ports. Keep supplied credentials out of files
and logs.

Read [networking](references/networking.md) for native/IRD placement, relays,
shared memory, and client tunnels. Use [tcp_forward.py](scripts/tcp_forward.py)
for individual host-side relays where appropriate. The shared
[operations workflow](../../../docs/OPERATIONS.md) and
[architecture](../../../docs/ARCHITECTURE.md) explain ordering and connections;
use the selected recipe for executable commands.

## Preserve operational invariants

- Verify the actual compute grid and assigned devices against the model recipe.
  A board label alone does not establish compatibility.
- Preserve source pins and separate incompatible build/runtime environments.
- Keep each worker with its runner's shared-memory channels. Detach standalone
  producers and ack drains before the worker owns those channels.
- Match worker dimensions, capacities, sockets, and migration formats to its
  runner. Usable context is bounded by both roles.
- Use reachable advertised addresses and distinguish worker rendezvous from
  KV-manager control ports. Check from the consuming namespace.
- Regenerate live-address tables after runner restart and reload the managers.
  Table identity follows the runner's hostname, which may be an IRD hostname.
- Demonstrate real device I/O, transferred bytes, and correct fresh output.
  Mock mode, health responses, and model registration alone are insufficient.
- Reuse compatible existing services when appropriate. The Mistral CLI manages
  only services it started; use manual procedures for hand-launched roles.
  Do not reset devices or recreate active IRDs merely to repair connectivity.

## Finish with a handoff

Record role/host/container mapping, revisions, configs, table ownership,
logs/process records, startup/shutdown order, health URLs, API/model, limits,
and validation evidence. Include direct and SSH-tunnel access and state
logout/reboot behavior. Distinguish functional smoke validation from numerical
accuracy or throughput qualification.
