#!/bin/bash
# 把 cc-glance 装成登录自启项(常驻)。跑一次即可;卸载见末尾。
set -e
cd "$(dirname "$0")"
swift build -c release
BIN="$(pwd)/.build/release/ccglance"
LABEL="com.shona.ccglance"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/ccglance.log</string>
  <key>StandardOutPath</key><string>/tmp/ccglance.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ 已装为登录自启:$PLIST"
echo "  卸载:launchctl unload \"$PLIST\" && rm \"$PLIST\""
