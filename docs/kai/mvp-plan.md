# kai MVP 計画 — 最低限のライブ配信まで

- **ステータス:** ドラフト（v0.1）
- **作成日:** 2026-07-03
- **前提:** `docs/kai/requirements.md` v0.2（確定方針 §9.1 を含む）

---

## 1. MVP の定義

### 1.1 ゴール（Definition of Done）

> **kai が YouTube でライブ配信を行い、Linux デスクトップ上で実際の開発作業を、AquesTalk 音声と日本語字幕の実況つきで見せられる。**

具体的な完了条件:

1. YouTube Live で **1時間の連続配信**が成立する（映像 = Linux デスクトップ、音声 = AquesTalk、画面下部に字幕）。
2. 配信中に kai が**実タスクを1件**（小さな GitHub Issue の実装 → PR 作成まで）処理する様子が映る。
3. kai の行動（ツール実行・思考）が**日本語の一言実況**として音声 + 字幕に流れる。
4. TTS（Mac）到達不能時に**字幕のみで縮退**し、配信が止まらない。
5. オーナーが Tailscale 経由のリモート GUI でデスクトップを操作・監視できる。

### 1.2 MVP に含めないもの（スコープ外）

| 項目                                                                         | 理由 / 時期                                                              |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| アバター（2D PNG・カーソル追従）                                             | 演出強化はポスト MVP。静止画 PNG を OBS に置く程度は任意                 |
| YouTube チャット読取・応答                                                   | ポスト MVP（フェーズ4）。MVP はチャット無視でよい                        |
| 自律開発ループ全体（スケジューラ、WorkItem、役割切替によるレビュー・マージ） | フェーズ1 本実装で対応。MVP は「オーナーが指定した Issue を1件やる」で可 |
| Beat 同期・表情・リアクション・BGM                                           | 簡易版（発話と字幕の同時表示）で代替                                     |
| X 運用・ニュース収集・視聴者プロファイル・合議制                             | フェーズ4-5                                                              |
| 配信の自動スケジュール（cron 起動）                                          | MVP は手動開始で可。M6 以降                                              |
| upstream 自動追従                                                            | 当面は手動 merge（`upstream/main` → `upstream` → `main`）                |

### 1.3 アーキテクチャ（MVP 時点）

```text
┌── オーナーの Mac（M4 Pro / 12コア / 24GB、スリープ無効運用）────────┐
│                                                                      │
│  UTM VM「kai-vm」（Ubuntu 24.04 Desktop arm64 / 6CPU / 12GB）         │
│  │  本物の Ubuntu GNOME デスクトップ（X11・自動ログイン・1920x1080）  │
│  │  ├─ kai（hermes fork, main）… M1 でここに導入予定                 │
│  │  ├─ OBS … 画面キャプチャ(XSHM) → RTMPS → YouTube                  │
│  │  ├─ 音声: PipeWire null-sink kai_speaker（デフォルトシンク）      │
│  │  └─ Chromium（snap・本物）                                        │
│  │  リモート操作: Tailscale SSH / xdotool / UTM ウィンドウ            │
│  │                                                                   │
│  Mac ネイティブ:                                                      │
│  └─ AquesTalk10 合成サービス（aquestalk_cli + 小型 HTTP ラッパ）      │
└──────────────┬───────────────────────────────────────────────────────┘
               │ Tailscale / LAN
               ▼
         自宅 Windows: ローカル LLM（OpenAI 互換）
```

（変遷: Oracle A1 → Mac Docker → **Mac UTM VM**（2026-07-04 確定）。「本物の GNOME」要望により VM 化。旧資産は温存: OCI 用 `setup.sh`/`units/`/`oracle/`、Docker 用 `docker/`）

kai 固有コードの配置は fork 運用原則に従う（コア無改変）:

- **narrator（実況変換）** → hermes **plugin**（`post_tool_call` 等の lifecycle hook で行動イベントを取得）
- **配信制御（OBS 起動・配信開始/終了）** → **CLI コマンド + skill**（シェルスクリプト + obs-cli / obs-websocket）
- **TTS クライアント / 字幕更新** → narrator plugin から呼ぶ薄いクライアント（Mac の HTTP サービスへ）
- **Mac TTS サービス / 配信スタック（X, OBS, audio）** → **hermes 外の独立コンポーネント**

---

## 2. マイルストーン

各マイルストーンは「検証（runtime acceptance）」を満たして完了とする（プロトタイプの品質文化を踏襲）。

### M0: インフラ PoC — Mac UTM VM + 配信経路の技術検証

**目的:** 配信スタック（Ubuntu デスクトップ + OBS + RTMP）の成立を検証する。

- UTM VM（Ubuntu 24.04 Desktop arm64）を作成し `kai-services/streaming/vm/setup.sh` を適用
- OBS（XSHM 画面キャプチャ + kai_speaker）でテスト配信（限定公開・30 分）を安定完走
- 詳細な構成・既知の問題（GPU アクセラレーション不可等）は `docs/kai/design/streaming.md` v0.3

**検証結果（2026-07-04）:** VM 上で GNOME/X11/自動ログイン/1920x1080/kai_speaker/OBS(llvmpipe) すべて動作。
配信開始成功（2512kbps / 30fps / ドロップ 0%）。xdotool によるリモート UI 操作も実証済み。

### M1: kai 本体のデプロイと LLM 接続

**目的:** hermes fork がサーバー上で 3 つの LLM バックエンドと会話できる状態。

- `main` をサーバーへ clone、venv 構築、`hermes` セットアップ
- `config.yaml` で LLM 接続を確認:
  - ローカル LLM: `provider: custom` + `base_url: http://<windows-tailscale-ip>:<port>/v1`
  - Claude: Anthropic API（`CLAUDE_CODE_OAUTH_TOKEN` 再利用）
  - Codex: `openai-codex` provider
- skin で kai の人格（「多脚思考 AI」、一人称「ボク」）の初期版を当てる
- `gh` CLI 認証、対象リポジトリの clone（まず kai-agent 自身のみ）
- systemd（または pm2 相当）で常駐化、再起動後の自動復帰確認
- **kai-trace plugin の骨格を導入**（F-22 全事象ロギング）: lifecycle hook で LLM 呼び出し・ツール実行・セッションを構造化ログとして永続化し、`save_trajectories` を有効化。**以後の全マイルストーンでデータが貯まる状態**にする（振り返りループの分析対象は後から遡れないため、収集だけ先行させる）
- **ストア構成（要件 §9.2-5 の段階1）**: 正本 = 追記専用 JSONL（全プロセス共通、相関 ID つき）、検索層 = SQLite（WAL + FTS5）。PostgreSQL はフェーズ1 本実装まで導入しない

**検証:** サーバー上の kai に CLI から日本語で指示し、3 バックエンドすべてで応答・ツール実行（terminal/file）ができる。Tailscale 越しのローカル LLM 応答レイテンシを記録。実行したタスクのツール呼び出し・LLM 呼び出しがトレースに記録されている。

### M2: 音声・字幕パイプライン

**目的:** 「テキストを渡すと、配信に AquesTalk 音声と字幕が乗る」を成立させる。

- **Mac 側:** `aquestalk_cli`（プロトタイプ資産）を包む小型 HTTP サービス（`POST /synthesize` → WAV 返却）。Tailscale IP のみ bind。launchd で常駐 + `caffeinate` 等でスリープ対策
- **サーバー側:** 発話キュー（FIFO・逐次再生）を持つ小型プロセス:
  1. テキスト受領 → Mac へ合成リクエスト → WAV を null-sink に再生
  2. 再生開始と同時に OBS の字幕テキストソースを更新（obs-websocket）、再生終了で字幕クリア
  3. **Mac 不達（タイムアウト）時は字幕のみ表示して続行**（縮退の実装）
- 読み上げ変換はまず kuromoji 相当の簡易処理 or ローカル LLM で koe 変換（プロトタイプの `tts-conversion-design` を参考に最小実装）

**検証:** `curl` でテキストを投げ、配信画面に音声 + 字幕が同期して乗る。Mac の Tailscale を切断しても字幕のみで継続する。

**ストレッチ（任意）:** [PuruPuruPNGTuber](https://github.com/seiichi3141/PuruPuruPNGTuber) をサーバー上の Chromium で起動し、TTS 再生音声（null-sink monitor）を仮想マイクとして与えて**口パクつきアバター**をデスクトップ上に表示する。同アプリは口パク（マイク音量駆動）・まばたき・髪揺れ・OBS 向け透過表示を既に備えるため、音声パイプが動けば追加実装なしで載る見込み。素材（表情差分 PNG + 前髪/後ろ髪）は Codex imagegen で事前制作。カーソル追従・表情のプログラム制御はポスト MVP。

### M3: narrator 最小版 — kai の行動を実況に変換

**目的:** kai が作業すると、自動で実況が配信に流れる状態。

- hermes **plugin** として narrator を実装（コア無改変）:
  - lifecycle hook（`post_tool_call` / `post_llm_call`）で AgentActivity（ツール実行・思考の要約）を収集（**M1 の kai-trace plugin と同じイベント源を共有**する設計にし、hook の二重実装を避ける）
  - バッファリングし、ローカル LLM で「視聴者向けの一言日本語実況」に変換（kickoff / work / summary の 3 モードの簡易版）
  - M2 の発話キューへ送出
- 発話頻度の制御（連続発話の抑制、同一内容の重複抑制）は最小限でよい
- 秘匿情報ガード: 実況テキストに env 値・トークン形式（`sk-`, `ghp_` 等）が混入しないフィルタを必ず入れる

**検証:** kai に小タスク（例: README の誤字修正）を指示し、着手宣言 → 作業実況 → 完了報告が音声・字幕で流れる。実況に秘匿情報・生のコマンド出力が漏れない。

### M4: 配信運転 — 開始/終了の手順化と1件の実タスク

**目的:** 「配信を立てて、kai が Issue を1件こなして、配信を締める」を通しで実行可能にする。

- 配信制御 skill / CLI: OBS 起動 → 配信開始 →（終了時）配信停止 の一括コマンド（obs-websocket 経由）。MVP はオーナーが手動実行
- 「配信モード」の運転台本: kai への指示テンプレート（挨拶 → 指定 Issue の実装 → PR 作成 → まとめ → 挨拶）
- 事前に用意した**小さな実 Issue**（kai-agent リポジトリの軽微な改善）を kai が実装 → PR 作成まで実行
- 配信中の監視動線: オーナーが x11vnc で監視、異常時に介入できること

**検証（= MVP の Definition of Done）:** §1.1 の 5 条件を満たす**リハーサル配信（限定公開・1時間）**を完走する。

### M5: 公開初配信（MVP 完了）

- リハーサルの改善点を反映して、公開でのライブ配信を実施
- 配信アーカイブ・CPU/メモリ・エラーを振り返り、ポスト MVP（フェーズ1 本実装 = 自律開発ループ、アバター、チャット応答）の優先順位を再確定

---

## 3. 実施順序と目安

依存関係: M0 → M1 →（M2 と M3 は並行可、M3 の結合は M2 完了後）→ M4 → M5。

| マイルストーン  | 主な作業場所      | 目安    |
| --------------- | ----------------- | ------- |
| M0 インフラ PoC | Mac / UTM VM      | 済み    |
| M1 kai デプロイ | サーバー + config | 1 日    |
| M2 音声・字幕   | Mac + サーバー    | 1〜2 日 |
| M3 narrator     | hermes plugin     | 2〜3 日 |
| M4 配信運転     | 結合・リハーサル  | 1〜2 日 |
| M5 公開初配信   | —                 | 1 日    |

合計の目安: **実働 7〜11 日**（ARM 起因のトラブルシュートで前後）。

---

## 4. リスクと対策

| リスク                                    | 影響               | 対策                                                                                                     |
| ----------------------------------------- | ------------------ | -------------------------------------------------------------------------------------------------------- |
| OBS / エンコードが ARM で不安定・性能不足 | M0 で判明          | ffmpeg x11grab 直配信に切替。720p に解像度を落とす。最悪 GCP x86_64 へ移行（構成は流用可）               |
| Oracle A1 の容量確保難・無料枠変更        | 開始遅延           | PAYG 化で確保率改善。取れない場合は GCP 停止運用で開始し後日移行                                         |
| YouTube ライブ有効化の 24h 待ち           | M0 遅延            | M0 の最初に有効化申請しておく                                                                            |
| Mac スリープ / 不達で音声停止             | 配信品質低下       | 字幕のみ縮退（M2 で必須実装）。launchd + caffeinate。将来 Linux 開発ライセンス追加でサーバー内合成へ移行 |
| 実況への秘匿情報混入                      | 重大（配信は公開） | M3 のフィルタ必須。`.env` はデスクトップに表示しない運用（エディタで開かない等の運転ルール）             |
| computer_use（cua-driver）の ARM 非対応   | GUI 操作デモの制限 | MVP は terminal / browser（headless→headed 表示）中心で成立。GUI 操作はポスト MVP で xdotool 代替を検討  |
| Tailscale 越しローカル LLM のレイテンシ   | 実況の遅延         | 実況は非同期キューなので配信は止まらない。narrator 用は軽量モデルを使用                                  |

---

## 5. ポスト MVP への接続

MVP 完了後、`docs/kai/requirements.md` のロードマップに接続する:

1. **フェーズ1 本実装**: 自律開発ループ（統一スケジューラ・WorkItem・役割切替による PR ゲート）→ kai が Issue を「自分で拾い」、実装 → レビュー（隔離コンテキスト）→ 承認・マージまで単一エージェントで回す
   1-b. **振り返りループ（F-30）**: MVP 期間中に貯めたトレースを対象に、cron + 振り返り skill（日次の振り返りレポート → 失敗パターン抽出 → 改善提案）を稼働させる。MVP 直後の最初の振り返りは「MVP 配信自体のレトロスペクティブ」を kai 自身に行わせる
2. **アバター**: PuruPuruPNGTuber ベースで本実装。(a) 透過・クリックスルー・最前面の Chromium ウィンドウを xdotool で X カーソル座標に追従させる「アバター＝カーソル」デーモン、(b) narrator → アバターの表情制御チャネル（WebSocket 等）を PuruPuruPNGTuber 側に拡張（kai 自身の開発題材＝配信コンテンツにする）
3. **チャット応答**: `plugins/platforms/youtube_live/` アダプタ
4. **配信自動化**: cron による配信スケジュール、YouTube Data API での broadcast 管理
