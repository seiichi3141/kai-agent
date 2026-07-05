#!/usr/bin/env bash
# trace-viewer を VM 上で systemd --user サービスとして登録する。
# 冪等: 何度実行してもよい。作法は kai-services/speechd/install.sh を踏襲。
#
# 使い方（VM 上、通常ユーザーで実行）:
#   bash ~/kai-agent/kai-services/trace-viewer/install.sh
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(cd ../.. && pwd)"

echo "==> 1/3 前提チェック"
command -v python3 >/dev/null || { echo "python3 が見つかりません" >&2; exit 1; }
python3 -m py_compile trace_viewer.py

echo "==> 2/3 systemd --user unit（テンプレートの @REPO_DIR@ を実パスに置換）"
mkdir -p "$HOME/.config/systemd/user"
sed "s#@REPO_DIR@#${REPO_DIR}#g" trace-viewer.service \
  > "$HOME/.config/systemd/user/trace-viewer.service"

systemctl --user daemon-reload
systemctl --user enable --now trace-viewer.service

echo "==> 3/3 linger（ログアウト/再起動後も user unit を維持）"
sudo loginctl enable-linger "$USER" 2>/dev/null || echo "!! loginctl enable-linger は手動で実行してください（sudo 権限が必要）"

echo ""
echo "登録完了。検証:"
echo "  systemctl --user status trace-viewer.service"
echo "  curl -s http://127.0.0.1:\${TRACE_VIEWER_PORT:-8910}/api/dates"
echo "ブラウザ（Tailscale 内の別マシン）から: http://<kai-vm の Tailscale IP>:8910/"
