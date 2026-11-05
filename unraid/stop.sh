#!/bin/bash
set -eu
FILE=/run/observatory-unraid/agent.pid
if [[ -r $FILE ]]; then
 PID=$(cat "$FILE")
 if [[ $PID =~ ^[0-9]+$ ]] && [[ -r /proc/$PID/cmdline ]] && tr '\0' ' ' < /proc/$PID/cmdline | grep -q 'observatory-unraid/collector.sh';then
  GROUP=$(ps -o pgid= -p "$PID" | tr -d ' ')
  if [[ $GROUP == "$PID" ]];then kill -- "-$PID";else kill "$PID";fi
 fi
 rm -f "$FILE"
fi

# Wait for any child from an older collector release to relinquish its lock.
flock -w 45 /run/observatory-unraid/agent.lock true
