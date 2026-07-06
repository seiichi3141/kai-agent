#!/usr/bin/env bash
# kai-vm に配信ステージ用の VSCode を導入・設定する（Issue #8）。
# 冪等（何度実行してもよい）。使い方（VM 上）:
#   bash ~/kai-agent/kai-services/streaming/vm/setup-vscode.sh
#
# やること:
#   1. VSCode (arm64 deb) のインストール（未導入時のみ）
#   2. 配信向け settings.json / argv.json（gnome-keyring ダイアログ抑止）
#   3. 空パスワードのデフォルトキーリング作成（システム全体のダイアログ抑止。
#      自動ログイン運用でキーリング未作成だと各アプリが作成ダイアログを出し、
#      配信画面に映る + ダイアログ待ちで CPU が張り付く — 実測済み）
#   4. kai-typewriter 拡張（編集のタイピング再生）の配置
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/4 VSCode"
if ! command -v code >/dev/null; then
  curl -fsSL -o /tmp/vscode-arm64.deb \
    "https://update.code.visualstudio.com/latest/linux-deb-arm64/stable"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/vscode-arm64.deb
  rm -f /tmp/vscode-arm64.deb
fi
code --version | head -1

echo "==> 2/4 配信向け設定（settings.json / argv.json）"
mkdir -p "$HOME/.config/Code/User" "$HOME/.vscode"
cat > "$HOME/.config/Code/User/settings.json" <<'EOF'
{
  "security.workspace.trust.enabled": false,
  "telemetry.telemetryLevel": "off",
  "update.mode": "none",
  "editor.fontSize": 16,
  "terminal.integrated.fontSize": 15,
  "window.zoomLevel": 0.5,
  "workbench.colorTheme": "Dark+",
  "workbench.startupEditor": "none",
  "explorer.autoReveal": true,
  "extensions.ignoreRecommendations": true,
  "extensions.autoCheckUpdates": false,
  "workbench.tips.enabled": false,
  "files.hotExit": "off",
  "window.restoreWindows": "none",
  "chat.commandCenter.enabled": false,
  "chat.experimental.offerSetup": false,
  "workbench.secondarySideBar.showLabels": false,
  "workbench.secondarySideBar.defaultVisibility": "hidden"
}
EOF
cat > "$HOME/.vscode/argv.json" <<'EOF'
{
  // gnome-keyring のパスワード作成ダイアログを配信画面に出さない（kai 運用）
  "password-store": "basic",
  "enable-crash-reporter": false
}
EOF

echo "==> 3/4 デフォルトキーリング（空パスワード）"
KEYRING_DIR="$HOME/.local/share/keyrings"
if [[ ! -f "${KEYRING_DIR}/default" ]]; then
  mkdir -p "${KEYRING_DIR}"
  chmod 700 "${KEYRING_DIR}"
  printf "Default_keyring" > "${KEYRING_DIR}/default"
  cat > "${KEYRING_DIR}/Default_keyring.keyring" <<'EOF'
[keyring]
display-name=Default keyring
ctime=0
mtime=0
lock-on-idle=false
lock-after=false
EOF
  chmod 600 "${KEYRING_DIR}/Default_keyring.keyring"
  pkill -x gnome-keyring-d 2>/dev/null || true # 再起動して新キーリングを読ませる
  echo "  作成しました"
else
  echo "  (既に存在)"
fi

echo "==> 4/4 kai-typewriter 拡張のインストール"
# 注意: ~/.vscode/extensions/ への手動フォルダコピーは VSCode に認識されない
# （extensions.json レジストリ管理のため — 実機確認済み）。VSIX を組んで
# code --install-extension で入れる。
VSIX="/tmp/kai-typewriter-0.1.0.vsix"
python3 - "${VSIX}" <<'PYEOF'
import sys
import zipfile
from pathlib import Path

out = Path(sys.argv[1])
src = Path("vscode/kai-typewriter")
manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="kai-typewriter" Version="0.1.0" Publisher="kai"/>
    <DisplayName>kai typewriter</DisplayName>
    <Description xml:space="preserve">kai の編集をタイピング風に再生する配信演出拡張</Description>
    <Categories>Other</Categories>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""
content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", content_types)
    z.writestr("extension.vsixmanifest", manifest)
    z.write(src / "package.json", "extension/package.json")
    z.write(src / "extension.js", "extension/extension.js")
print(f"  built {out}")
PYEOF
code --install-extension "${VSIX}" --force
rm -f "${VSIX}"

echo ""
echo "✅ 完了。VSCode の起動（配信ステージ）:"
echo "  systemd-run --user --unit=kai-vscode --collect --setenv=DISPLAY=:0 \\"
echo "    code --disable-gpu --wait ~/kai-agent"
echo "拡張の疎通確認（VSCode 起動後）:"
echo "  curl -s -X POST http://127.0.0.1:8920/edit -d '{\"files\":[\"/tmp/x\"]}'"
