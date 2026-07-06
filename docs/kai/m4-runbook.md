# M4 運転台本 — 配信モードの手順書

- **ステータス:** v0.4（2026-07-06 「本物の人間のライブコーディング」構成へ改訂。
  hermes を隠しセッションで動かし、kai_ide の自作ツールで VSCode/端末を実操作。
  冒頭説明は Desktop のブラウザで。stage.sh / kai-ask.sh を追加）
- **第 1 回リハーサル記録（2026-07-05）:** 限定公開で 17 分実施しオーナー判断で中断
  （課題が多いため）。配信自体は健全（ドロップ 0・切断なし）。幕 1〜2 は実施、
  縮退実演・幕 4〜5 は未実施。課題は Issues #7（TTS 読み品質）/ #8（演出）/
  #9（実況のパス整形）/ #10（無音対策）/ #11（ブラウザ操作）。解消済み。
- **第 2 回リハーサル記録（2026-07-05）:** 限定公開で 22 分実施（`test-live-02.md`）。
  配信健全（ドロップ 0・切断なし）。DoD 5 条件を技術的に全通過（TTS 縮退実演成功、
  kai が配信中に PR を 3 本自作 = #27/#28/#34）。読み上げ品質は改善確認。
  新規課題は Issues #29（指示プロンプトの映り込み）/ #30（コマンド実行の可視化）/
  #31（実況の質）/ #32（VSCode ステージ整備）。解消済み。
- **第 3 回リハーサル記録（2026-07-06）:** 限定公開で 7 分実施（`test-live-03.md`）。
  音声・1 行指示・実況の質・コマンド可視化を確認。方向性の指摘 = 「本物の人間の
  ライブコーディングに見せる」（Issues #46 指示プロンプトを隠す / #47 冒頭を Desktop
  ブラウザで / #48 コマンドを見える端末で実行 / #49 VSCode 自作ツール）。本 v0.4 で対応
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

### 3.1 ステージ（VSCode + 2 セッション。#46/#48/#49）

配信の画は **VSCode 全画面（エディタ + 統合ターミナル）+ 必要時ブラウザ**。
「本物の人間のライブコーディング」に見せるため、hermes（kai の頭脳）は**配信に
映さない隠しセッション**で動かし、配信画面には kai の実操作だけを出す:

- **kai の編集** → kai_ide が `write_file`/`patch` を override し、ディスク書込 +
  kai-typewriter でタイプ表示（VSCode に映る）
- **kai のコマンド** → kai_ide が `terminal` を override し、見える tmux セッション
  `kai-term`（VSCode 統合ターミナルに attach）で実際に実行（配管は `kai '...'`
  ヘルパーに隠す）
- **kai の応答・実況** → speechd で音声・字幕
- **オーナーの指示** → `kai-ask.sh` で隠しセッション `kai-brain` へ（配信に映らない）

ステージ準備は 1 コマンド（内部で 2 セッション作成・VSCode 起動・整形・attach を行う）:

```bash
# 初回のみ: bash ~/kai-agent/kai-services/streaming/vm/setup-vscode.sh
bash ~/kai-agent/kai-services/streaming/vm/stage.sh
```

`stage.sh` は `kai-term`（配信に映る作業ターミナル）と `kai-brain`（隠しの頭脳）を
作り、VSCode を最大化・残タブとチャットパネルを閉じ、統合ターミナルで `kai-term` に
attach し、ブリッジ疎通まで確認する。

### 3.2 配信開始

```bash
cd ~/kai-agent/kai-services/streaming/vm
bash broadcast.sh start      # OBS 起動 + websocket 疎通待ち（まだ配信されない）
bash broadcast.sh scene "シーン"   # メイン（Desktop の画面キャプチャ）
# UTM ウィンドウ（または x11vnc）で画面・音声メーターを目視確認してから:
bash broadcast.sh stream-start
bash broadcast.sh status     # 「配信中 hh:mm:ss」を確認
```

- YouTube Studio（Mac 側ブラウザ）で受信状態と**公開範囲 = 限定公開**を確認
- 配信開始から 2〜3 分は YouTube 側のプレビューで音声・字幕の乗りを確認

OBS シーンの前提: メインの「シーン」= Desktop 全体の画面キャプチャ + 字幕
ブラウザソース。**冒頭スライド（kai-slide ブラウザソース）は廃止**（#47）— 冒頭説明は
kai が `stream-browser` で Desktop にブラウザを開いて見せる（§4 幕 1）。

### 3.3 モデル構成 / ツール構成

- kai 本体（コーディング）: gpt-5.5（openai-codex）
- 実況（narration）・koe 生成: gpt-5.4-mini（auxiliary.\* / koe-bridge 経由）
- **kai_ide の自作ツール**（VSCode 実操作。config で `plugins.entries.kai_ide.allow_tool_override: true` が必要）:
  `terminal`/`write_file`/`patch` を override、`vscode_state`/`vscode_open`/`vscode_close_tab`
- LLM 不達時: koe はルールベースへ、実況はスキップへ。ブリッジ/tmux 不在時は
  各ツールが built-in にフォールバック（作業は止めない）

## 4. 運転台本（kai への指示テンプレート）

配信の本編は次の 5 幕。**指示はすべて `kai-ask.sh` で隠しセッションへ送る**
（配信画面には出ない #46）。SOUL.md に手順が入っているので指示は短くてよい。

```bash
K=~/kai-agent/kai-services/streaming/vm/kai-ask.sh
```

### 幕 1: 挨拶 + 今日やることを Desktop のブラウザで見せる（#47）

```bash
bash "$K" "配信を始めます。視聴者に向けて自分が何者かを2文で挨拶して、\
今日やる GitHub Issue #<N> を stream-browser で開いて、何をするのか説明してください"
```

kai が `stream-browser.py open` で Issue を Desktop のブラウザに表示し、口頭で説明する
（OBS のブラウザソースは使わない）。

### 幕 2〜3: Issue 実装 → PR 作成（本編）

SOUL.md に Issue 対応の標準手順（feature ブランチ / Conventional Commits /
verify.sh 緑 / PR / verify.sh --pr / マージしない / 配信中は broadcast.sh の
危険サブコマンドを実行しない / 節目で実況）が入っているので、指示は 1 行でよい:

```bash
bash "$K" "#<N> の対応を行う"
```

- kai の編集は VSCode にタイプ表示、コマンドは kai-term で実際に実行される
- 目安 30〜40 分。停滞したら追加指示で軌道修正（`kai-ask.sh` で送るので配信に
  指示は映らない）。例: `bash "$K" "テストが落ちている原因を先に調べて"`

### 幕 4: まとめ

```bash
bash "$K" "今日の作業のまとめを、やったこと・PR の状態・積み残しを視聴者向けに3〜5文で話して"
```

### 幕 5: 締めの挨拶

```bash
bash "$K" "配信を終わります。締めの挨拶をお願いします"
```

## 5. 監視と介入（DoD 条件 5）

- **監視:** UTM ウィンドウ（Mac 上）が主。リモート時は Tailscale 経由の x11vnc。
  発話・字幕・作業イベントの時系列は trace-viewer
  （`http://<kai-vm の Tailscale IP>:8910/`、「ライブ追従」オン）で見る
- **介入手段:** `kai-ask.sh` での追加指示（配信に映らない #46）/ 隠しセッション
  `kai-brain` で `Ctrl-C`（kai の中断）。指示・介入は配信画面に出ない
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
