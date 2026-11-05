#!/bin/bash
# Outbound-only native Unraid collector. Requires existing jq, curl and system tools.
set -u
CONFIG=${1:-/boot/config/plugins/observatory-unraid/collector.json}
BASE=$(jq -er '.dashboard_url' "$CONFIG"); BASE=${BASE%/}
CID=$(jq -er '.collector_id' "$CONFIG")
[[ $CID =~ ^[a-z][a-z0-9-]{1,39}$ ]] || exit 1
TOKENFILE=$(jq -er '.token_file' "$CONFIG")
INTERVAL=$(jq -r '.interval_seconds // 30' "$CONFIG")
[[ $INTERVAL =~ ^[0-9]+$ ]] || exit 1
(( INTERVAL >= 15 )) || INTERVAL=15
WORK=/run/observatory-unraid
mkdir -p "$WORK"; chmod 700 "$WORK"; umask 077
TOKEN=$(tr -d '\r\n' < "$TOKENFILE")
[[ ${#TOKEN} -ge 32 && $TOKEN =~ ^[A-Za-z0-9_-]+$ ]] || exit 1
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$WORK/auth.conf"
unset TOKEN
[[ -f $WORK/selection.json ]] || printf '{"disabled_metrics":[],"disabled_sources":[]}' > "$WORK/selection.json"
get_url() { curl --config "$WORK/auth.conf" --fail --silent --show-error --max-time 15 "$1"; }
metric() {
 local key=$1 label=$2 val=$3 unit=$4 group=$5 entity=${6:-}
 [[ $val =~ ^-?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$ ]] || return 0
 val=$(awk -v v="$val" 'BEGIN {printf "%.12g",v}')
 jq -nc --arg k "$key" --arg l "$label" --argjson v "$val" --arg u "$unit" --arg g "$group" --arg e "$entity" --arg s "$SOURCE" '{key:$k,metric:{label:$l,value:$v,unit:$u,group:$g,entity:$e,source:$s,warn:null,crit:null}}' >> "$WORK/metrics.ndjson"
}
entity() { jq -nc --arg k "$1" --arg n "$2" --arg st "$3" --arg d "$4" --arg g "$5" --arg s "$SOURCE" '{key:$k,entity:{name:$n,status:$st,detail:$d,group:$g,source:$s}}' >> "$WORK/entities.ndjson"; }
host_collect() {
 entity host "Unraid · $(hostname)" running "$(uname -r)" Host
 if [[ -f $WORK/cpu.previous ]]; then
  awk -v source="$SOURCE" 'FNR==NR {if ($1 ~ /^cpu/) {for(i=2;i<=9;i++) total[$1]+=$i; idle[$1]=$5+$6} next} $1 ~ /^cpu/ {t=0;for(i=2;i<=9;i++)t+=$i;dt=t-total[$1];if(dt<=0)next;v=100*(1-(($5+$6)-idle[$1])/dt);key=$1=="cpu"?"host.cpu":"host."$1;label=$1=="cpu"?"CPU utilization":"Logical CPU "substr($1,4);group=$1=="cpu"?"Host":"CPU cores";printf "{\"key\":\"%s\",\"metric\":{\"label\":\"%s\",\"value\":%.4f,\"unit\":\"%%\",\"group\":\"%s\",\"entity\":\"%s\",\"source\":\"%s\"}}\n",key,label,v,group,$1,source}' "$WORK/cpu.previous" /proc/stat >> "$WORK/metrics.ndjson"
 fi
 cp /proc/stat "$WORK/cpu.previous"
 local total available used
 total=$(awk '/^MemTotal:/{print $2}' /proc/meminfo);available=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo);used=$((total-available))
 metric host.memory 'Memory used' "$(awk -v u="$used" -v t="$total" 'BEGIN{print 100*u/t}')" '%' Host
 metric host.memory_gb 'Memory used' "$(awk -v u="$used" 'BEGIN{print u/1048576}')" GiB Host
 metric host.load 'Load average' "$(awk '{print $1}' /proc/loadavg)" '' Host
 metric host.uptime 'Host uptime' "$(awk '{print $1/3600}' /proc/uptime)" hours Host
 if sensors -j > "$WORK/sensors.json" 2>/dev/null; then
  jq -c --arg src "$SOURCE" 'to_entries[]|. as $chip|.value|to_entries[]|select(.value|type=="object")|. as $feature|.value|to_entries[]|select(.key|endswith("_input"))|select(.value|type=="number")|. as $reading|($reading.key|split("_")[0]) as $kind|{key:("sensor."+$chip.key+"."+$reading.key),metric:{label:$feature.key,value:$reading.value,unit:(if ($kind|startswith("temp")) then "°C" elif ($kind|startswith("fan")) then "RPM" elif ($kind|startswith("power")) then "W" elif ($kind|startswith("in")) then "V" elif ($kind|startswith("curr")) then "A" else "" end),group:(if ($kind|startswith("temp")) then "Temperatures" else "Hardware sensors" end),entity:$chip.key,source:$src,warn:$feature.value[$kind+"_max"],crit:$feature.value[$kind+"_crit"]}}' "$WORK/sensors.json" >> "$WORK/metrics.ndjson"
 fi
 local now iface rx tx stamp oldrx oldtx oldtime fields
 now=$(date +%s); : > "$WORK/net.new"
 while IFS=: read -r iface fields; do
  iface=${iface// /}; [[ -n $iface && $iface != lo && $iface != veth* && $iface != docker* && $iface != br-* && $iface != virbr* ]] || continue
  read -ra values <<< "$fields"; [[ ${#values[@]} -ge 16 ]] || continue
  rx=${values[0]};tx=${values[8]};printf '%s %s %s %s\n' "$iface" "$rx" "$tx" "$now" >> "$WORK/net.new"
  if [[ -f $WORK/net.previous ]]; then
   read -r stamp oldrx oldtx oldtime <<< "$(awk -v n="$iface" '$1==n{print;exit}' "$WORK/net.previous")"
   if [[ -n ${oldtime:-} ]] && ((now>oldtime)); then
    metric "net.$iface.rx" "$iface receive" "$(((rx-oldrx)/(now-oldtime)))" B/s Network "$iface"
    metric "net.$iface.tx" "$iface transmit" "$(((tx-oldtx)/(now-oldtime)))" B/s Network "$iface"
   fi
  fi
  metric "net.$iface.errors" "$iface errors" "$((${values[2]}+${values[10]}))" errors Network "$iface"
  metric "net.$iface.drops" "$iface drops" "$((${values[3]}+${values[11]}))" drops Network "$iface"
 done < <(tail -n +3 /proc/net/dev)
 mv "$WORK/net.new" "$WORK/net.previous"
}
smart_collect() {
 local dev serial model status
 for dev in /dev/sd? /dev/nvme*n1; do
  [[ -b $dev ]] || continue
  # Do not wake spun-down array HDDs for monitoring.
  timeout 12 smartctl -n standby -a -j "$dev" > "$WORK/smart.json" 2>/dev/null || true
  jq -e '.model_name' "$WORK/smart.json" >/dev/null 2>&1 || continue
  serial=$(jq -r '.serial_number // .device.name' "$WORK/smart.json");model=$(jq -r '.model_name' "$WORK/smart.json")
  status=$(jq -r 'if .smart_status.passed==true then "healthy" elif .smart_status.passed==false then "warning" else "unknown" end' "$WORK/smart.json")
  entity "drive.$serial" "$model" "$status" "$dev · $(jq -r '.firmware_version // ""' "$WORK/smart.json")" Drives
  jq -c --arg serial "$serial" --arg model "$model" --arg src "$SOURCE" '
   . as $d | (
    {k:"temperature",label:"temperature",value:$d.temperature.current,unit:"°C",warn:$d.temperature.op_limit_max,crit:$d.temperature.critical_limit_max},
    ($d.nvme_smart_health_information_log // {} | to_entries[] | select(.key=="percentage_used" or .key=="available_spare" or .key=="media_errors" or .key=="critical_warning" or .key=="warning_temp_time") | {k:.key,label:(.key|gsub("_";" ")),value:.value,unit:(if .key=="percentage_used" or .key=="available_spare" then "%" else "count" end)}),
    ($d.nvme_smart_health_information_log.temperature_sensors // [] | to_entries[] | {k:("sensor"+(.key|tostring)),label:("sensor "+((.key+1)|tostring)),value:.value,unit:"°C"}),
    ($d.ata_smart_attributes.table // [] | .[] | select(.id==5 or .id==187 or .id==188 or .id==197 or .id==198 or .id==199) | {k:("ata"+(.id|tostring)),label:(.name|gsub("_";" ")),value:.raw.value,unit:"count"})
   ) | select(.value|type=="number") | {key:("drive."+$serial+"."+.k),metric:{label:($model+" · "+.label),value:.value,unit:.unit,group:"Drives",entity:$serial,source:$src,warn:.warn,crit:.crit}}' "$WORK/smart.json" >> "$WORK/metrics.ndjson"
 done
}
docker_collect() {
 timeout 20 docker stats --no-stream --format '{{json .}}' > "$WORK/dockerstats" || return 1
 jq -c --arg s "$SOURCE" '. as $d | {k:"cpu",v:($d.CPUPerc|rtrimstr("%")|tonumber),l:"CPU"},{k:"memory",v:($d.MemPerc|rtrimstr("%")|tonumber),l:"memory"} | {key:("docker."+$d.Name+"."+.k),metric:{label:($d.Name+" "+.l),value:.v,unit:"%",group:"Docker",entity:$d.Name,source:$s}}' "$WORK/dockerstats" >> "$WORK/metrics.ndjson"
 timeout 10 docker ps -a --format '{{json .}}' | jq -c --arg s "$SOURCE" '{key:("docker."+.Names),entity:{name:.Names,status:.State,detail:.Status,group:"Docker",source:$s}}' >> "$WORK/entities.ndjson"
}
array_collect() {
 local state resync mount total used avail use
 state=$(awk -F= '$1=="mdState"{gsub(/"/,"",$2);print $2}' /var/local/emhttp/var.ini 2>/dev/null)
 entity array 'Unraid array' "${state:-unknown}" 'Array status from Unraid' Unraid
 resync=$(awk -F= '$1=="mdResync"{gsub(/"/,"",$2);print $2}' /var/local/emhttp/var.ini 2>/dev/null)
 [[ ${resync:-0} == 0 ]] && resync=0 || resync=1
 metric unraid.parity_active 'Parity operation active' "${resync:-0}" flags Unraid
 while read -r mount total used avail use; do
  [[ $mount == /mnt/user || $mount == /mnt/disk* || $mount == /mnt/cache || $mount == /mnt/*pool* ]] || continue
  entity "fs.$mount" "$mount" online 'Mounted filesystem' Storage
  metric "fs.$mount.used" "$mount used" "${use%%%}" '%' Storage "$mount"
  metric "fs.$mount.free" "$mount available" "$(awk -v a="$avail" 'BEGIN{print a/1073741824}')" GiB Storage "$mount"
 done < <(df -P -B1 | awk 'NR>1{print $6,$2,$3,$4,$5}')
}
zfs_collect() {
 command -v zpool >/dev/null || return 1
 local name size used free status
 while read -r name size used free status; do
  entity "pool.$name" "$name" "$status" 'ZFS pool' Storage
  metric "pool.$name.used" "$name pool allocated" "$(awk -v u="$used" -v t="$size" 'BEGIN{print 100*u/t}')" '%' Storage "$name"
  metric "pool.$name.free" "$name raw free" "$(awk -v a="$free" 'BEGIN{print a/1073741824}')" GiB Storage "$name"
 done < <(zpool list -Hp -o name,size,alloc,free,health 2>/dev/null)
}
ups_collect() {
 local name model status k v
 name=$(jq -r '.ups_name // "ups@localhost"' "$CONFIG")
 timeout 12 upsc "$name" > "$WORK/ups" 2>/dev/null || return 1
 model=$(sed -n 's/^ups.model: //p' "$WORK/ups");status=$(sed -n 's/^ups.status: //p' "$WORK/ups")
 [[ $status == *OL* ]] && status=online
 entity ups "${model:-UPS}" "$status" "$name" UPS
 while IFS=': ' read -r k v; do
  case "$k" in
   battery.charge) metric ups.battery.charge 'Battery charge' "$v" '%' UPS "$model";;
   battery.runtime) metric ups.battery.runtime 'Estimated runtime' "$(awk -v v="$v" 'BEGIN{print v/60}')" minutes UPS "$model";;
   ups.load) metric ups.ups.load 'UPS load' "$v" '%' UPS "$model";;
   input.voltage) metric ups.input.voltage 'Input voltage' "$v" V UPS "$model";;
   battery.voltage) metric ups.battery.voltage 'Battery voltage' "$v" V UPS "$model";;
  esac
 done < "$WORK/ups"
}
collect_source() {
 SOURCE=$1;local fn=$2
 if jq -e --arg s "$SOURCE" '(.disabled_sources // [] | index($s))!=null' "$WORK/selection.json" >/dev/null; then
  jq -nc --arg s "$SOURCE" '{key:$s,status:{ok:false,disabled:true,ts:0,error:"Disabled by configuration"}}' >> "$WORK/sources.ndjson";return
 fi
 if "$fn"; then
  jq -nc --arg s "$SOURCE" --argjson ts "$(date +%s)" '{key:$s,status:{ok:true,ts:$ts}}' >> "$WORK/sources.ndjson"
 else
  jq -nc --arg s "$SOURCE" '{key:$s,status:{ok:false,ts:0,error:"Source unavailable on this host"}}' >> "$WORK/sources.ndjson"
 fi
}
while :; do
 START=$(date +%s)
 if get_url "$BASE/api/collector-config?collector=$CID" > "$WORK/selection.new" && jq -e '(.disabled_sources|type)=="array" and (.disabled_metrics|type)=="array"' "$WORK/selection.new" >/dev/null; then mv "$WORK/selection.new" "$WORK/selection.json";fi
 : > "$WORK/metrics.ndjson"; : > "$WORK/entities.ndjson"; : > "$WORK/sources.ndjson"
 collect_source 'Host & Linux sensors' host_collect
 collect_source 'Drive SMART' smart_collect
 collect_source 'Docker' docker_collect
 collect_source 'Unraid array' array_collect
 collect_source 'ZFS' zfs_collect
 collect_source 'UPS' ups_collect
 jq -s 'reduce .[] as $m ({}; .[$m.key]=$m.metric)' "$WORK/metrics.ndjson" > "$WORK/metrics.json"
 jq -s 'reduce .[] as $e ({}; .[$e.key]=$e.entity)' "$WORK/entities.ndjson" > "$WORK/entities.json"
 jq -s 'reduce .[] as $s ({}; .[$s.key]=$s.status)' "$WORK/sources.ndjson" > "$WORK/sources.json"
 jq -n --argjson ts "$START" --slurpfile metrics "$WORK/metrics.json" --slurpfile entities "$WORK/entities.json" --slurpfile sources "$WORK/sources.json" --slurpfile settings "$WORK/selection.json" '{ts:$ts,metrics:($metrics[0]|with_entries(select(.key as $k | ($settings[0].disabled_metrics|index($k))==null))),entities:$entities[0],sources:$sources[0],catalog:($metrics[0]|with_entries(.value |= del(.value)))}' > "$WORK/pending.json"
 if ! curl --config "$WORK/auth.conf" --fail --silent --show-error --max-time 20 -H 'Content-Type: application/json' --data-binary "@$WORK/pending.json" "$BASE/api/ingest?collector=$CID" > /dev/null; then echo "$(date -Iseconds) Upload failed; next cycle will retry fresh telemetry";fi
 [[ ${OBSERVATORY_ONCE:-0} == 1 ]] && break
 ELAPSED=$(($(date +%s)-START));DELAY=$((INTERVAL-ELAPSED));((DELAY>0)) || DELAY=1;sleep "$DELAY"
done
