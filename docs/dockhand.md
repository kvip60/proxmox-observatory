# Git builds and redeploys in Dockhand

Use the standalone `compose.dockhand.yaml` at the repository root for a
Dockhand Git stack. It builds the checked-out source and never needs a published
`proxmox-observatory` registry image. The Python base image still comes from
Docker Hub. The existing `compose.yaml` remains the CLI installation option.

## Preserve the existing installation

Before changing the stack, record its Compose project, data volume and secret
mounts on the Docker host (these commands show paths, not secret contents):

```sh
docker inspect proxmox-observatory-dashboard-1 --format '{{ index .Config.Labels "com.docker.compose.project" }}'
docker inspect proxmox-observatory-dashboard-1 --format '{{range .Mounts}}{{printf "%s %s %s %s\n" .Type .Name .Source .Destination}}{{end}}'
```

Find the named volume mounted at `/data`, and the existing directory containing
`admin_password.txt` and `ingest_token.txt`. Keep these files outside Dockhand's
Git checkout and synchronized stack directory: files absent from Git can be
removed during synchronization. Use a protected, persistent host directory.
Do not generate replacement secrets or commit them to Git.

Back up the database volume consistently (stop the dashboard while copying all
database files, or use SQLite's online backup API), plus the existing secrets
and environment settings. Keep the previous image/source for rollback.

## Configure the Git stack

In Dockhand, use the environment that runs the existing dashboard:

1. Add the public repository `https://github.com/kvip60/proxmox-observatory.git`,
   branch `main`. Public checkout needs no GitHub token.
2. Create/configure a Git stack with Compose path `compose.dockhand.yaml`.
   Preserve the existing Compose project name. If your Dockhand version cannot
   convert an existing stack, arrange a controlled migration: stop the old
   deployment and remove its stack registration without deleting the volume or
   secrets before deploying the replacement. Do not run two dashboards against
   the same database. Do not delete the original host checkout.
3. Store these variables in Dockhand's stack environment settings, substituting
   the actual values discovered above:

   ```dotenv
   OBSERVATORY_DATA_VOLUME=actual-existing-volume-name
   OBSERVATORY_SECRETS_DIR=/absolute/persistent/path/to/existing/secrets
   DASHBOARD_BIND_IP=127.0.0.1
   DASHBOARD_PORT=9106
   ADMIN_USER=admin
   RETENTION_DAYS=7
   ```

   Preserve your current binding, port, user, retention and optional sidebar
   URLs. The loopback example suits a local reverse proxy; direct LAN access
   needs an IP assigned to the Docker host. Configure variables in Dockhand,
   rather than committing a live `.env` file. The secrets path must be available
   to Dockhand/its deployment agent and resolve to the same files on the target
   Docker host; local Docker deployments need matching host/container paths.
   Preserve permissions allowing container UID 10001 to read the secret files.
4. Enable **Build images on deploy**. Leave **Re-pull images** off: it attempts
   to pull the application tag. If offered, **Pull base images** can refresh
   `python:3.12-slim` during builds. Leave the build cache enabled.
5. Test the Git configuration, then deploy manually once. Confirm the deployment
   log contains an image build and the dashboard becomes healthy. Check
   `/healthz`, sign in, and verify existing history and incoming collector data.
6. Only after that succeeds, enable the Git stack's automatic update/polling
   schedule (for example, every five minutes). Each detected `main` change will
   rebuild and redeploy. This is separate from registry image auto-updates;
   disable registry update checks/auto-updates for this local dashboard image.

The required volume and secrets variables have no silent defaults. The volume
is external, so a misspelled or missing volume fails deployment instead of
creating an empty history database. Existing accounts, host registrations and
sensor settings remain in that volume.

Repository files alone cannot enable a Dockhand schedule. The repository,
environment variables and build/update options must also be saved in Dockhand.
Polling works over outbound HTTPS; no public webhook endpoint is necessary.

## Rollback

Disable automatic Git updates first. Stop the new dashboard and redeploy the
previous source/image using the original project, secret paths and volume. If
an update changes the database incompatibly, restore the consistent backup
before starting the old version. Never use `docker compose down --volumes`.

References: [Dockhand manual](https://dockhand.pro/manual/),
[Compose pull policy](https://docs.docker.com/reference/compose-file/services/#pull_policy),
[external volumes](https://docs.docker.com/reference/compose-file/volumes/#external).
