# 配信前プリフライトチェックリスト

リハーサル / 配信の直前に、オフライン状態で上から順に確認する。1 つでも赤い項目が
ある場合は配信を開始せず、直してから再実行する。

## チェックリスト

- [ ] **Mac の aquestalk-server が WAV を返すこと。** `/health` の 200 だけでは不十分。
      `/synthesize` を叩き、HTTP 200 かつ `Content-Type` が `audio/wav`（または
      `audio/x-wav`）で、出力ファイルが空ではないことを確認する。

  ```bash
  curl -fsS -o /tmp/kai-preflight.wav \
    -H 'Content-Type: application/json' \
    -d '{"text":"配信前チェックです"}' \
    http://<mac-tts>:8890/synthesize
  file /tmp/kai-preflight.wav
  test -s /tmp/kai-preflight.wav
  ```

- [ ] **VM の speechd / koe-bridge の health が緑であること。** speechd は発話 API、
      koe-bridge は Mac TTS への橋渡しなので、両方が生きていることを確認する。
      Tailscale SSH の定期再認証で `Tailscale SSH requires an additional check` と
      認証 URL が出た場合は、URL の認証を通してからチェックを続ける。

  ```bash
  curl -fsS http://127.0.0.1:8900/health
  curl -fsS http://127.0.0.1:8930/health
  ```

- [ ] **config.yaml に `plugins.entries.kai_ide.allow_tool_override: true` があること。**
      kai_ide が terminal / write_file / patch を配信向けに見える操作へ差し替えるための
      設定なので、配信前に有効化を確認する。

  ```bash
  python3 - <<'PY'
  import sys
  import yaml
  from pathlib import Path

  config = yaml.safe_load(Path.home().joinpath('.hermes/config.yaml').read_text()) or {}
  value = (
      config.get('plugins', {})
      .get('entries', {})
      .get('kai_ide', {})
      .get('allow_tool_override')
  )
  if value is not True:
      sys.exit('plugins.entries.kai_ide.allow_tool_override is not true')
  print('kai_ide allow_tool_override: true')
  PY
  ```

- [ ] **VSCode ブリッジ（127.0.0.1:8920）の疎通があること。** kai が VSCode を開く・
      タブを閉じる・編集を見せる操作の入口なので、実在する `/state` が応答することを確認する。

  ```bash
  curl -fsS http://127.0.0.1:8920/state
  ```

- [ ] **stage.sh で kai-term / kai-brain が立つこと。** 配信に映る作業端末
      `kai-term` と、隠しの頭脳セッション `kai-brain` が作られ、VSCode 統合ターミナルが
      `kai-term` に attach されることを確認する。

  ```bash
  bash ~/kai-agent/kai-services/streaming/vm/stage.sh
  tmux has-session -t kai-term
  tmux has-session -t kai-brain
  ```

## 秘密・認証・通知ゲート（赤なら配信しない）

配信画面は VM デスクトップの**全画面キャプチャ**であり、画面に映った生ピクセルは
narrator / trace / speechd の三層マスクの対象外（Issue #76）。ターミナル・VSCode・
ダイアログに秘密が一瞬でも映れば漏洩なので、以下は 1 つでも赤なら配信を開始しない。

- [ ] **gh 認証が完了していること（配信中に認証画面を出さない）。** デバイスコード
      URL や OAuth 画面が配信中に出ると、コード・アカウント情報が画面に映る。
      認証は必ず配信前に完結させる（`m4-runbook.md` §2.1）。

  ```bash
  gh auth status
  ```

- [ ] **VSCode に機微ファイルのタブが無いこと。** `.env` / `~/.hermes/config.yaml` /
      鍵ファイルを開いたタブが残っていると、配信開始と同時に中身が映る。

  ```bash
  # ブリッジの /state にタブ一覧が出る。機微パスが 1 件も無ければ OK
  curl -fsS http://127.0.0.1:8920/state \
    | grep -Eic '\.env|config\.yaml|\.pem|id_rsa|id_ed25519|\.ssh' \
    && echo 'NG: 機微タブが開いている' || echo 'OK'
  ```

- [ ] **シェル履歴に秘密を表示するコマンドが残っていないこと。** 配信中に端末で
      履歴を遡る（Ctrl-R・上キー）と `printenv` や `cat .env` の実行が再現されうる。
      残っていたら配信前に履歴を消す。

  ```bash
  grep -En '(^|[;&| ])(printenv|env)([ ;&|]|$)|cat .*\.env' ~/.bash_history \
    && echo 'NG: 履歴に秘密表示コマンド' || echo 'OK'
  # 消す場合:
  history -c && > ~/.bash_history
  ```

- [ ] **デスクトップ通知・ダイアログが抑止されていること。** GNOME 通知（メール・
      アップデート）やキーリングのパスワードダイアログが配信画面に被さらないようにする
      （キーリングは空パスワードのデフォルトキーリング設定済みが前提）。

  ```bash
  gsettings set org.gnome.desktop.notifications show-banners false
  gsettings get org.gnome.desktop.notifications show-banners   # false なら OK
  ```

- [ ] **OBS の Settings 画面が閉じていること。** Settings > Stream には**ストリーム
      キーが平文で表示される**。配信前に Settings を閉じ、配信中は開かない
      （キーが映ったら即 `broadcast.sh stream-stop` → キー再発行）。

- [ ] **obs-websocket に認証（ServerPassword）が設定されていること。** 未設定だと
      tailnet/LAN から OBS を乗っ取り、`GetStreamServiceSettings` で RTMP キーを
      平文取得→配信ジャックできる（Issue #77 M-c）。あわせて Tailscale ACL /
      host firewall で 4455 を loopback 以外 drop にするのが望ましい。

  ```bash
  python3 ~/kai-agent/kai-services/streaming/vm/obsws.py --check-auth
  # => OK: obs-websocket 認証あり（NG なら OBS の Settings > WebSocket でパスワード設定）
  ```

## 公開配信（M5）前の追加ゲート

限定公開では tailnet/LAN 内前提で許容していたが、YouTube 公開配信の前に必須。

- [ ] **koe-bridge に共有トークンが設定されていること（Issue #77 C1）。** 非 loopback
      bind でトークン未設定なら起動しない（fail-closed）が、明示確認する。

  ```bash
  test -s ~/.config/kai/koe-bridge.env && grep -q '^KOE_BRIDGE_TOKEN=.' ~/.config/kai/koe-bridge.env \
    && echo 'OK' || echo 'NG: KOE_BRIDGE_TOKEN 未設定'
  ```

- [ ] **trace-web に認証トークンが設定されていること（Issue #77 H-a）。** trace-web は
      生に近い作業ログ＋スクショを配信するため、公開前にトークン必須。

  ```bash
  test -s ~/.config/kai/trace-web.env && grep -q '^TRACE_WEB_TOKEN=.' ~/.config/kai/trace-web.env \
    && echo 'OK' || echo 'NG: TRACE_WEB_TOKEN 未設定'
  ```

- [ ] **ネットワーク境界が多層防御になっていること（Issue #77・ops）。** アプリ側の
      認証（上記）に加え、Tailscale ACL / host firewall で到達範囲自体を絞る。
      loopback 前提のサービス（speechd 8900・VSCode ブリッジ 8920・typewriter・
      obs-websocket 4455）は tailnet からも遮断し、Tailscale 越しに使うもの
      （aquestalk 8890・trace-web 8910・koe-bridge 8930）だけを自端末に限定する。

  ```bash
  # VM: ufw で tailscale0 以外からの待受ポートを drop（例。実 IF 名は要確認）
  sudo ufw status verbose | grep -E '8900|8920|4455' && echo '↑ これらが tailnet に露出していないか確認'
  # Tailscale ACL: 8890/8910/8930 を Mac ⇔ kai-vm 間のみ許可（管理コンソールで設定）
  ```

## 配信後の後片付け

- [ ] `stage.sh` で起動した補助サーバを停止し、次回のポート衝突を防ぐ。
- [ ] `kai-term` / `kai-brain` の tmux セッションを終了し、古い端末状態を残さない。
- [ ] 配信用に起動した VSCode を停止し、次回の配信前チェックで新しい状態から立ち上げる。

## OK の基準

- 音声合成は health だけでなく、実際に WAV を生成できている。
- VM 内の音声・字幕経路と VSCode 操作経路がどちらも応答している。
- kai_ide の tool override が有効で、配信画面に kai の操作が見える状態になっている。
- `stage.sh` 実行後、配信に映る端末と隠しセッションの役割分担が崩れていない。
