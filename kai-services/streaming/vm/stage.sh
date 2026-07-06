#!/usr/bin/env bash
# kai 配信ステージのセットアップ（運転台本 v0.4 / Issue #46 #47 #48 #49）。
# 「本物の人間のライブコーディング」に見せるため、tmux を 2 セッションに分ける:
#
#   kai-term  … 配信に映る作業ターミナル。kai の terminal ツール（override）が
#               ここで実際にコマンドを実行する（tee/marker は kai() 関数に隠す）。
#               VSCode の統合ターミナルにこれを attach して見せる。
#   kai-brain … 配信に映さない頭脳セッション。ここで hermes を動かす。オーナーは
#               kai-ask.sh でここへ指示を送る（指示プロンプトが配信に出ない #46）。
#
# 使い方（VM 上、DISPLAY のある環境で）:
#   bash ~/kai-agent/kai-services/streaming/vm/stage.sh
# その後 kai-ask.sh で指示を投入する。冪等（何度でも実行可）。
set -euo pipefail

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

echo "==> 1/4 tmux セッション（見える kai-term / 隠し kai-brain）"
# 見える作業ターミナル。kai() ヘルパーの ready フラグはリセット（起動時に再定義）。
rm -f /tmp/kai-term.ready /tmp/kai-term.out /tmp/kai-term.done
tmux kill-session -t kai-term 2>/dev/null || true
tmux new-session -d -s kai-term -x 200 -y 24 "cd '${REPO}' && clear && exec bash"
# 隠しの頭脳セッション（配信には attach しない）。
tmux kill-session -t kai-brain 2>/dev/null || true
tmux new-session -d -s kai-brain -x 200 -y 50 "cd '${REPO}' && exec bash"

echo "==> 2/4 VSCode 起動"
systemctl --user stop kai-vscode 2>/dev/null || true
pkill -x code 2>/dev/null || true
sleep 2
systemctl --user reset-failed kai-vscode 2>/dev/null || true
systemd-run --user --unit=kai-vscode --collect --setenv=DISPLAY="${DISPLAY}" \
  code --disable-gpu --wait "${REPO}"
sleep 22

echo "==> 3/4 VSCode 整形（最大化・残タブ/チャット非表示・統合ターミナルで kai-term に attach）"
VSWIN="$(wmctrl -lx | awk '$3 ~ /^code\./ {print $1; exit}')"
if [[ -n "${VSWIN}" ]]; then
  wmctrl -i -a "${VSWIN}"
  wmctrl -i -r "${VSWIN}" -b add,maximized_vert,maximized_horz
  xdotool key --window "${VSWIN}" ctrl+k ctrl+w          # 全エディタを閉じる
  sleep 1
  xdotool key --window "${VSWIN}" --clearmodifiers ctrl+shift+p
  sleep 1.5
  xdotool type --delay 50 "View: Close Secondary Side Bar"  # 右チャット非表示
  sleep 0.5; xdotool key Return
  sleep 1
  xdotool key --window "${VSWIN}" ctrl+grave              # 統合ターミナル
  sleep 2
  # 配信に映るのは kai-term（kai-brain ではない）。
  xdotool type --delay 50 "tmux attach -t kai-term"; xdotool key Return
fi

echo "==> 4/4 疎通確認"
sleep 2
curl -s -X POST http://127.0.0.1:8920/edit -d '{"edits": []}' >/dev/null 2>&1 \
  && echo "  ブリッジ /edit OK" || echo "  !! ブリッジに繋がりません（拡張未起動？）"
tmux has-session -t kai-term 2>/dev/null && echo "  kai-term OK（配信に映る作業ターミナル）"
tmux has-session -t kai-brain 2>/dev/null && echo "  kai-brain OK（隠しの頭脳セッション）"

echo ""
echo "✅ ステージ準備完了。指示は kai-ask.sh で隠しセッションへ:"
echo "   bash ${REPO}/kai-services/streaming/vm/kai-ask.sh '#44 の対応を行う'"
