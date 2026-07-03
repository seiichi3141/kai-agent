#!/usr/bin/env bash
# kai 配信スタックのセットアップ（Ubuntu 24.04 arm64 / Oracle A1 想定）。
# 冪等: 何度実行してもよい。設計: docs/kai/design/streaming.md
#
# 使い方（サーバー上で、通常ユーザーで実行）:
#   bash kai-services/streaming/setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/6 apt パッケージ"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  xserver-xorg-core xserver-xorg-video-dummy x11-utils x11-xserver-utils \
  xfce4 xfce4-terminal dbus-x11 \
  x11vnc \
  pipewire pipewire-pulse wireplumber pulseaudio-utils \
  obs-studio \
  fonts-noto-cjk fonts-noto-color-emoji \
  ffmpeg curl jq

echo "==> 2/6 Xorg 設定（dummy ドライバ・非 root 起動許可）"
sudo install -D -m 0644 conf/10-dummy.conf /etc/X11/xorg.conf.d/10-dummy.conf
sudo tee /etc/X11/Xwrapper.config >/dev/null <<'EOF'
allowed_users=anybody
needs_root_rights=no
EOF

echo "==> 3/6 PipeWire null-sink（kai_speaker）"
install -D -m 0644 conf/10-kai-speaker.conf \
  "$HOME/.config/pipewire/pipewire.conf.d/10-kai-speaker.conf"
systemctl --user restart pipewire wireplumber 2>/dev/null || true

echo "==> 4/6 VNC パスワード（VNC_PASSWORD env で非対話設定可）"
if [[ ! -f "$HOME/.vnc/passwd" ]]; then
  mkdir -p "$HOME/.vnc"
  if [[ -n "${VNC_PASSWORD:-}" ]]; then
    x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd"
  else
    x11vnc -storepasswd "$HOME/.vnc/passwd"
  fi
fi

echo "==> 5/6 systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp units/kai-xorg.service units/kai-desktop.service "$HOME/.config/systemd/user/"
# x11vnc は Tailscale IP に bind する（テンプレートの @TAILSCALE_IP@ を置換）
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [[ -z "$TS_IP" ]]; then
  echo "!! tailscale ip が取れません。先に 'sudo tailscale up' を済ませてから再実行してください。" >&2
  exit 1
fi
sed "s/@TAILSCALE_IP@/$TS_IP/" units/kai-x11vnc.service \
  > "$HOME/.config/systemd/user/kai-x11vnc.service"

systemctl --user daemon-reload
systemctl --user enable --now kai-xorg.service kai-desktop.service kai-x11vnc.service

echo "==> 6/6 linger（ログアウト/再起動後も user unit を維持）"
sudo loginctl enable-linger "$USER"

echo ""
echo "セットアップ完了。検証:"
echo "  DISPLAY=:0 xdpyinfo | grep dimensions      # → 1920x1080"
echo "  pactl list short sinks | grep kai_speaker"
echo "  VNC クライアントで ${TS_IP}:5900 に接続"
echo "OBS は VNC 内で手動起動して初期設定してください（README.md 参照）。"
