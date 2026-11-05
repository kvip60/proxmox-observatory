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
  jq -c --arg src "$SOURCE" 'to_entries[]|. as $chip|.value|to_entries[]|select(.value|type=="object")|. as $feature|.value|to_entries[]|select(.key|test("_(input|average|alarm|crit_alarm)$"))|select(.value|type=="number")|. as $reading|($reading.key|split("_")[0]) as $kind|{key:("sensor."+$chip.key+"."+$reading.key),metric:{label:($feature.key+(if ($reading.key|endswith("alarm")) then " alarm" else "" end)),value:$reading.value,unit:(if ($reading.key|endswith("alarm")) then "flags" else (if ($kind|startswith("temp")) then "°C" elif ($kind|startswith("fan")) then "RPM" elif ($kind|startswith("power")) then "W" elif ($kind|startswith("in")) then "V" elif ($kind|startswith("curr")) then "A" else "" end) end),group:(if ($reading.key|endswith("alarm")) then "Hardware sensors" elif ($kind|startswith("temp")) then "Temperatures" elif ($kind|test("^(fan|power|in|curr)")) then "Fans & power" else "Hardware sensors" end),entity:$chip.key,source:$src,warn:(if ($reading.key|endswith("alarm")) then null else $feature.value[$kind+"_max"] end),crit:(if ($reading.key|endswith("alarm")) then 1 else $feature.value[$kind+"_crit"] end)}}' "$WORK/sensors.json" >> "$WORK/metrics.ndjson"
 fi
 local zone zname ztemp
 for zone in /sys/class/thermal/thermal_zone*; do
  [[ -r $zone/temp ]] || continue
  zname=$(cat "$zone/type");ztemp=$(cat "$zone/temp")
  metric "thermal.${zone##*/}" "$zname temperature" "$(awk -v t="$ztemp" 'BEGIN{if(t>-273150&&t<1000000)print t/1000}')" '°C' Temperatures "$zname"
 done
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
    {k:"power_on_hours",label:"power on hours",value:$d.power_on_time.hours,unit:"hours"},
    {k:"power_cycles",label:"power cycles",value:$d.power_cycle_count,unit:"count"},
    {k:"capacity",label:"capacity",value:($d.user_capacity.bytes // null | if type=="number" then ./1073741824 else null end),unit:"GiB"},
    ($d.nvme_smart_health_information_log // {} | to_entries[] | select(.key=="percentage_used" or .key=="available_spare" or .key=="media_errors" or .key=="critical_warning" or .key=="warning_temp_time" or .key=="power_on_hours" or .key=="power_cycles" or .key=="unsafe_shutdowns" or .key=="num_err_log_entries" or .key=="data_units_read" or .key=="data_units_written" or .key=="controller_busy_time" or .key=="critical_comp_time") | {k:.key,label:(.key|gsub("_";" ")),value:.value,unit:(if .key=="percentage_used" or .key=="available_spare" then "%" else "count" end)}),
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
  metric "fs.$mount.total_gb" "$mount total capacity" "$(awk -v a="$total" 'BEGIN{print a/1073741824}')" GiB Storage "$mount"
  metric "fs.$mount.used_gb" "$mount used capacity" "$(awk -v a="$used" 'BEGIN{print a/1073741824}')" GiB Storage "$mount"
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
   ups.realpower.nominal) metric ups.ratedpower 'UPS rated output power' "$v" W UPS "$model";;
   ups.realpower) metric ups.realpower 'UPS real power' "$v" W UPS "$model";;
   ups.power) metric ups.apparentpower 'UPS apparent power' "$v" VA UPS "$model";;
   output.voltage) metric ups.output.voltage 'Output voltage' "$v" V UPS "$model";;
   ups.temperature) metric ups.temperature 'UPS temperature' "$v" '°C' UPS "$model";;
   battery.voltage) metric ups.battery.voltage 'Battery voltage' "$v" V UPS "$model";;
  esac
 done < "$WORK/ups"
}
io_collect() {
 local now dev
 now=$(awk '{print $1}' /proc/uptime)
 : > "$WORK/io.devices"
 for dev in /sys/block/sd* /sys/block/nvme* /sys/block/md* /sys/block/dm-*; do
  [[ -e $dev ]] && basename "$dev" >> "$WORK/io.devices"
 done
 if [[ -f $WORK/io.previous ]]; then
  awk -v now="$now" -v src="$SOURCE" '
   FILENAME==ARGV[1]{allowed[$1]=1;next}
   FILENAME==ARGV[2]{if(NF==1){oldtime=$1;next}r[$3]=$6;w[$3]=$10;rc[$3]=$4;wc[$3]=$8;busy[$3]=$13;next}
   ($3 in allowed) && ($3 in r) {dt=now-oldtime;if(dt<=0||dt>180)next;n=$3;
    v[1]=($6-r[n])*512/dt;v[2]=($10-w[n])*512/dt;v[3]=($4-rc[n])/dt;v[4]=($8-wc[n])/dt;v[5]=($13-busy[n])/(dt*10);v[6]=$12;
    split("read write read_iops write_iops busy inflight",k," ");split("B/s B/s IOPS IOPS % count",u," ");split("read,write,read operations,write operations,busy time,in-flight requests",l,",");
    for(i=1;i<=6;i++){if(v[i]<0)continue;if(i==5&&v[i]>100)v[i]=100;printf "{\"key\":\"io.%s.%s\",\"metric\":{\"label\":\"%s %s\",\"value\":%.6f,\"unit\":\"%s\",\"group\":\"Drive I/O\",\"entity\":\"%s\",\"source\":\"%s\"}}\n",n,k[i],n,l[i],v[i],u[i],n,src}
   }' "$WORK/io.devices" "$WORK/io.previous" /proc/diskstats >> "$WORK/metrics.ndjson" || return 1
 fi
 { printf '%s\n' "$now";cat /proc/diskstats; } > "$WORK/io.new"
 mv "$WORK/io.new" "$WORK/io.previous"
}
power_collect() {
 local path id name energy range now oldenergy oldtime delta found=0
 now=$(awk '{print $1}' /proc/uptime);: > "$WORK/power.new"
 for path in /sys/class/powercap/intel-rapl:*; do
  [[ -r $path/energy_uj && -r $path/max_energy_range_uj ]] || continue
  found=1;id=${path##*/};name=$(cat "$path/name");energy=$(cat "$path/energy_uj");range=$(cat "$path/max_energy_range_uj")
  printf '%s %s %s\n' "$id" "$energy" "$now" >> "$WORK/power.new"
  if [[ -f $WORK/power.previous ]]; then
   read -r oldenergy oldtime <<< "$(awk -v id="$id" '$1==id{print $2,$3;exit}' "$WORK/power.previous")"
   if [[ -n ${oldtime:-} ]]; then
    delta=$(awk -v e="$energy" -v o="$oldenergy" -v r="$range" -v t="$now" -v ot="$oldtime" 'BEGIN{dt=t-ot;if(dt<=0||dt>180)exit;v=e-o;if(v<0)v+=r;print v/1000000/dt}')
    metric "power.$id.watts" "$id $name power" "$delta" W 'Fans & power' "$name"
   fi
  fi
 done
 mv "$WORK/power.new" "$WORK/power.previous"
 [[ $found == 1 ]]
}
gpu_collect() {
 local uuid name bus temp util used total power fan path vendor class driver pci count=0
 # Inventory includes GPUs reserved for guests; do not rebind or query them through VFIO.
 for path in /sys/bus/pci/devices/*; do
  [[ -r $path/vendor && -r $path/class ]] || continue
  vendor=$(cat "$path/vendor");class=$(cat "$path/class")
  [[ $vendor == 0x10de && $class == 0x03* ]] || continue
  pci=${path##*/};driver=$(readlink "$path/driver" 2>/dev/null || true);driver=${driver##*/}
  if [[ $driver == vfio-pci ]]; then entity "gpu.pci.$pci" "NVIDIA GPU $pci" passthrough 'Reserved for a guest; host sensor access unavailable' GPU;fi
 done
 command -v nvidia-smi >/dev/null || return 1
 timeout 15 nvidia-smi --query-gpu=uuid,name,pci.bus_id,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed --format=csv,noheader,nounits > "$WORK/gpu.csv" || return 1
 while IFS=, read -r uuid name bus temp util used total power fan; do
  [[ -n $uuid ]] || continue
  uuid=${uuid// /};name=${name# };bus=${bus// /};count=$((count+1))
  entity "gpu.$uuid" "$name" online "$bus NVIDIA GPU" GPU
  metric "gpu.$uuid.temperature" "$name temperature" "${temp// /}" '°C' GPU "$bus"
  metric "gpu.$uuid.utilization" "$name utilization" "${util// /}" '%' GPU "$bus"
  metric "gpu.$uuid.memory" "$name memory used" "${used// /}" MiB GPU "$bus"
  metric "gpu.$uuid.memory_total" "$name memory total" "${total// /}" MiB GPU "$bus"
  metric "gpu.$uuid.power" "$name power" "${power// /}" W GPU "$bus"
  metric "gpu.$uuid.fan" "$name fan speed" "${fan// /}" '%' GPU "$bus"
 done < "$WORK/gpu.csv"
 [[ $count -gt 0 ]]
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
 collect_source 'Drive I/O' io_collect
 collect_source 'CPU & memory power' power_collect
 collect_source 'GPU' gpu_collect
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
