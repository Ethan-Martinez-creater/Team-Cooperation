# Single-host production deployment

This bundle targets one Ubuntu 22.04 host without Kubernetes. It deliberately
uses names, ports, databases, services and storage separate from any legacy
`/opt/coifesp` installation.

- Compose project: `team-cooperation-prod`
- PostgreSQL: container-only data volume, loopback host port `5433`
- Keycloak: loopback `8180`, published through Nginx at `/auth`
- Control plane: systemd, loopback `8020`
- Agent Worker: one bounded shared multi-tenant systemd process
- Tool Worker: one bounded shared multi-tenant systemd process using rootless
  Podman; never mount `/var/run/docker.sock` and never run it privileged
- GitHub adapter: optional loopback `8011`, published at `/github-adapter`

Filled `infrastructure.env`, `app.env`, `github-adapter.env`, generated realm
imports and credentials are deployment Secrets. Keep them outside Git with mode
`0600`. Generate independent random values; do not reuse audit, envelope,
memory, database, OIDC-client or GitHub-adapter keys.

Place each immutable source snapshot under `/opt/team-cooperation/releases/<sha>`
and atomically point `/opt/team-cooperation/current` at the selected release.
The systemd units use only `current`; do not overwrite a previous release in
place. This keeps application rollback independent of database and secret data.

Bring up PostgreSQL first, then Keycloak. Run `alembic upgrade head` exactly once
with the application environment before enabling the four systemd units. Install
`nginx-http.conf` under a new site name, replace `__PUBLIC_HOST__`, validate with
`nginx -t`, and only then replace the placeholder site's enabled symlink. Obtain
a trusted TLS certificate before using `COIFESP_ENV=production`; every OIDC and
connector URL must use the same HTTPS origin.

The database volume is not deleted during ordinary rollback. Rollback disables
only `team-cooperation-*` units, restores the previous Nginx enabled-site
snapshot, and stops this Compose project. Never delete `/opt/coifesp`, its
systemd units, Nginx site, PostgreSQL cluster or database.

Deployment acceptance requires:

1. `docker compose ps`, Keycloak realm discovery, and `alembic current` succeed.
2. `/health/live` and `/health/ready` return 200 through HTTPS.
3. all service units run as `teamcoop`; only Nginx exposes public ports.
4. Agent and Tool Workers use plural tenant allowlists and distinct OIDC clients.
5. Tool execution proves rootless Podman, `--network none`, a read-only root,
   dropped capabilities and bounded resources; otherwise keep Tool Worker off.
6. OIDC PKCE login, one project conversation, cross-team exchange, durable
   recovery and the approved GitHub test-repository workflow complete.
