# Initial disaggregated Mistral Small 4 deployment

This is the original manual deployment snapshot. The repository CLI has since
rebuilt and tested this host pair using namespace `m4revalidated`; see
[deployment validation](VALIDATION.md) for the managed lifecycle and current
validation state. The historical launch scripts and process inventory below
must not be used to control the managed deployment.

Verified 2026-10-01 UTC. Prefill runs on `bh-lb-17` (`10.250.36.143`),
decode on `bh-lb-08` (`10.250.36.68`). The OpenAI-compatible endpoint is
`http://10.250.36.68:8000/v1`, model `mistral4`, namespace `m4disagg`.
One concurrent slot; maximum total decode context is 8,192 tokens.

The runner containers were preserved. Only prefill was restarted, to enable
migration and export its live KV table. Decode retained its original runner.
No changes to tt-metal, Blaze, or d-gen source were needed.

## Validation

| Request | Prompt tokens | Result |
|---|---:|---|
| Existing standalone decode fixture, now through the disaggregated API | 553 | `The capital of France is Paris.` |
| New code-recall chat request | 1,286 | `MARIGOLD-7826` |
| New code-recall request across two prefill chunks | 6,291 | `COBALT-4931` |

The last request executed prefill ranges `[0,5120)` and `[5120,6291)`.
Across all three tests, the prefill KV manager recorded 144 successful layer
migrations, 86,169,600 payload bytes sent, and zero transfer-batch failures.
Both KV-manager health endpoints returned `healthy`. These are functional
smokes, not a throughput or full numerical-accuracy qualification.

Artifacts are in shared storage:
`/home/aknezevic/disagg_lb_runtime/current/`. They include the three
`*-request.json` / `*-response.json` pairs, final KV-manager metrics, launch
scripts, worker JSON configs, and KV-manager environment files. No credentials
are stored in those files.

## Processes and networking

SSH port 22 reaches the bare-metal host; port 41338 reaches its runner container.
Both runner containers use bridge networking with private IP `172.17.0.2`.

- Model runners and d-gen workers stay inside the reservation containers,
  sharing their existing `/dev/shm`.
- KV managers run as additional Docker containers, `m4-kvm-prefill` on lb17
  and `m4-kvm-decode` on lb08, using host networking and real `dmk` device I/O.
- The KV-manager image is `kv-manager:m4-lb-disagg`, image ID `7a4aa96ee27c`,
  labeled with d-gen revision `ba4c33e33752ee19223b2d219c319dd4ed703b8d`.
- KV transport is Mooncake TCP. Its dynamically assigned RPC and data ports
  are reachable directly through host networking.
- etcd runs on lb08 at `10.250.36.68:2379`.
- NATS runs in `m4-disagg-nats` on lb08 at `10.250.36.68:4222`.
  Both Dynamo request and event planes use NATS.
- The frontend runs directly on lb08, port 8000, with `--enforce-disagg`.
- Host-side TCP relays forward `19071` and `19101` to the same ports in each
  reservation container. Port 19071 is engine KV coordination; 19101 is the
  configured Dynamo response-stream port.
- The prefill KV-manager command ingress is `tcp://10.250.36.143:9093`.
  Both managers use port 18650 for peer coordination and 18081 for health.

Worker launchers set `DYN_SELF_HOST_METADATA=0`: model metadata uses shared
home storage. Advertising the default container HTTP metadata URL breaks
cross-host discovery because both containers have the same private IP.

## Configuration and logs

Let `R=/home/aknezevic/disagg_lb_runtime/current`.

| File under R | Purpose / execution location |
|---|---|
| `runner_prefill.yaml` | SP=2, TP=4, chunk=5120, capacity=10240, one user; migration/file export enabled |
| `start-prefill.sh` | Prefill runner; execute inside lb17's reservation container |
| `dynamo.prefill.json`, `start-worker-prefill.sh` | Prefill d-gen worker; same container |
| `dynamo.decode.json`, `start-worker-decode.sh` | Decode d-gen worker; inside lb08's reservation container |
| `kvm.prefill.env`, `start-kvm-prefill.sh` | KV manager; execute launcher on lb17 bare metal |
| `kvm.decode.env`, `start-kvm-decode.sh` | KV manager; execute launcher on lb08 bare metal |
| `start-frontend.sh` | Frontend; execute on lb08 bare metal |
| `tcp_forward.py` | Host-side TCP relay |

Prefill log:
`/localdev/aknezevic/runtime-mistral4-prefill/runner-disagg.log` on lb17.
Other process logs are under `/home/aknezevic/disagg_lb_runtime/logs/`:
`current-prefill-worker-v2.log`, `current-decode-worker-v2.log`,
`current-frontend.log`, `current-*-proxy.log`, and `decode-ring.log`.
Use `sudo docker logs m4-kvm-prefill` / `m4-kvm-decode` on the respective
hosts for KV-manager logs.

Both live tables and device maps are under
`/home/aknezevic/disagg_lb_runtime/kv_tables/`.
`KV_MANAGER_TABLE_HOST` must match the corresponding runner container's
hostname, because that identity was encoded in its table. The table hosts
are `host-29e34bdf` (prefill) and `host-274f8b09` (decode).

## Health and test

```bash
curl -fsS http://10.250.36.143:18081/health
curl -fsS http://10.250.36.68:18081/health
curl -fsS http://10.250.36.68:8000/v1/models
curl -fsS http://10.250.36.68:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data-binary @/home/aknezevic/disagg_lb_runtime/current/fresh-request.json
```

Do not run the standalone prefill smoke producer or consume its layer-ack
channel while the d-gen worker owns it. Do not reset devices while either
runner or KV manager is active.

## Restart order

This is a manual bring-up, not a reboot-persistent service deployment.
Do not launch duplicate workers or managers against the same runner.

1. Stop/drain the d-gen workers before stopping KV managers or model runners.
   Send SIGTERM to the verified worker PID; prefill's drain can take about
   30 seconds. Worker shutdown does not stop the hand-launched decode ring.
2. If a model runner restarted, regenerate its KV table and device map before
   restarting both KV managers. Old device addresses must not be reused.
   Prefill's local `runtime-mistral4-prefill/start-runner.sh` now launches the
   migration-enabled manifest; the standalone launcher was saved separately.
3. Ensure etcd and NATS are running. `sudo docker start m4-disagg-nats`
   restarts the existing NATS container if stopped.
4. After the tables are ready, `sudo docker start m4-kvm-prefill` on lb17 and
   `sudo docker start m4-kvm-decode` on lb08 restart existing stopped managers.
   Use the `start-kvm-*.sh` scripts only when their containers do not exist.
   Both `/health` endpoints must become healthy.
5. Ensure the host-side relays are running. Each uses this command, with
   `HOST_IP` set to that host's 10.250.36.x address and `PORT` to 19071 or 19101:

   ```bash
   python3 /home/aknezevic/disagg_lb_runtime/current/tcp_forward.py \
     --listen HOST_IP:PORT --target 172.17.0.2:PORT
   ```

   Re-check the reservation container's IP after container recreation.
6. Run `start-worker-prefill.sh` inside lb17's reservation container and
   `start-worker-decode.sh` inside lb08's. Run `start-frontend.sh` on lb08
   only if the frontend is not already running. For a detached launch, use
   `setsid SCRIPT > LOG 2>&1 < /dev/null &` and record `$!`.
7. Check health and rerun the fresh request above.

The old fleet config under lb08's `run/mistral4/disagg/` assigns the opposite
roles. Do not use it for this deployment.
