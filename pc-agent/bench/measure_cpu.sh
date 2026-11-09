#!/bin/bash
# PC-side CPU cost per frame for the agent on a port (agent + ffmpeg child).
#   bench/measure_cpu.sh 8095 10
set -u
PORT="${1:?port}"; SECS="${2:-10}"
PID=$(cat "/tmp/kvm-agent-$PORT.pid" 2>/dev/null)
[ -z "${PID:-}" ] && { echo "no pidfile for $PORT"; exit 1; }
frames_json() {
  curl -s "http://127.0.0.1:$PORT/healthz" | tr ',' '\n' | awk -F: '/"frames"/{gsub(/ /,"",$2); print $2}'
}
f0=$(frames_json)
read_cpu() {
  local total=0
  for p in "$PID" $(pgrep -P "$PID"); do
    [ -r "/proc/$p/stat" ] || continue
    # shellcheck disable=SC2046
    t=$(awk '{print $14+$15}' "/proc/$p/stat")
    total=$((total + t))
  done
  echo "$total"
}
c0=$(read_cpu)
sleep "$SECS"
c1=$(read_cpu)
f1=$(frames_json)
python3 - "$c0" "$c1" "$f0" "$f1" "$SECS" <<'EOF'
import sys
c0,c1,f0,f1,secs = map(float, sys.argv[1:6])
frames = f1 - f0
cpu_s = (c1 - c0) / 100.0          # CLK_TCK=100
print(f"frames={int(frames)} in {secs:g}s ({frames/secs:.1f} fps); "
      f"CPU={cpu_s:.2f} cpu-s => {cpu_s/max(1,frames)*1000:.1f} ms/frame "
      f"({cpu_s/secs*100:.0f}% of one core)")
EOF
