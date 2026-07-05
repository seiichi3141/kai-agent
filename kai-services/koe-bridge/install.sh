#!/usr/bin/env bash
# koe-bridge を VM 上で systemd --user サービスとして登録する。
# 冪等: 何度実行してもよい。作法は kai-services/speechd/install.sh を踏襲。
#
# 使い方（VM 上、通常ユーザーで実行）:
#   bash ~/kai-agent/kai-services/koe-bridge/install.sh
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(cd ../.. && pwd)"

echo "==> 1/2 前提チェック"
[[ -x "${REPO_DIR}/.venv/bin/python3" ]] || { echo "hermes の venv がありません（${REPO_DIR}/.venv）" >&2; exit 1; }
"${REPO_DIR}/.venv/bin/python3" -m py_compile koe_bridge.py

echo "==> 2/2 systemd --user unit（テンプレートの @REPO_DIR@ を実パスに置換）"
mkdir -p "$HOME/.config/systemd/user"
sed "s#@REPO_DIR@#${REPO_DIR}#g" koe-bridge.service \
  > "$HOME/.config/systemd/user/koe-bridge.service"

systemctl --user daemon-reload
systemctl --user enable --now koe-bridge.service

echo ""
echo "登録完了。検証:"
echo "  curl -s http://127.0.0.1:8930/health"
echo "Mac 側 aquestalk-server の .env:"
echo "  KOE_LLM_BASE_URL=http://<kai-vm の Tailscale IP>:8930/v1"
