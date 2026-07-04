#!/usr/bin/env bash
# kai 配信オーバーレイ（字幕 Web オーバーレイ）を VM の :0 デスクトップに
# 透過・最前面・枠なしで表示する。設計: docs/kai/design/00-system.md §4。
#
# 前提: DISPLAY=:0 の X11 セッションが起動済み（kai-desktop.service。
# kai-services/streaming/units/kai-desktop.service 参照）。透過ウィンドウを
# 実際に「透けさせる」には、ウィンドウマネージャ側でコンポジタが有効である
# 必要がある（XFCE は既定で xfwm4 の compositor が有効。無効化されている
# 場合は `xfconf-query -c xfwm4 -p /general/use_compositing -s true` で有効化）。
#
# 使い方（VM 上、:0 セッション内のターミナルで）:
#   DISPLAY=:0 bash show-overlay.sh
set -euo pipefail
export DISPLAY="${DISPLAY:-:0}"
cd "$(dirname "$0")"

OVERLAY_PATH="$(pwd)/index.html"
OVERLAY_URL="file://${OVERLAY_PATH}"
WIN_CLASS="kai-overlay"
PROFILE_DIR="${HOME}/.config/kai-overlay-profile"

if command -v chromium >/dev/null 2>&1; then
  CHROMIUM=chromium
elif command -v chromium-browser >/dev/null 2>&1; then
  CHROMIUM=chromium-browser
else
  echo "chromium が見つかりません（kai-services/streaming/vm/setup.sh で snap install 済みのはず）" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

# --enable-transparent-visuals: X11 の ARGB visual を使わせ、ページの
#   `background: transparent` が実際にデスクトップへ透過するようにする。
# --disable-gpu: GPU 合成パスが透過を上書きしてしまうことがあるため無効化
#   （X11 + software 合成のほうが透過ウィンドウでは安定する）。
# --app=: URL バー・タブなどの chrome UI を消してページだけを表示する。
# --window-position / --window-size: 1920x1080 のデスクトップ全面に配置。
# --class: wmctrl / xdotool でウィンドウを特定するためのヒント。
"$CHROMIUM" \
  --app="$OVERLAY_URL" \
  --window-size=1920,1080 \
  --window-position=0,0 \
  --class="$WIN_CLASS" \
  --enable-transparent-visuals \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --user-data-dir="$PROFILE_DIR" \
  >/tmp/kai-overlay-chromium.log 2>&1 &
CHROME_PID=$!
echo "chromium overlay 起動 (pid=${CHROME_PID})。ウィンドウ検出・最前面固定を試みます..."

sleep 3

WIN_ID=""
if command -v xdotool >/dev/null 2>&1; then
  WIN_ID="$(xdotool search --class "$WIN_CLASS" 2>/dev/null | head -1 || true)"
fi

if [[ -n "$WIN_ID" ]] && command -v wmctrl >/dev/null 2>&1; then
  # 最前面固定・枠なし（可能な範囲で。完璧でなくてよい）。
  wmctrl -i -r "$WIN_ID" -b add,above
  echo "最前面固定 OK: window id = ${WIN_ID}"
  if command -v xdotool >/dev/null 2>&1; then
    xdotool windowmove "$WIN_ID" 0 0 2>/dev/null || true
    xdotool windowsize "$WIN_ID" 1920 1080 2>/dev/null || true
  fi
else
  echo "!! ウィンドウ検出/最前面固定に失敗しました（wmctrl/xdotool の有無や"
  echo "   タイミングを確認してください。手動なら:"
  echo "   wmctrl -l                       # ウィンドウ一覧"
  echo "   wmctrl -r <window> -b add,above # 最前面固定"
fi

echo ""
echo "終了するには: kill ${CHROME_PID}"
echo "ログ: /tmp/kai-overlay-chromium.log"
