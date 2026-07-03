# kai システム基本設計書

- **ステータス:** ドラフト（v0.1）
- **作成日:** 2026-07-03
- **前提:** `docs/kai/requirements.md` v0.3 / `docs/kai/mvp-plan.md`
- **検討過程:** Opus（deep-reasoner）と Codex に同一の設計課題を独立検討させ、両者の結論を統合した（本書 §2 の決定記録参照）

本書は kai 全体のアーキテクチャと、全コンポーネント設計が従う**共通規約の正典**である。個別コンポーネントの設計は `docs/kai/templates/component-design.md` に従い、本書を参照する。

---

## 1. 全体構成

```
┌─ Oracle Cloud ARM A1（Ubuntu 24.04 arm64・東京）──────────────────────┐
│                                                                        │
│  hermes gateway（systemd 常駐・単一プロセス）                            │
│  ├─ cron scheduler … kai-tick ジョブ（自律開発ループの駆動）             │
│  ├─ plugins/kai_trace     … 全事象ロギング（hook 観測 → JSONL/SQLite）  │
│  ├─ plugins/kai_narrator  … 実況生成（hook 観測 → 補助LLM → speechd）   │
│  └─ (後日) plugins/platforms/youtube_live … チャット入出力              │
│                                                                        │
│  kai-services/（独立プロセス群・systemd）                                │
│  ├─ speechd    … 発話・字幕キュー（TTS 呼出・再生・OBS 字幕・同期の正典） │
│  ├─ streaming  … X(dummy) デスクトップ + OBS + RTMP（セットアップ/制御）  │
│  └─ avatar     … PuruPuruPNGTuber オーバーレイ（M2 ストレッチ〜ポストMVP）│
│                                                                        │
│  X DISPLAY=:0（1920x1080）… kai の作業デスクトップ = 配信映像            │
│  PipeWire null-sink … speechd が再生 → OBS が取込 → YouTube RTMP        │
└──────────────────────┬─────────────────────────────────────────────────┘
                       │ Tailscale（全通信を私設網内に閉じる。公開ポートなし）
        ┌──────────────┴──────────────┐
        ▼                             ▼
   自宅 Windows                      Mac
   ローカル LLM（OpenAI 互換）        kai-services/mac-aquestalk
   … 実況生成・意図分類など           … AquesTalk10 HTTP 合成サービス
     補助タスク専用                    （launchd 常駐）
```

**GitHub** が作業状態の唯一のソース・オブ・トゥルース（Issue / PR / CI / レビュー状態）。ローカルに作業キューの複製を持たない。

---

## 2. アーキテクチャ決定記録（ADR）

### ADR-1: 実況は「kai 自身の応答」+「hook 観測 → 補助 LLM」のハイブリッド（F-7）

**決定:**

1. kai の**最終応答テキスト**（ターン完了時の発言。作業開始宣言・完了サマリ等）を、そのまま発話ビートとして speechd に送る。人格は skin + ペルソナコンテキストで kai 自身に備わっているため変換不要。追加 LLM コストゼロ。
2. **ツール実行中の無音区間**は、`kai_narrator` plugin が lifecycle hook（`post_tool_call` 等）で行動イベントを捕捉し、**auxiliary LLM タスク `narration`**（ローカル LLM に割当）で短い一人称実況に変換して speechd に送る。頻度制御（レート制限・重複抑制）を行う。

**根拠:**

- hermes の plugin hook は CLI / gateway / **cron / delegation subagent の全 surface で発火**する（`tools/delegate_tool.py` が子も `run_conversation` を通すため）。どの駆動方式でも実況が流れる。
- hook は観測専用でプロンプトキャッシュに触れない。auxiliary LLM 呼び出し（`agent/auxiliary_client.py`）は会話外の独立メッセージで、メイン会話を汚染しない。`ctx.register_auxiliary_task("narration", ...)` + `auxiliary.narration.*` 設定で実況専用にローカル LLM を割当できる。
- kai の応答を使うことで人格の二重化（メイン人格 vs 実況人格の乖離）を防ぎ、補助 LLM の実況は「つなぎ」に限定してコストと秘匿リスクを最小化する。

**却下案:**

- **(A) 配信を gateway platform adapter として実装** — gateway の auto-TTS / send 経路は inbound メッセージ起点のセッションでしか発火せず、cron 駆動の自律ループでは通らない。「配信への独白」と「チャット返信」は責務が異なり、抽象が合わない。（Opus / Codex 一致で却下）
- **(C) 全イベントを独立実況 LLM レイヤーで常時変換（プロトタイプ式）** — 実装量・LLM コスト・秘匿フィルタ面が過大で、人格が二重化する。無音対策は (B) の hook + レート制御で足りる。

**実装上の絶対ルール:** hermes の hook は**エージェントのターンスレッド上で同期実行**される。hook 内では構造化イベントを in-memory キューに積んで**即 return** し、LLM 変換・HTTP 送出は plugin 内の背景スレッドで行うこと（違反すると kai の作業が止まる）。

### ADR-2: 自律ループは単一 "kai-tick" cron ジョブ、状態は GitHub から毎回導出（F-1〜F-5）

**決定:**

- **単一のリカーリング cron ジョブ `kai-tick`**（例: 15〜30 分間隔）が自律開発ループを駆動する。cron の in-flight 重複防止により、前の tick が実行中なら次はスキップされ、**単一エージェントの直列実行が機構的に保証**される。
- 各 tick は**新規セッション**で `kai/workloop` skill を実行する: 「GitHub から最優先の作業を 1 件選び、完遂せよ。作業がなければ bounded なアイドル活動を 1 ビート行え」。
- **フェーズ復旧は GitHub をステートマシンとして使う**: tick は毎回 `gh` で現状を導出する（open PR に自分のレビューがなければ → レビューから / approve 済み＆CI green なら → マージから / 未着手 Issue → 実装から）。tick 途中でクラッシュしても、次の tick が GitHub の状態から正しいフェーズを再開する。プロセス内に作業状態を持たない。
- **レビュー役の隔離**は同一セッション内から `delegate_task`（隔離コンテキストの子エージェント。親履歴なし・summary のみ返る）で行う。
- cron のタイムアウトは**非活動タイムアウト**（デフォルト 600 秒、`HERMES_CRON_TIMEOUT` で延長・無効化可能）であり、活動が続く限りリセットされる。長時間の TDD 作業を阻害しない（※AGENTS.md の「3 分ハード割り込み」記述は実コードと不一致であることを確認済み）。

**根拠:** Footprint Ladder ②（cron + skill）で新規常駐コードゼロ。cron はジョブを `jobs.json` に永続化し再起動から自動復帰。作業単位＝新セッションはキャッシュ境界・トレース相関（session_id ≒ 作業スレッド）として理想的。GitHub 導出方式は要件の「GitHub 唯一のソース」と一致し、Codex が指摘した「フェーズ復旧の弱さ」を解消する。

**却下案:**

- **(b) 長寿命常駐セッション** — コンテキストが際限なく成長し、複数 Issue が 1 会話に混在してレビュー隔離とトレース相関を濁す。
- **(c) Kanban dispatcher** — マルチ worker 向け機構（worker を別プロセスで spawn）で単一エージェントには過剰。**昇格先として保持**: 優先度レーン・failure 自動ブロック・linked task（レビューを別 profile で read-only 強制）が要件化したら移行する。
- **(d) 外部 driver デーモン** — cron の再発明。hermes 統合（session DB・hook・トレース）を薄める。Codex 推奨だったが、その利点（フェーズ復旧・排他）は GitHub 導出 + in-flight dedup で吸収できる。

### ADR-3: 発話・字幕・同期の正典は speechd（独立プロセス）が持つ（F-8〜F-10）

**決定:** 発話の生成者（producer）と配信への到達（sync/再生）を分離する。producer は複数（kai の応答 / narrator の実況 / 後日 youtube_live のチャット返信）だが、すべて **speechd の単一 FIFO キュー**に `POST /say` で集約する。同期メカニズムは §4。

**根拠:** cron セッションは gateway プロセス内、将来 Kanban 昇格時の worker は別プロセス——プロセストポロジに依存しない IPC（localhost HTTP）で統一する。プロトタイプの Beat モデルは採用せず、「speechd のビート単位処理」として F-10 を満たす。

---

## 3. コンポーネント一覧

| # | 名前 | 種別 | 責務 | 配置 | 時期 |
| --- | --- | --- | --- | --- | --- |
| 1 | kai_trace | plugin | 全事象ロギング（F-22）。hook で捕捉 → 秘匿マスク → JSONL 追記 + SQLite(FTS5) インデックス | `plugins/kai_trace/` | M1 |
| 2 | kai_narrator | plugin | 実況（F-7）。hook 観測 → キュー → 背景スレッドで auxiliary `narration`（ローカル LLM）変換 → 秘匿マスク → speechd へ POST。kai_trace とイベント捕捉基盤を共有 | `plugins/kai_narrator/` | M3 |
| 3 | speechd | 独立プロセス | 発話・字幕キュー（F-8〜F-10）。TTS 呼出・null-sink 再生・OBS 字幕更新・縮退・アバター通知。同期の正典 | `kai-services/speechd/` | M2 |
| 4 | mac-aquestalk | 独立プロセス（Mac） | AquesTalk10 合成 HTTP サービス（`POST /synthesize` → WAV）。Tailscale bind、launchd 常駐 | `kai-services/mac-aquestalk/` | M2 |
| 5 | streaming | 独立プロセス + skill | X(dummy) + OBS + RTMP のセットアップ・制御（F-6）。obs-websocket 制御は skill から | `kai-services/streaming/` + `skills/kai/broadcast/` | M0/M4 |
| 6 | avatar | 独立プロセス | PuruPuruPNGTuber オーバーレイ（F-11/11b）。口パクは null-sink monitor の仮想マイク駆動 | `kai-services/avatar/` | M2ストレッチ〜ポストMVP |
| 7 | kai/workloop | skill + cron ジョブ | 自律ループの 1 tick（F-1〜F-4, F-28/29）。GitHub から作業導出 → 完遂 or idle-play | `skills/kai/workloop/` | フェーズ1（MVP は手動指示） |
| 8 | kai/review | skill | 隔離レビューの手順書（F-5）。delegate_task の子が使用。read-only 原則 | `skills/kai/review/` | フェーズ1 |
| 9 | kai/retro | skill + cron ジョブ | 振り返り（F-30）。トレース分析 → 日報・改善提案 | `skills/kai/retro/` | ポストMVP |
| 10 | kai persona | skin + コンテキスト | 「多脚思考 AI」人格・ブランディング。narrator の auxiliary プロンプトにも同一ペルソナを注入 | skin YAML | M1 |
| 11 | kai config | config.yaml / .env | provider（codex/claude/local）、`auxiliary.narration`、cron、speechd エンドポイント等。機密のみ .env | `config.yaml` | M1 |
| 12 | youtube_live | plugin (platform) | ライブチャット入出力（F-14〜F-16）。返信も speechd へ | `plugins/platforms/youtube_live/` | フェーズ4 |

**命名規約:** hermes plugin は `plugins/kai_<name>/`（アンダースコア）、skill は `skills/kai/<name>/`、独立プロセスは `kai-services/<name>/`。すべて**新規ディレクトリの追加のみ**で、upstream ファイルと衝突しない。

---

## 4. 発話・字幕同期メカニズム（F-10 の正典）

speechd は FIFO 単一コンシューマで**1 ビートずつ直列処理**する（発話の重なりなし）。

**Enqueue API:**

```jsonc
POST http://127.0.0.1:8200/say
{
  "beat_id": "uuid",              // speechd が未指定なら採番
  "session_id": "cron_kai-tick_20260703T120000",
  "work_thread_id": "kai-agent#123",   // Issue/PR 由来。なければ null
  "source": "agent_response" | "narrator" | "chat_reply",
  "priority": "normal" | "low",   // low は滞留時に drop 可
  "display_text": "テストの失敗箇所を確認してるよ。",  // 字幕用（技術トークンは英語のまま）
  "tts_text": "てすとの しっぱいかしょを かくにんしてるよ。",  // 読み上げ用（省略時 display_text）
  "emotion": "focused"            // 7 種。avatar へ中継
}
→ 202 {"queued": true, "beat_id": "...", "queue_depth": 2}
```

**ビート処理手順:**

1. **秘匿マスク**（producer 側でも実施済み。二層防御）
2. mac-aquestalk へ合成要求（タイムアウト 3 秒）→ WAV + duration 取得
3. **同一時点で同時発行:** (a) obs-websocket で字幕テキストソースを `display_text` に更新、(b) WAV を null-sink へ再生開始、(c) avatar へ `{beat_id, emotion}` を通知
4. **字幕クリアは再生プロセスの終了を一次トリガー**とする。WAV duration は watchdog（残留防止の上限）と口パク用メタデータに使う
5. **縮退（TTS 不達）:** 音声をスキップし、字幕のみを文字数ベースの表示時間 `max(2.0, min(8.0, len(display_text)/8.5))` 秒表示してクリア。**配信は止めない**
6. 各ビートの `speech_started / speech_finished / speech_failed / subtitle_cleared` を相関 ID つきでトレースシンクへ記録（F-22 と F-10 の検証が同じ ID で追える）

**キュー制御:** 重複抑制（直近 N 件と同文なら drop）、最短発話間隔、滞留時は `priority: low`（narrator のつなぎ実況）から drop。字幕は常に 1 件のみ・履歴なし（F-9）。

---

## 5. 共通規約

### 5.1 トレースイベントの共通エンベロープ（JSONL）

全プロセスは 1 行 1 イベントで追記する。ファイルは `~/.kai/trace/YYYY-MM-DD.jsonl`（ローテーション・N 日で圧縮）。

```jsonc
{
  "v": 1,
  "ts": "2026-07-03T12:00:00.123Z",
  "session_id": "...",          // hermes セッション ID（プロセス系イベントは null 可）
  "work_thread_id": "...",      // "owner/repo#123" 形式。作業に紐付かないイベントは null
  "component": "kai_trace | kai_narrator | speechd | streaming | avatar | workloop | ...",
  "kind": "tool_call | llm_call | speech_started | ... ",  // component ごとに定義
  "payload": { }                // kind 固有。スキーマは各コンポーネント設計書 §4 で定義
}
```

- **相関 ID:** `session_id`（hermes セッション）/ `work_thread_id`（Issue/PR）/ `beat_id`（発話）。この 3 つで全事象を横断検索する。
- **SQLite インデックス:** kai_trace が JSONL を取り込み `~/.kai/trace/index.db`（WAL + FTS5）を維持。振り返り skill はこれに問い合わせる。
- **正本は JSONL**。SQLite は再構築可能な派生物とする（要件 §9.2-5）。

### 5.2 ポート割当（すべて localhost / Tailscale 内。公開ポートなし）

| ポート | サービス | bind |
| --- | --- | --- |
| 8200 | speechd `/say` ほか | 127.0.0.1 |
| 8300 | avatar 制御（表情 WS 等） | 127.0.0.1 |
| 8100 | mac-aquestalk `/synthesize` | Mac の Tailscale IP |
| 4455 | obs-websocket | 127.0.0.1 |
| （外部） | ローカル LLM `/v1` | Windows の Tailscale IP |

### 5.3 秘匿情報

- マスクは**書き込み前・送出前**に各層で実施（producer と speechd の二層）。対象: `sk-` / `ghp_` / `gho_` 形式トークン、`.env` の値、Webhook URL、完全なエンドポイント URL。
- 配信画面に `.env`・認証情報を表示しない運転ルール（エディタ・ターミナルで開かない）。
- `.env` は機密のみ。振る舞い設定はすべて `config.yaml` / 各サービスの設定ファイル。

### 5.4 プロセス管理

- すべて **systemd**（hermes gateway / speechd / streaming / avatar）。`Restart=on-failure`。Mac 側は launchd。
- 起動依存: streaming（X → OBS）→ speechd → hermes。ただし各サービスは依存先不達でも縮退起動する（配信スタックなしでも kai は作業できる）。

---

## 6. システム制約（全コンポーネント設計に適用）

1. **コアファイル改変禁止**（`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, `toolsets.py`, `model_tools.py`）。kai コードは plugins / skills / skin / kai-services / config のみ。
2. **プロンプトキャッシュ不可侵**: hook は観測専用、注入は user メッセージ内限定、system prompt はバイト安定。
3. **hook 内で同期 LLM / HTTP 呼び出し禁止**（ADR-1 の実装ルール）。enqueue + 背景スレッド。
4. **optional 機能の失敗はループ・配信を止めない**（warn + 縮退）。
5. **GitHub が唯一の作業状態**。ローカルに作業キューの複製・ミラーを持たない。
6. upstream 追従: `upstream/main` → `main`（ff）→ `kai/main` へ merge。kai ファイルは新規ディレクトリのみなのでコンフリクトは原則発生しない。

---

## 7. リスクと未決事項

| # | 事項 | 対応方針 |
| --- | --- | --- |
| 1 | `delegate_task` は per-call の read-only ツール制限・モデル指定が不可 → レビュー役の権限を §6.3（要件）の水準まで絞れない | MVP〜フェーズ1 は delegation の隔離コンテキストで可とする。厳格化が必要になったら reviewer profile / Kanban linked task へ昇格（ADR-2 の昇格パス） |
| 2 | `post_llm_call` hook から最終応答テキストを取得できるかは実測確認が必要 | kai_narrator 実装時に hook kwargs を検証。取れない場合は代替 hook（`transform_llm_output` 系）で tee する |
| 3 | cron セッションは既定 `skip_memory=True` → 作業間の学習（F-25）が自動では効かない | workloop skill 内で明示的に memory を読む / `context_from` チェーン / curator 経由を実装時に選定 |
| 4 | self-update（F-27）と実行中 tick・配信の競合 | self-update ジョブは kai-tick の in-flight を確認してドレイン後に再起動。相互排他は cron の実行状態で判定 |
| 5 | OBS arm64 / cua-driver の ARM 動作 | M0 PoC で検証（mvp-plan §4） |
| 6 | narrator の実況品質（頻度・自然さ） | レート制限・重複抑制のパラメータは配信リハーサル（M4）で調整。トレースに narration イベントが残るため振り返りで改善可能 |
