#!/usr/bin/env bash
# aquestalk-server を Mac の launchd LaunchAgent として常駐登録する。
# .env（このディレクトリ）を読み、キーを含む実 plist を ~/Library/LaunchAgents に生成する。
# 実 plist はライセンスキーを含むためリポジトリには置かない。
#
# 使い方: cd kai-services/aquestalk-server && bash install-launchd.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.kai.aquestalk-server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[[ -f .env ]] || { echo ".env がありません（AQUESTALK_* とキーを設定してください）"; exit 1; }
# .env 読み込み
set -a; source .env; set +a
NODE="$(nodenv which node 2>/dev/null || command -v node)"
[[ -x "$NODE" ]] || { echo "node が見つかりません"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" logs

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$NODE</string>
    <string>$DIR/src/server.mjs</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AQUESTALK_CLI_PATH</key><string>${AQUESTALK_CLI_PATH}</string>
    <key>AQUESTALK_SDK_DIR</key><string>${AQUESTALK_SDK_DIR}</string>
    <key>AQUESTALK_DEV_KEY</key><string>${AQUESTALK_DEV_KEY:-}</string>
    <key>AQUESTALK_USR_KEY</key><string>${AQUESTALK_USR_KEY:-}</string>
    <key>PORT</key><string>${PORT:-8890}</string>
    <key>BIND_ADDR</key><string>${BIND_ADDR:-0.0.0.0}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/server.log</string>
</dict>
</plist>
EOF
chmod 600 "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "登録完了: $LABEL"
echo "検証: curl -s --noproxy '*' http://127.0.0.1:${PORT:-8890}/health"
echo "ログ: $DIR/logs/server.log"
echo "停止: launchctl bootout gui/$(id -u)/$LABEL"
