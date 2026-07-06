#!/usr/bin/env bash
# kai へ指示を投入する（隠しの kai-brain セッションへ送る。Issue #46）。
# 配信画面には映らない — 指示プロンプト（hermes コマンド）を視聴者に見せないため。
# kai の作業（terminal 実行・編集・実況）は見える kai-term / VSCode / 音声に出る。
#
# 使い方（VM 上、stage.sh の後）:
#   kai-ask.sh "#44 の対応を行う"
#   kai-ask.sh "今日の作業のまとめを話して"
#
# SOUL.md に Issue 対応の標準手順が入っているので、指示は 1 行でよい。
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SESSION="${KAI_BRAIN_SESSION:-kai-brain}"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "使い方: kai-ask.sh \"指示（例: #44 の対応を行う）\"" >&2
  exit 2
fi
PROMPT="$1"

if ! tmux has-session -t "${SESSION}" 2>/dev/null; then
  echo "隠しセッション ${SESSION} がありません。先に stage.sh を実行してください。" >&2
  exit 1
fi

# 単一引用符をエスケープして hermes -z '...' に包み、隠しセッションへ送る。
ESCAPED="${PROMPT//\'/\'\\\'\'}"
tmux send-keys -t "${SESSION}" -l ".venv/bin/hermes --yolo -z '${ESCAPED}'"
tmux send-keys -t "${SESSION}" Enter
echo "kai へ指示を送信（隠しセッション ${SESSION}）: ${PROMPT}"
