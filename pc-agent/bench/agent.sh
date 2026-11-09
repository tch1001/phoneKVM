#!/bin/bash
# Start/stop one kvm-lite agent instance per port (pidfile based, so tests can
# run next to the production agent on 8080 without touching it).
#   bench/agent.sh start 8095 h264 1920 1080 30 [extra args...]
#   bench/agent.sh stop 8095
set -u
PORT="${2:?port}"
PIDFILE="/tmp/kvm-agent-$PORT.pid"
LOG="/tmp/agent-$PORT.log"
case "$1" in
  start)
    shift 2
    CODEC="${1:-jpeg}"; W="${2:-1920}"; H="${3:-1080}"; FPS="${4:-30}"; shift 4 || true
    cd /home/degen2/kvm-lite/pc-agent || exit 1
    : > "$LOG"
    nohup .venv/bin/python -m kvm_agent --port "$PORT" --codec "$CODEC" \
        --width "$W" --height "$H" --fps "$FPS" --lat-log "/tmp/lat-$PORT.jsonl" \
        "$@" >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 40); do
      if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
        echo "agent up on $PORT (pid $(cat "$PIDFILE"))"; exit 0
      fi
      sleep 0.25
    done
    echo "agent on $PORT failed to start"; tail -20 "$LOG"; exit 1
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break; sleep 0.2; done
      rm -f "$PIDFILE"
    fi
    PID=$(ss -ltnp 2>/dev/null | sed -n "s/.*:$PORT .*pid=\([0-9]*\).*/\1/p")
    [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null
    echo "agent on $PORT stopped"
    ;;
  *) echo "usage: $0 start|stop PORT ..."; exit 2;;
esac
