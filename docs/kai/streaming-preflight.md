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

  ```bash
  curl -fsS http://127.0.0.1:8900/health
  curl -fsS http://127.0.0.1:8901/health
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
      タブを閉じる・編集を見せる操作の入口なので、ローカル HTTP が応答することを確認する。

  ```bash
  curl -fsS http://127.0.0.1:8920/health
  ```

- [ ] **stage.sh で kai-term / kai-brain が立つこと。** 配信に映る作業端末
      `kai-term` と、隠しの頭脳セッション `kai-brain` が作られ、VSCode 統合ターミナルが
      `kai-term` に attach されることを確認する。

  ```bash
  bash ~/kai-agent/kai-services/streaming/vm/stage.sh
  tmux has-session -t kai-term
  tmux has-session -t kai-brain
  ```

## OK の基準

- 音声合成は health だけでなく、実際に WAV を生成できている。
- VM 内の音声・字幕経路と VSCode 操作経路がどちらも応答している。
- kai_ide の tool override が有効で、配信画面に kai の操作が見える状態になっている。
- `stage.sh` 実行後、配信に映る端末と隠しセッションの役割分担が崩れていない。
