#!/bin/bash
set -eu
FILE=/run/observatory-unraid/agent.pid
if [[ -r $FILE ]]; then
 PID=$(cat "$FILE")
 if [[ $PID =~ ^[0-9]+$ ]] && [[ -r /proc/$PID/cmdline ]] && tr '\0' ' ' < /proc/$PID/cmdline | grep -q 'observatory-unraid/collector.sh';then kill "$PID";fi
 rm -f "$FILE"
fi
