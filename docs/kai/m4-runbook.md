# M4 運転台本 — 配信モードの手順書

- **ステータス:** v0.3（2026-07-05 配信ステージを VSCode 構成へ改訂。冒頭スライド・
  モデル使い分け・trace-viewer 監視を追加）
- **第 1 回リハーサル記録（2026-07-05）:** 限定公開で 17 分実施しオーナー判断で中断
  （課題が多いため）。配信自体は健全（ドロップ 0・切断なし）。幕 1〜2 は実施、
  縮退実演・幕 4〜5 は未実施。課題は Issues #7（TTS 読み品質）/ #8（演出）/
  #9（実況のパス整形）/ #10（無音対策）/ #11（ブラウザ操作）。
  再リハーサルはこれらの解消後
- **前提:** `docs/kai/mvp-plan.md` §M4。操作はすべてオーナーが手動実行（MVP）
- **場所:** 特記なければ kai-vm 上（`ssh kai@<kai-vm>` または UTM ウィンドウ内ターミナル）

## 1. ゴール（この台本で満たす DoD）

`mvp-plan.md` §1.1 の 5 条件を 1 時間のリハーサル配信（**限定公開**)で通しで満たす:

1. 1 時間の連続配信（映像 = デスクトップ、音声 = AquesTalk、下部字幕）
2. kai が実 Issue を 1 件処理（実装 → PR 作成）する様子が映る
3. 行動が日本語の一言実況として音声 + 字幕に流れる
4. TTS 到達不能時に字幕のみで縮退し配信が止まらない
5. オーナーがリモートでデスクトップを監視・操作できる

## 2. 事前チェックリスト（配信開始前・オフライン状態で行う）

### 2.1 機密・認証の運転ルール（★配信に映ってはいけないもの）

- **すべての認証を配信前に済ませる。** OAuth のデバイスコード・確認 URL・トークンは
  配信画面に映してはならない。`gh auth status` と kai の LLM 接続を先に確認する
- 配信中にエディタ・`cat` で開いてはいけないファイル: `.env`、`~/.hermes/config.yaml`
  （API キー・obs-websocket パスワード等を含む設定全般）
- 認証切れが配信中に起きた場合は**再認証しない**。そのタスクは字幕で断って打ち切り、
  配信終了後に対応する
- 通知・ポップアップ類（GNOME 通知、キーリングダイアログ）が出ない状態を確認する

### 2.2 サービス疎通（すべて緑になるまで配信しない)

```bash
# VM 内サービス（kai-overlay.service は廃止済み・inactive が正常。字幕は
# OBS ブラウザソース → speechd の /overlay/ 経路）
systemctl --user is-active speechd.service koe-bridge.service trace-viewer.service
# 音声・字幕の end-to-end（VM のデフォルトシンク kai_speaker に乗り、字幕が出ること）
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"はいしんまえチェックです"}'
# Mac TTS
curl -s -o /dev/null -w '%{http_code}\n' http://<mac-tts>:8890/health || echo "TTS 縮退運転になる"
# 実況 LLM（sei-win）
curl -s -o /dev/null -w '%{http_code}\n' http://<sei-win>:8080/v1/models
# kai 本体（1 コマンド素振り。narrator の実況が音声+字幕で出ること。-z 必須）
cd ~/kai-agent && .venv/bin/hermes -z "こんにちは。今日の配信の準備確認です。挨拶だけしてください"
```

- ディスク空き（`df -h ~`）と、OBS シーンにブラウザソース `kai-overlay-browser` が
  あることを確認（`broadcast.sh start` → UTM ウィンドウで目視）
- Mac 側で音をモニタしたい場合のみ loopback を張る（揮発。手順はメモリ/運用ノート参照）

### 2.3 縮退の事前確認（DoD 条件 4）

リハーサルでは配信中に 1 回、意図的に TTS を落として字幕のみ縮退を実演する:

```bash
# Mac 側で: launchctl unload ~/Library/LaunchAgents/com.kai.aquestalk-server.plist
# → VM で /say → 字幕のみ出て配信が続くことを確認 → launchctl load で復旧
```

## 3. 配信ステージの準備と配信開始

### 3.1 ステージ（VSCode + tmux）

配信の画は **VSCode 全画面（エディタ + 統合ターミナル）**。kai の編集は
kai_director → kai-typewriter 拡張がタイピング風に再生し、実行コマンドは
コマンドログのペインに `$ ...` で流れ、kai の応答は音声・字幕に出る。

tmux は 2 ペイン: 上ペイン = kai セッション（指示を渡す・kai の出力）、
下ペイン = コマンドログの `tail -f`（kai が内部実行したコマンドが見える。Issue #30）。

```bash
# 初回のみ: bash ~/kai-agent/kai-services/streaming/vm/setup-vscode.sh
: > ~/.config/kai/command-log   # コマンドログを空にしてから始める
tmux new-session -d -s kai-stream -x 200 -y 30 "cd ~/kai-agent && exec bash"
tmux split-window -v -t kai-stream -l 8 "tail -f ~/.config/kai/command-log"
tmux select-pane -t kai-stream.0
systemd-run --user --unit=kai-vscode --collect --setenv=DISPLAY=:0 \
  code --disable-gpu --wait ~/kai-agent

# VSCode を最大化し、前セッションの残タブと右のチャットパネルを閉じる（Issue #32）:
VSWIN=$(wmctrl -lx | awk '$3 ~ /^code\./ {print $1; exit}')
wmctrl -i -r "$VSWIN" -b add,maximized_vert,maximized_horz
xdotool key --window "$VSWIN" ctrl+k ctrl+w              # 全エディタを閉じる（残タブ整理）
xdotool key --window "$VSWIN" --clearmodifiers ctrl+shift+p   # コマンドパレット
xdotool type --delay 40 "View: Close Secondary Side Bar"; xdotool key Return  # 右チャット非表示
# 統合ターミナルを開いて tmux に attach（下ペインにコマンドログが流れる）:
xdotool key --window "$VSWIN" ctrl+grave
xdotool type --delay 50 "tmux attach -t kai-stream"; xdotool key Return

# 拡張の疎通確認:
curl -s -X POST http://127.0.0.1:8920/edit -d '{"edits": []}'   # {"queued":0}
```

### 3.2 アジェンダと配信開始

```bash
cd ~/kai-agent/kai-services/streaming/vm
bash broadcast.sh agenda "今日やること 1" "今日やること 2"   # 冒頭スライドの内容
bash broadcast.sh start      # OBS 起動 + websocket 疎通待ち（まだ配信されない）
bash broadcast.sh scene kai-slide   # 冒頭はスライドから
# UTM ウィンドウ（または x11vnc）でスライド・音声メーターを目視確認してから:
bash broadcast.sh stream-start
bash broadcast.sh status     # 「配信中 hh:mm:ss」を確認
```

- YouTube Studio（Mac 側ブラウザ）で受信状態と**公開範囲 = 限定公開**を確認
- 配信開始から 2〜3 分は YouTube 側のプレビューで音声・字幕の乗りを確認
- スライドのまま幕 1（挨拶 + 予定紹介）→ 終わったらメインへ:
  `bash broadcast.sh scene "シーン"`（VSCode デスクトップの画面キャプチャ）

OBS シーンの前提（初回のみ作成）: `kai-slide` = ブラウザソース
`http://127.0.0.1:8900/overlay/slide.html`（1920x1080）、メインの「シーン」=
画面キャプチャ + 字幕ブラウザソース（既存）。

### 3.3 モデル構成（2026-07-05 確定）

- kai 本体（コーディング）: gpt-5.5（openai-codex）
- 実況（narration）・koe 生成: gpt-5.4-mini（auxiliary.\* / koe-bridge 経由）
- LLM 不達時: koe はルールベース変換へ、実況はスキップへ自動縮退

## 4. 運転台本（kai への指示テンプレート）

配信の本編は次の 5 幕。各幕の指示は VM 内ターミナル（配信に映る画面）で kai に渡す。

### 幕 1: 挨拶（スライドを映したまま）

```text
こんにちは、kai です。今日はライブ配信です。まず視聴者に向けて、自分が何者かを
2 文程度で挨拶して、画面に出ている「本日の予定」を順に紹介してください。
```

挨拶が終わったら `broadcast.sh scene "シーン"` でメイン画面（VSCode）へ切替。

### 幕 2〜3: Issue 実装 → PR 作成（本編）

```text
GitHub Issue #<N>（<タイトル>）を実装してください。進め方:
- 作業は feature ブランチで行い、コミットは Conventional Commits
- 完了の根拠は scripts/kai/verify.sh の緑（自己申告しない）
- 仕上げに PR を作成し、verify.sh --pr で CI 緑と mergeable を確認する
- マージはしない（オーナーが配信後に確認してマージする)
- いままさに配信中のため、broadcast.sh の stop / stream-stop / stream-start /
  start / scene は絶対に実行しないこと（status と screenshot は使用可）
- 作業の節目で何をしているか一言ずつ話すこと
```

- 目安 30〜40 分。停滞したら追加指示で軌道修正（指示もそのまま配信に映って良い）

### 幕 4: まとめ

```text
今日の作業のまとめをお願いします。やったこと・PR の状態・積み残しを
視聴者向けに 3〜5 文で話してください。
```

### 幕 5: 締めの挨拶

```text
配信を終わります。締めの挨拶をお願いします。
```

## 5. 監視と介入（DoD 条件 5）

- **監視:** UTM ウィンドウ（Mac 上）が主。リモート時は Tailscale 経由の x11vnc。
  発話・字幕・作業イベントの時系列は trace-viewer
  （`http://<kai-vm の Tailscale IP>:8910/`、「ライブ追従」オン）で見る
- **介入手段:** VM 内ターミナルでの追加指示 / `Ctrl-C`（kai の中断）。
  介入自体は配信に映って構わない（開発配信の一部として扱う）
- **緊急時（機密が映った・映りそう）:** 迷わず `broadcast.sh stream-stop`。
  配信停止が最優先、原因調査は停止後

## 6. 異常時の対応

| 事象                    | 挙動                             | 対応                                                                     |
| ----------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| Mac TTS 落ち            | 字幕のみで自動縮退（配信は継続） | そのまま続行可。復旧は Mac 側 launchctl                                  |
| 実況 LLM（sei-win）落ち | 実況が止まる（kai 本体は動く）   | 続行可。narrator は次回発話から自動復帰                                  |
| kai の暴走・停滞        | —                                | `Ctrl-C` → 幕 2 の指示を絞って再投入                                     |
| OBS 配信の不調          | status の outputReconnecting 等  | `broadcast.sh status` で確認 → 必要なら stream-stop → start からやり直し |
| 機密の露出（疑い含む)   | —                                | 即 `stream-stop`。§5 参照                                                |

## 7. 終了手順と証跡（P5）

```bash
bash broadcast.sh stream-stop   # 配信停止（OBS は起動のまま）
bash broadcast.sh stop          # OBS クリーン終了（シーン保存はこの経路のみ）
```

- YouTube Studio でアーカイブの生成を確認（リハーサルは限定公開のまま残す）
- 証跡の保存: `~/.hermes/kai_trace/<日付>.jsonl`、OBS ログ
  （`~/.config/obs-studio/logs/`）、配信アーカイブ URL、気づきのメモ
- 振り返り: DoD 5 条件の充足を 1 つずつ判定し、本書 v0.2 に反映して M5（公開初配信）へ
