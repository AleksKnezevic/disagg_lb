# Disaggregated serving on LoudBox

Setup guidance and model recipes for running prefill and decode on separate
Tenstorrent hosts, either as native bare-metal processes or inside IRD
reservation containers.

Shared documentation covers process placement, networking, live KV migration,
lifecycle order, and validation. Model folders contain their own source pins,
builds, geometry, configurations, launch tools, and test evidence.

| Location | Contents |
|---|---|
| [Setup skill](.agents/skills/setup-disagg/SKILL.md) | Agent workflow for selecting and executing a model recipe |
| [Architecture](docs/ARCHITECTURE.md) | Request/KV paths, host/container boundaries, and example ports |
| [Operations](docs/OPERATIONS.md) | Shared deployment workflow and model-specific commands |
| [Networking](.agents/skills/setup-disagg/references/networking.md) | Bare-metal access, IRD relays, shared memory, metadata, and client tunnels |
| [Mistral Small 4](<mistrall 4/README.md>) | Verified two-LoudBox recipe, CLI, tests, and current deployment handoff |

Agents can use `$setup-disagg` or read its `SKILL.md` directly. Select the
model recipe before generating configs or launching services; the current CLI
targets Mistral Small 4 and is not a general model launcher.

Run the Mistral recipe's offline tests from the repository root:

```bash
python3 -m unittest discover -s 'mistrall 4/tests' -v
```

Keep checkpoint weights, caches, upstream builds, credentials, and live runtime
state outside Git. The model recipe documents those dependencies. Repository
layout changes do not require restarting an existing deployment.
