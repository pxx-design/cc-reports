#!/bin/bash
# cc-glance 菜单栏浮层 · 启停
#   ./run.sh          构建(如需)并前台启动
#   ./run.sh restart  重启后台实例
#   ./run.sh stop     停掉
set -e
cd "$(dirname "$0")"
BIN=".build/release/ccglance"

build() { swift build -c release; }

case "${1:-start}" in
  stop)    pkill -f "release/ccglance" 2>/dev/null && echo "stopped" || echo "not running" ;;
  restart) pkill -f "release/ccglance" 2>/dev/null; sleep 0.3
           [ -x "$BIN" ] || build
           nohup "$BIN" >/tmp/ccglance.log 2>&1 &
           echo "restarted (PID $!)" ;;
  *)       [ -x "$BIN" ] || build
           exec "$BIN" ;;
esac
