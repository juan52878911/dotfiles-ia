#!/usr/bin/env bash
# Resource sampler for a worker node (started by `delega metrics start`, stopped by `metrics stop`).
# Every INTERVAL seconds it appends one line to <dir>/resources.csv. Stop it with: kill $(cat <dir>/sampler.pid)
#   sample.sh [interval_s=15] [dir=~/delega/metrics]
INTERVAL=${1:-15}
DIR=${2:-$HOME/delega/metrics}
mkdir -p "$DIR"
echo $$ > "$DIR/sampler.pid"
FILE="$DIR/resources.csv"
[[ -s "$FILE" ]] || echo "ts,mem_total_mb,mem_avail_mb,swap_used_mb,load1,cpu_us,cpu_sy,cpu_id,cpu_wa,disk_free_gb,containers,claude_procs,node_procs,oom_kills,top_rss" > "$FILE"
while true; do
  read -r mt ma <<<"$(awk '/MemTotal/{t=int($2/1024)} /MemAvailable/{a=int($2/1024)} END{print t, a}' /proc/meminfo)"
  sw=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)
  c1=$(cut -d' ' -f1 /proc/loadavg)
  read -r us sy id wa <<<"$(vmstat 1 2 | tail -1 | awk '{print $13, $14, $15, $16}')"
  disk=$(df -BG --output=avail ~ | tail -1 | tr -dc 0-9)
  cont=$(docker ps -q 2>/dev/null | wc -l)
  pcl=$(pgrep -c -x claude || true); pno=$(pgrep -c -x node || true)
  oom=$(awk '/^oom_kill /{print $2}' /proc/vmstat)
  top=$(ps -eo rss=,comm= --sort=-rss | head -4 | awk '{printf "%s:%d;", $2, $1/1024}')
  echo "$(date +%Y-%m-%dT%H:%M:%S),$mt,$ma,$sw,$c1,$us,$sy,$id,$wa,$disk,$cont,$pcl,$pno,${oom:-0},$top" >> "$FILE"
  sleep $((INTERVAL > 1 ? INTERVAL - 1 : 1))
done
