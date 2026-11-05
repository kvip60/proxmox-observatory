#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then echo 'Run as root on the Proxmox host.' >&2; exit 1; fi
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -f /etc/systemd/system/proxmox-observatory-collector.service ]; then
  echo 'Collector already installed. Use the README upgrade procedure.' >&2; exit 1
fi
install -d -m 0755 /opt/proxmox-observatory-collector
install -d -m 0700 /etc/proxmox-observatory /var/lib/proxmox-observatory
install -m 0644 "$ROOT/collector/agent.py" "$ROOT/collector/collectors.py" /opt/proxmox-observatory-collector/
if [ ! -f /etc/proxmox-observatory/collector.json ]; then
  install -m 0600 "$ROOT/deploy/collector.example.json" /etc/proxmox-observatory/collector.json
fi
install -m 0644 "$ROOT/deploy/proxmox-observatory-collector.service" /etc/systemd/system/
systemctl daemon-reload
echo 'Installed but NOT started. Configure collector.json and copy ingest_token, then follow README.'
