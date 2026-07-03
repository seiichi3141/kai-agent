#!/usr/bin/env bash
# kai 配信スタックのセットアップ — UTM VM（Ubuntu 24.04 Desktop arm64）内で実行する。
# 冪等: 何度実行してもよい。設計: docs/kai/design/streaming.md
#
# 前提: Ubuntu Desktop を標準インストール済み（ユーザー名 kai 推奨）、ネットワーク接続あり。
# 使い方（VM 内のターミナルで）:
#   git clone https://github.com/seiichi3141/kai-agent.git ~/kai-agent
#   bash ~/kai-agent/kai-services/streaming/vm/setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/7 apt パッケージ"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  openssh-server \
  obs-studio ffmpeg \
  x11-utils xdotool pulseaudio-utils \
  fonts-noto-cjk fonts-noto-color-emoji \
  language-pack-ja language-pack-gnome-ja \
  curl jq git

echo "==> 1b. 日本語ロケール（インストーラーは文字化けのため英語で入れる前提。ここで日本語化）"
sudo locale-gen ja_JP.UTF-8
sudo localectl set-locale LANG=ja_JP.UTF-8 || sudo update-locale LANG=ja_JP.UTF-8

echo "==> 2/7 ブラウザ（snap の本物の Chromium。VM なので snap が使える）"
sudo snap install chromium 2>/dev/null || echo "(chromium: already installed)"

echo "==> 3/7 Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo ""
  echo "!! 次のコマンドを実行してブラウザで認証してください: sudo tailscale up --ssh --hostname=kai-vm"
fi

echo "==> 4/7 PipeWire null-sink（kai_speaker）"
install -D -m 0644 ../conf/10-kai-speaker.conf \
  "$HOME/.config/pipewire/pipewire.conf.d/10-kai-speaker.conf"
systemctl --user restart pipewire wireplumber pipewire-pulse 2>/dev/null || true

echo "==> 5/7 gdm: 自動ログイン + X11 固定（Wayland は OBS XSHM / xdotool と相性が悪い）"
sudo python3 - <<PYEOF
import configparser, io
p = "/etc/gdm3/custom.conf"
c = configparser.ConfigParser()
c.optionxform = str
c.read(p)
if "daemon" not in c: c.add_section("daemon")
c["daemon"]["WaylandEnable"] = "false"
c["daemon"]["AutomaticLoginEnable"] = "true"
c["daemon"]["AutomaticLogin"] = "$USER"
buf = io.StringIO(); c.write(buf)
open(p, "w").write(buf.getvalue())
print("gdm3/custom.conf updated")
PYEOF

echo "==> 6/7 GNOME: 画面ロック・アイドル・サスペンドの無効化（配信事故防止）"
gsettings set org.gnome.desktop.session idle-delay 0 || true
gsettings set org.gnome.desktop.screensaver lock-enabled false || true
gsettings set org.gnome.desktop.lockdown disable-lock-screen true || true
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' || true
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' || true
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false || true

echo "==> 7/7 SSH 有効化"
sudo systemctl enable --now ssh

echo ""
echo "セットアップ完了。次の手順:"
echo "  1. まだなら: sudo tailscale up --ssh --hostname=kai-vm   （ブラウザ認証）"
echo "  2. 再起動して X11 セッション + 自動ログインを確認: sudo reboot"
echo "  3. 再起動後の検証（Mac から ssh kai@kai-vm で可）:"
echo "     echo \$XDG_SESSION_TYPE            # → x11"
echo "     pactl list short sinks | grep kai_speaker"
echo "  4. 設定 → ディスプレイ で解像度を 1920x1080 に"
echo "  5. OBS 初期設定は kai-services/streaming/README.md §3 と同じ（画面キャプチャ XSHM）"
