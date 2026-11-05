# Proxmox Observatory

A self-hosted sensor dashboard with graphs, plus outbound-only collectors for Proxmox VE and Unraid hosts. The dashboard runs in an unprivileged Docker container. The collector runs on Proxmox and performs read-only queries.

**This repository does not install, replace, stop, or modify an existing monitoring service.** Installation is explicit. The dashboard defaults to port **9106**, allowing an older dashboard on 9105 to remain in place.

## What it monitors

| Source | Measurements |
| --- | --- |
| Host | CPU utilization, every logical CPU, RAM, load, uptime |
| Linux hardware sensors | CPU/core temperatures, drive and NIC temperatures, and other numeric sensors exposed by `lm-sensors` |
| Local BMC / IPMI | Temperature, fan RPM, power, voltage, current, and discrete hardware status when exposed by the BMC |
| Drives | SATA/NVMe model, serial, firmware, temperature, NVMe sensor temperatures, endurance, spare capacity, lifetime errors and selected ATA counters |
| ZFS | Pool state, allocated/free raw space and usable dataset availability |
| Network / drive I/O | Receive/transmit rates, interface errors/drops, drive read/write rates |
| Proxmox | Running/stopped state, CPU and memory for VMs and LXCs |
| Docker | Container state, health description, CPU and memory from Docker inside one configurable LXC |
| NVIDIA | GPU utilization, temperature, memory, power and fan when supported |
| UPS | NUT battery charge/runtime, load, input and battery voltage, UPS power when reported |

Availability depends on hardware, permissions, drivers, and installed tools. Unsupported readings are omitted; failed sources are visible and are not plotted as zero. Historical SMART counters are lifetime totals, not proof of a current failure. Temperature limits shown are reported by the device; this project does not change them.

### Graphs and selection

- Last hour, 6 hours, 24 hours or 7 days; configurable retention.
- Search metrics; click any card for a larger graph; export the current view to CSV.
- Open **Settings → Sensors** to disable whole collectors or individual sensors.
- A disabled collector is **not polled** after the agent fetches the new settings.
- Deselecting an individual sensor filters its future values before upload and storage. Its source may still be queried to collect other readings (for example, one SMART query supplies several sensors).
- Disabled readings are hidden from historical graph responses too. Old records are retained until normal expiry; re-enabling reveals historical data again.
- Sensor settings are separate for each host and persist in its data volume. Re-enable a collector to discover sensors added later.
- Shorter charts use the stored samples. Long ranges reduce points using the **maximum observed value in each bucket**, preserving spikes. These are sampled peaks, not time-weighted averages.
- Graph history starts when the new collector connects. This package does not automatically copy an existing dashboard's database or SMART logs.

## Architecture

```text
Proxmox host                            Docker host
┌────────────────────────────┐          ┌─────────────────────────────┐
│ Python collector (root)    │  HTTPS   │ Dashboard / receiver        │
│ • reads local sensors      ├─────────►│ • non-root UID 10001        │
│ • no listening TCP port    │ outbound │ • no Docker socket/devices  │
│ • reads pct / pvesh        │          │ • SQLite volume /data       │
└────────────────────────────┘          └──────────────┬──────────────┘
                                                     │ browser
                                          Password-protected dashboard
```

There are no cloud services, analytics, external JavaScript dependencies, SSH keys, or remote-command endpoints. Root is needed by the host collector for SMART, local IPMI, and Proxmox/LXC inspection. Dashboard settings choose fixed collectors and sensor IDs; they cannot submit shell commands.

The initial version supports **one Proxmox host per dashboard**. Run a separate instance for another host. IPMI is local (`/dev/ipmi0`); it does not log in to iDRAC over the network. UPS data comes from the host's existing NUT server.

## 1. Prepare the Docker host

Requirements: Docker Engine and Docker Compose v2 or newer, plus Python 3 for initial secret generation. The image uses only Python's standard library; it installs no pip packages.

```sh
git clone https://github.com/kvip60/proxmox-observatory.git
cd proxmox-observatory
python3 deploy/setup-secrets.py
```

This creates missing files only:

- `.env` from `.env.example`.
- `.secrets/admin_password.txt`: random dashboard password.
- `.secrets/ingest_token.txt`: independent random collector token.

Neither secret is printed, committed, nor copied into the image. The containing directory is mode 0700 on Linux. Secret files are mode 0644 so the non-root container UID can read Compose's file-backed secret mounts; the private parent directory limits access by other host users. Use a secret manager or equivalent controls for a multi-user deployment.

Edit `.env`:

```dotenv
# Loopback default suits a reverse proxy on the Docker host.
DASHBOARD_BIND_IP=127.0.0.1
DASHBOARD_PORT=9106
ADMIN_USER=admin
RETENTION_DAYS=7
# Optional sidebar links:
PROXMOX_URL=
IDRAC_URL=
```

**Remote collector access:** loopback cannot be reached from another host. Put a TLS reverse proxy in front of the service and use its HTTPS URL in the collector, or bind `DASHBOARD_BIND_IP` to the Docker host's LAN IP. If a reverse proxy runs in another container, use an appropriate Docker network/service address or reachable LAN binding. Do not bind to an IP absent from the Docker host.

For direct LAN access, HTTP transmits the dashboard password and token without encryption. Use it only on a trusted isolated network; HTTPS is recommended. Do not expose this application directly to the Internet. Accounts support administrator and viewer roles, hashed passwords, twelve-hour sessions, and login rate limiting. Use an authenticated reverse proxy for broader access; MFA is not built in. Preserve the incoming `Host` header when proxying, so settings-origin checks work.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:9106/healthz
```

If bound to a LAN IP, use that IP instead of `127.0.0.1` for the host-side `curl` command. Container health checks always use the container's own loopback.

Open `http://DOCKER_HOST:9106` (or your HTTPS proxy URL). Sign in using `ADMIN_USER` and the password stored in `.secrets/admin_password.txt`. Read that file locally; do not paste it into issues or chat. The page will initially say it is waiting for the collector.

The container drops all capabilities, uses a read-only root filesystem, and stores data only in the named `observatory_data` volume. No privileged mode, host PID/network mode, Proxmox filesystem mount, device mount, or Docker socket is required.

## 2. Install the collector on Proxmox

Copy or clone this repository onto the Proxmox host. The collector installer is separate from the Docker deployment.

### Dependencies

Core collection needs Python 3 and standard Proxmox tools (`pvesh`, `pct`, `zpool`, `zfs`). Optional sources need:

```sh
apt-get update
apt-get install --no-install-recommends smartmontools lm-sensors ipmitool
```

- NUT: configure the UPS separately and confirm `upsc UPS_NAME@localhost` works. Installing `nut-client` supplies `upsc` if needed; this project does not rewrite your UPS or shutdown configuration.
- GPU: `nvidia-smi` must already work on Proxmox. This project does not install drivers or configure passthrough.
- Docker: Docker must be available inside the chosen LXC, and `pct exec CT_ID -- docker stats --no-stream` must work. This collector does not SSH to a separate Docker VM. Disable Docker collection if the host does not use that arrangement.
- IPMI: the local BMC device must be available. Disable that source on hardware without IPMI. Do not run `sensors-detect` blindly on a production server; the project only reads existing sensors.

Install files without starting the collector:

```sh
cd /path/to/proxmox-observatory
sh deploy/install-collector.sh
```

The script creates only these new locations and refuses to overwrite an existing collector service:

```text
/opt/proxmox-observatory-collector/
/etc/proxmox-observatory/collector.json
/var/lib/proxmox-observatory/
/etc/systemd/system/proxmox-observatory-collector.service
```

Edit `/etc/proxmox-observatory/collector.json`:

```json
{
  "dashboard_url": "https://YOUR_DASHBOARD_HOST",
  "token_file": "/etc/proxmox-observatory/ingest_token",
  "interval_seconds": 30,
  "spool_dir": "/var/lib/proxmox-observatory/spool",
  "disabled_sources": [],
  "options": {
    "docker_ct_id": 104,
    "ups_name": "UPS_NAME@localhost"
  }
}
```

Use the actual Docker LXC ID and NUT UPS name. `upsc -l localhost` lists UPS identifiers. The source names accepted by `disabled_sources` are:

```text
Host & Linux sensors
BMC / IPMI
Drive SMART
ZFS
Proxmox guests
Docker
UPS
GPU
```

Host-file exclusions take precedence over web settings; the UI cannot turn those collectors on. For example, use `"disabled_sources": ["GPU", "UPS"]` on a host without a GPU or UPS.

Copy **only the ingestion token**, not the administrator password, from the Docker host to Proxmox. Run this on the Docker host, replacing the destination:

```sh
scp .secrets/ingest_token.txt root@PROXMOX_HOST:/etc/proxmox-observatory/ingest_token
```

On Proxmox:

```sh
chmod 600 /etc/proxmox-observatory/collector.json /etc/proxmox-observatory/ingest_token
python3 /opt/proxmox-observatory-collector/agent.py --config /etc/proxmox-observatory/collector.json --once
systemctl enable --now proxmox-observatory-collector
journalctl -u proxmox-observatory-collector -n 30 --no-pager
```

`--once` performs one real collection and upload attempt. Inspect its log for upload errors; it queues samples if the receiver is unreachable. The minimum interval is 15 seconds; 30–60 seconds is recommended. CPU and I/O rates require two samples. Give initial collection up to a minute before checking every panel.

No inbound collector port needs opening. Allow Proxmox outbound access to the receiver/proxy, and allow your browsers to reach the dashboard. HTTPS certificate validation is enabled; there is no insecure-certificate bypass switch. Add a private CA to the host trust store when needed.

## 3. Configure sensor selection

1. Wait for the first sample, then click **Settings → Sensors**.
2. Uncheck a whole collector to stop querying it on the next poll.
3. Search and uncheck individual sensor IDs to stop transmitting/storing their values and hide their graphs.
4. Use **Select visible / Deselect visible** to apply a selection to the filtered list.
5. Click **Save selection**. Settings are saved immediately; the collector fetches them each cycle.

Disabled-source badges are intentional, not failures. Unavailable sources show their collection error type. The agent never substitutes stale values for missing new readings. If telemetry stops, the page indicates stale/offline status.

## Data, backup, upgrade, and rollback

- Dashboard: SQLite database in `/data/history.db`, including history, sensor catalog, latest snapshot, and selections. Seven-day retention by default. Data older than the configured period expires on new ingestion; deletion does not immediately shrink the SQLite file.
- Collector: queues up to one day of samples locally during receiver outages, then retries in bounded batches. Samples are keyed by integer timestamp; a repeated sample in the same second replaces the earlier one. Older queued data cannot replace the dashboard's newest sample.
- One source failing does not prevent the others from uploading.
- No existing monitoring history is migrated automatically. Existing monitors can run alongside this project, at the cost of additional polling.

To back up the Docker data, stop the dashboard briefly and copy/export the named volume with all database files. Alternatively, use SQLite's online backup API. A live copy of `history.db` alone may miss its WAL contents. Back up `.env` and `.secrets` separately in protected storage.

Dashboard upgrade:

```sh
git pull --ff-only
docker compose up -d --build
```

Collector upgrade (after updating the checkout on Proxmox):

```sh
systemctl stop proxmox-observatory-collector
install -m 0644 collector/agent.py collector/collectors.py /opt/proxmox-observatory-collector/
systemctl start proxmox-observatory-collector
```

Keep the prior image/source and a database backup for rollback. To disable this new deployment while preserving its data:

```sh
systemctl disable --now proxmox-observatory-collector   # on Proxmox
docker compose down                                 # on Docker host
```

Do **not** add `--volumes` to `docker compose down` unless you intend to delete history and selections. These commands do not stop an unrelated existing Observatory service. If you later migrate from an older implementation, validate this deployment first and decide separately whether to stop the old one.

### Credentials

The UI uses a login form with HttpOnly, SameSite sessions. Existing Basic authentication clients remain compatible. On upgrade, the existing administrator credentials are migrated into hashed accounts without resetting them. Subsequent account changes live in SQLite; editing the bootstrap password file does not reset an existing account. Collector ingestion/configuration uses separate per-host bearer tokens. A token cannot view dashboard history, manage accounts, or write settings. Never commit secrets, live collector configurations, databases, or telemetry exports.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Dashboard waiting for collector | URL, LAN binding, firewall, HTTPS trust, matching ingestion-token files, collector journal |
| Cannot sign in | Check username; use the recovery command below if needed. Proxy must preserve cookies and `Host`. |
| Sensor selection save rejected | Reverse proxy must preserve `Host`; use the same origin for page and API |
| CPU/I/O graphs initially absent | Wait for the second successful sample |
| IPMI unavailable | `ipmitool sensor -c`, local BMC access; unsupported sensors can be discrete rather than numeric |
| SMART unavailable | `smartctl -a -j /dev/DRIVE`; RAID controllers/USB bridges may need unsupported device-specific arguments |
| Docker unavailable | Correct LXC ID, running guest, working `pct exec CT_ID -- docker stats --no-stream` |
| UPS unavailable | `upsc -l localhost`, configured UPS name and working local NUT service |
| No GPU data | `nvidia-smi` must work on the host; unsupported GPU fields are omitted |
| Secret permission denied in container | Use the setup script's secret-file modes and a private parent directory; inspect bind-mount access policies |
| History lost after redeploy | Reuse the same Compose project and named data volume |

## Development and verification

```sh
python3 -m unittest discover -s tests -v
node --check dashboard/static/app.js
sh -n deploy/install-collector.sh
docker build -t observatory:test .
```

Tests use temporary local files and synthetic metrics, not your Proxmox host. They cover credential separation, settings/CSRF checks, disabled sources and sensors, history peak retention, queued-upload recovery, and out-of-order samples. CI also builds the container and checks its health/authentication. No tests run hardware updates, change temperatures, reboot hosts, or install into production.

This is a small custom monitor, not a replacement for backup validation, hardware diagnostics, or a dedicated alerting system. It displays source status and reported thresholds but does not send email/push alerts or perform automatic recovery/shutdown.


## Settings, accounts and appearance

Open **Settings** from the dashboard. **Sensors** lets administrators choose the host, disable entire sources, search/filter individual sensors, and select or deselect the visible set. Sensor selections affect everyone viewing that host. The dashboard host selector switches graphs and history independently between registered collectors.

**Appearance** offers Dark Slate, Light Paper, Midnight, and System themes, six accent presets, and a custom colour picker with an immediate preview. Press Save to persist it for your account. Colours are adjusted for readable contrast. **My account** changes your username or password after verifying your current password. Passwords require 16–256 characters. **Users** lets administrators add or edit administrators and viewers; viewers can view monitoring and change their own appearance/account, but cannot manage sensors, users, or collectors. The last administrator cannot be demoted. Password changes revoke previous sessions.

Keep the data volume when updating: it contains accounts, preferences, host registrations, history and selections. For local administrator password recovery, run the interactive command below on the Docker host; it prompts without echoing the new password:

```sh
docker compose exec dashboard python manage.py reset-password admin
```

For HTTPS deployments set `SESSION_COOKIE_SECURE=1` in the dashboard environment, then recreate the container. Plain HTTP is suitable only for a trusted private network.

## Add a separate Unraid collector

1. Sign in as administrator and open **Settings → Hosts & collectors**. Choose a unique slug such as `unraid`, a label, platform **Unraid**, and optional host address. Confirm with your current administrator password. The new ingestion token is displayed only once: save it locally and keep it private.
2. Copy the three shell scripts in `unraid/` to `/boot/config/plugins/observatory-unraid/` on Unraid. Copy `collector.example.json` there as `collector.json`. The native collector uses the tools already supplied by Unraid: Bash, jq, curl, smartctl, lm-sensors, Docker, and optionally ZFS/NUT. Python and a privileged collector container are unnecessary.
3. Edit the JSON locally: set `dashboard_url` to your reachable dashboard URL, `collector_id` to the registered slug, and `ups_name` to your local NUT instance (often `ups@localhost`). Save the ingestion token in the configured `ingest_token` file. Keep the flash configuration private; FAT filesystems do not enforce Unix permissions. Do not place credentials on a public share.
4. Start it with `bash /boot/config/plugins/observatory-unraid/start.sh`. Check `/var/log/observatory-unraid.log`. Add this line once to `/boot/config/go`, preserving existing contents, for reboot persistence:

```sh
bash /boot/config/plugins/observatory-unraid/start.sh
```

Stop with `bash /boot/config/plugins/observatory-unraid/stop.sh`. The process lock prevents duplicate collectors. Select the host in the dashboard; a first complete cycle can take a minute. CPU/network rates need two samples.

Unraid collects host CPU/core and memory, Linux sensors, network rates/errors, SMART (without waking standby HDDs), native Docker CPU/memory/status, array status/parity activity, mounted array/cache storage, ZFS pools, and NUT UPS metrics. It does not query Proxmox guests or IPMI. Unavailable sources are shown as unavailable. A host has its own token, graph history, and sensor choices. The native agent caches choices in RAM and uploads fresh samples each cycle; unlike the Python Proxmox agent, it does not spool an outage backlog. Use HTTPS when crossing untrusted networks.
