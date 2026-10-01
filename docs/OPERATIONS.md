# Disaggregated serving workflow

Start with the [setup skill](../.agents/skills/setup-disagg/SKILL.md), the
[architecture](ARCHITECTURE.md), and the selected model's recipe. Build pins,
mesh geometry, runner interfaces, KV formats, capacity, and smoke-test
thresholds belong to that recipe.

The implemented lifecycle CLI is currently the
[Mistral Small 4 CLI](<../mistrall 4/docs/OPERATIONS.md>). Its configuration
accepts different hosts, paths, and ports, but its model assumptions remain
Mistral-specific. The [deployment handoff](<../mistrall 4/docs/VALIDATION.md>)
records the running host pair and how to control it.

## Establish the site

Record both bare-metal hosts, routable addresses, device reservations, and
runner environments. For IRDs, distinguish host SSH access from container SSH
access and record container names, hostnames, bridge addresses, mounts, and IPC
configuration. Keep each worker in its runner's shared-memory environment.

Provide checkout, checkpoint, cache, and runtime paths through the recipe's
site configuration. Verify builds and images at the documented pins. Use keys,
an SSH agent, or authenticated control connections; keep passwords out of files.

Inspect processes and listeners before launching. Follow the
[networking guide](../.agents/skills/setup-disagg/references/networking.md)
for relays, advertised addresses, metadata visibility, and payload ports.
Matching filesystem path strings on two hosts do not prove shared storage.

## Bring up and validate

For the d-gen/KV-manager architecture documented here, start discovery and
broker services, then model runners. Wait for each runner's readiness marker
and fresh KV table/map exports. Start both KV managers before waiting for peer
health, then the IRD relays, adapter workers, and frontend.

Use the selected recipe's config generator, preflight, and lifecycle tools.
Send fresh requests, check their answers, and demonstrate source-device, wire,
and destination-device bytes with no new transfer errors. Exercise multiple
prefill chunks when supported. Process liveness and model registration alone
are insufficient.

Record the API base URL, model name, concurrency/context limits, configs, logs,
ownership records, and validation results. State whether services survive
logout or reboot. Native-runner support in a recipe does not imply that every
native hardware configuration has been tested.

## Shutdown and recovery

Drain traffic, then stop frontend/workers, relays, KV managers, model runners,
and discovery services in dependency order. Use recorded process identities or
deployment-owned containers; preserve unrelated services.

A runner restart invalidates its exported device addresses. Generate fresh
tables/maps, make them visible to the managers, and reload the managers before
serving again. Follow the model-specific readiness and shutdown mechanisms;
an initializing runner may not yet consume a serving-loop stop file.

For another model, add a recipe folder with its pins, supported hardware,
configs, launch/stop procedures, and migration validation. Keep shared guidance
here, and link the recipe from the root README and setup skill.
