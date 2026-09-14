# Kubernetes production runtime baseline

This directory deploys the existing COIFESP control-plane application factory,
durable Agent Worker, and durable Tool Worker without changing their API or
protocol contracts. It is a production baseline, not a Secret manager, database,
identity provider, ingress controller, metrics adapter, or image build.

## Layout

- `base/`: namespaced runtime objects, zero-access service accounts/RBAC,
  default-deny networking, PDBs, security contexts, probes, resources, and the
  control-plane HPA.
- `overlays/production/`: HA replica counts and mandatory image substitutions.
- `autoscaling/`: opt-in KEDA examples for lease-aware Worker scale-out. They are
  deliberately excluded from Kustomize until their metrics contract is met.

Kubernetes 1.33 or newer is required. The Tool Worker uses the stable native
sidecar API so the rootless sandbox engine must be ready before the worker can
claim a job.

## Required replacements

Before deployment, replace every `example.invalid`, `replace-me`, and placeholder
digest. Both the harness and rootless-engine images must support non-root,
read-only operation at the declared UIDs. The Tool Worker image must contain the
`docker` client. The sandbox profile image must also stay pinned by digest.

Create these Secrets out of band (External Secrets, sealed delivery, or your
platform Secret manager); no Secret object or material is committed here:

| Secret | Required keys |
| --- | --- |
| `coifesp-control-plane-secrets` | `database-url`, `audit-signing-key`, `envelope-signing-key`, `memory-master-key`, `metrics-bearer-token` |
| `coifesp-agent-worker-secrets` | the four keys above plus `worker-client-secret`, `directory-client-secret`, `llm-api-key` |
| `coifesp-tool-worker-secrets` | the four keys above plus `tool-worker-client-secret` |

The three database URLs should use independently scoped, non-superuser,
non-`BYPASSRLS` roles. The application readiness check also requires the exact
Alembic revision and forced RLS/audit triggers expected by the checked-out code.
Do not run migrations in these Deployments.

The sample configures both Worker deployments as bounded multi-tenant shared
pools through `COIFESP_WORKER_TENANTS` and `COIFESP_TOOL_WORKER_TENANTS`. Replace
the comma-separated examples with the exact authorized tenant set. Keep both
sets equal unless a reviewed deployment topology intentionally prevents some
tenants from using tools. Do not add the legacy singular variables alongside
the plural variables: `COIFESP_WORKER_TENANT_ID` and
`COIFESP_TOOL_WORKER_TENANT_ID` remain runtime compatibility inputs only for
existing single-tenant installations.

The OAuth clients identify platform Worker services, not a permanent tenant.
Each durable Run or Job carries the authoritative tenant, and a Worker may claim
it only when that tenant is in its configured pool allowlist. Horizontal
replicas consume the same pool; do not create one Deployment per account. For
key rotation, put the keyring JSON metadata in a Secret too, because it contains
Secret environment variable names; never place key material in a ConfigMap.

Replace all identity/model URLs and client/tenant IDs in the ConfigMaps. In
production those URLs must be HTTPS. Keep Agent Worker, Tool Worker, and directory
OAuth clients distinct. The base enables HTTPS Trace export to the separately
deployed Collector assets in `deploy/observability`; mount its receiver CA in the
harness image trust store. Control-plane metrics require the distinct
`metrics-bearer-token` Secret and should be scraped only through an authenticated
monitoring integration. Worker metrics remain disabled until their process entry
points expose an authenticated scrape endpoint.

## Network and sandbox prerequisites

The default-deny policy permits DNS plus ports 443/5432 only to namespaces labeled
`coifesp.dev/runtime-dependency=true`. Label only namespaces that contain the
approved PostgreSQL, identity, directory, LLM/connector, telemetry, artifact
registry, or Secret egress proxy endpoints. The PostgreSQL tables are the durable
queues; this slice intentionally does not introduce a second queue with weaker
fencing semantics. Because a standard `NetworkPolicy` cannot safely allow an
arbitrary external FQDN, external dependencies should be reached through a
namespace-local egress gateway and enforced by a NetworkPolicy-capable CNI.

Label only the ingress-controller namespace with
`coifesp.dev/control-plane-ingress=true`. In-cluster blackbox probes, if used,
must run in this namespace with `app.kubernetes.io/name=coifesp-probe`. TLS must
terminate at the ingress/gateway; the included Service remains `ClusterIP` and
does not publish the API directly.

Provision `RuntimeClass/coifesp-sandbox` with a runtime that safely supports
rootless nested containers and user namespaces. Validate it with the cluster and
runtime vendor, including that the rootless daemon preserves the default
container seccomp profile. Never substitute privileged containers, host
PID/network, a hostPath, or `/var/run/docker.sock`. The rootless daemon disables its bridge and
iptables; sandbox jobs additionally execute with `--network none`, read-only root,
all capabilities dropped, no-new-privileges, non-root UID, and explicit
CPU/memory/PID/output/time limits. Admission policy should pin the allowed sandbox
images and reject mutation of these settings.

## Lease-safe scaling and disruption

Each Worker process claims at most one durable item at a time. Heartbeats fence
the claim, but SIGTERM waits for the current item instead of releasing it. The
baseline therefore uses two replicas, `maxUnavailable: 0`, a PDB, and a 660-second
grace period (longer than the default maximum model/tool call plus cleanup). If an
administrator allows longer sandbox profiles (up to 3600 seconds), increase the
Tool Worker grace period above the configured maximum plus cleanup margin.

Do not attach CPU/memory HPA to Workers and do not scale them to zero. Those
signals do not represent claimable work and a scale-down can terminate an active
lease. The optional KEDA objects permit bounded scale-out only and explicitly
disable scale-down. Their Prometheus metrics must be produced from a
transactionally consistent query with all of these properties:

- aggregated only across the exact tenant allowlist of the Worker deployment;
- counts only rows claimable **now**, including retry/backoff eligibility;
- excludes active non-expired leases, running work, approvals, dependencies,
  cancelled and terminal rows;
- monotonically non-negative, fresh, and unavailable on query failure (never
  silently reported as zero).

Manual scale-down is a drain operation: stop new claims for the chosen replicas,
wait for active work to checkpoint/finish, verify no lease owner matches them,
then reduce replicas. Scaling up is safe because claims use database fencing.
The optional KEDA example uses a tenant-label regular expression and `sum` to
represent total claimable backlog for the shared pool. Its tenant set must be
updated whenever the corresponding ConfigMap allowlist changes.

## Validation and rollout

Offline validation requires only Python and PyYAML already declared by this
project:

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_kubernetes_assets.py
E:\miniconda3\envs\bettafish\python.exe -m pytest tests\test_kubernetes_assets.py
kubectl kustomize deploy/kubernetes/overlays/production
```

The validator intentionally fails while placeholder registries, values, or
digests remain. For structural CI with placeholders, use `--allow-placeholders`.
It will use `kubectl kustomize` automatically when available. A client-only
`kubectl apply --dry-run=client` still performs discovery in many kubectl builds;
run client/server dry-run against the target cluster as a deployment gate after
installing the RuntimeClass and any optional KEDA CRDs:

```sh
kubectl kustomize deploy/kubernetes/overlays/production > rendered.yaml
kubectl apply --dry-run=server -f rendered.yaml
```

Apply the rendered output only after the strict validator passes, external
Secrets exist, namespace labels and RuntimeClass are ready, and the database
schema/security readiness check passes. Then wait for all three Deployments to
roll out and confirm `/health/ready` through the authorized ingress path.
