#!/bin/bash
set -eu
DIR=$(cd -- "$(dirname -- "$0")" && pwd)
mkdir -p /run/observatory-unraid
if [[ -f /run/observatory-unraid/agent.pid ]] && kill -0 "$(cat /run/observatory-unraid/agent.pid)" 2>/dev/null;then echo 'Collector already running';exit 0;fi
nohup flock -n -F /run/observatory-unraid/agent.lock bash "$DIR/collector.sh" "$DIR/collector.json" >> /var/log/observatory-unraid.log 2>&1 < /dev/null &
echo $! > /run/observatory-unraid/agent.pid
echo 'Unraid collector started. Log: /var/log/observatory-unraid.log'
