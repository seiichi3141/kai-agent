#!/usr/bin/env bash
# trace-web を VM で常駐させる（npm ci + build + systemd --user）。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> 依存インストール（npm ci）"
npm ci || npm install

echo "==> ビルド（next build）"
npm run build

echo "==> systemd --user サービス登録"
mkdir -p "$HOME/.config/systemd/user"
cp kai-trace-web.service "$HOME/.config/systemd/user/kai-trace-web.service"
systemctl --user daemon-reload
systemctl --user enable --now kai-trace-web.service
echo "✅ http://<kai-vm の Tailscale IP>:8910/ で閲覧できます"
systemctl --user status kai-trace-web.service --no-pager | head -5 || true
