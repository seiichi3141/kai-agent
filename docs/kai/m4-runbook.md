# M4 運転台本 — 配信モードの手順書

- **ステータス:** v0.1（リハーサルで検証し改訂する）
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
# VM 内サービス
systemctl --user is-active speechd.service kai-overlay.service
# 音声・字幕の end-to-end（VM のデフォルトシンク kai_speaker に乗り、字幕が出ること）
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"はいしんまえチェックです"}'
# Mac TTS
curl -s -o /dev/null -w '%{http_code}\n' http://<mac-tts>:8890/health || echo "TTS 縮退運転になる"
# 実況 LLM（sei-win）
curl -s -o /dev/null -w '%{http_code}\n' http://<sei-win>:8080/v1/models
# kai 本体（1 コマンド素振り。narrator の実況が音声+字幕で出ること）
cd ~/kai-agent && .venv/bin/hermes "こんにちは。今日の配信の準備確認です。挨拶だけしてください"
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

## 3. 配信開始手順

```bash
cd ~/kai-agent/kai-services/streaming/vm
bash broadcast.sh start      # OBS 起動 + websocket 疎通待ち（まだ配信されない）
# UTM ウィンドウ（または x11vnc）でシーン・音声メーターを目視確認してから:
bash broadcast.sh stream-start
bash broadcast.sh status     # outputActive: true を確認
```

- YouTube Studio（Mac 側ブラウザ）で受信状態と**公開範囲 = 限定公開**を確認
- 配信開始から 2〜3 分は YouTube 側のプレビューで音声・字幕の乗りを確認

## 4. 運転台本（kai への指示テンプレート）

配信の本編は次の 5 幕。各幕の指示は VM 内ターミナル（配信に映る画面）で kai に渡す。

### 幕 1: 挨拶

```text
こんにちは、kai です。今日はライブ配信で、このリポジトリの Issue を 1 件実装します。
まず視聴者に向けて、自分が何者か・今日やることを 3 文程度で挨拶してください。
```

### 幕 2〜3: Issue 実装 → PR 作成（本編）

```text
GitHub Issue #<N>（<タイトル>）を実装してください。進め方:
- 作業は feature ブランチで行い、コミットは Conventional Commits
- 完了の根拠は scripts/kai/verify.sh の緑（自己申告しない）
- 仕上げに PR を作成し、verify.sh --pr で CI 緑と mergeable を確認する
- マージはしない（オーナーが配信後に確認してマージする)
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

- **監視:** UTM ウィンドウ（Mac 上）が主。リモート時は Tailscale 経由の x11vnc
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
