#!/usr/bin/env bash
# speechd を VM 上で systemd --user サービスとして登録する。
# 冪等: 何度実行してもよい。設計: docs/kai/design/00-system.md §3(ADR-3) / §4。
# 作法は kai-services/streaming/setup.sh（kai-x11vnc.service の @TAILSCALE_IP@
# 置換）を踏襲する。
#
# 使い方（VM 上、通常ユーザーで実行）:
#   git clone https://github.com/seiichi3141/kai-agent.git ~/kai-agent
#   bash ~/kai-agent/kai-services/speechd/install.sh
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(cd ../.. && pwd)"

echo "==> 1/3 前提チェック"
command -v python3 >/dev/null || { echo "python3 が見つかりません" >&2; exit 1; }
command -v paplay >/dev/null || echo "!! paplay が見つかりません（pulseaudio-utils を導入してください。再生は縮退＝字幕のみになります）"
python3 -m py_compile speechd.py

echo "==> 2/3 systemd --user unit（テンプレートの @REPO_DIR@ を実パスに置換）"
mkdir -p "$HOME/.config/systemd/user"
sed "s#@REPO_DIR@#${REPO_DIR}#g" speechd.service \
  > "$HOME/.config/systemd/user/speechd.service"

systemctl --user daemon-reload
systemctl --user enable --now speechd.service

echo "==> 3/3 linger（ログアウト/再起動後も user unit を維持）"
sudo loginctl enable-linger "$USER" 2>/dev/null || echo "!! loginctl enable-linger は手動で実行してください（sudo 権限が必要）"

echo ""
echo "登録完了。検証:"
echo "  systemctl --user status speechd.service"
echo "  curl -s http://127.0.0.1:\${SPEECHD_PORT:-8900}/health"
echo "  journalctl --user -u speechd.service -f"
echo "詳細な手動検証手順は README.md を参照してください。"
