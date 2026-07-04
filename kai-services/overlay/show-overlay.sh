#!/usr/bin/env bash
# kai 配信オーバーレイ（字幕 Web オーバーレイ）を VM の :0 デスクトップに
# 透過・最前面・枠なし・クリックスルーで表示する（手動起動用ヘルパー）。
# 常駐は kai-overlay.service（systemd --user）を使うこと。
#
# 実体は overlay-window.py（WebKitGTK）。設計: docs/kai/design/00-system.md §4。
#
# 使い方（VM 上で）:
#   DISPLAY=:0 bash show-overlay.sh
set -euo pipefail
export DISPLAY="${DISPLAY:-:0}"
cd "$(dirname "$0")"

python3 overlay-window.py > /tmp/kai-overlay.log 2>&1 &
PID=$!
echo "kai-overlay 起動 (pid=${PID})"
echo "ログ: /tmp/kai-overlay.log"
echo "終了するには: kill ${PID}"
