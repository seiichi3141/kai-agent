# speechd

kai の発話・字幕キュー。VM（Ubuntu）上で常駐する独立プロセス。

producer（kai の応答 / narrator の実況 / 後日 youtube_live のチャット返信）から
`POST /say` でテキストを受け取り、単一 FIFO キューに直列で積む。ワーカースレッドが
1件ずつ取り出し、Mac の TTS サーバー（AquesTalk10、`kai-services/aquestalk-server/`）
へ合成要求 → 得られた WAV を VM のスピーカー sink（PipeWire null-sink
`kai_speaker`）へ `paplay` で同期再生しつつ、`GET /events`（SSE）で購読中の
Web オーバーレイ（`kai-services/overlay/`）へ字幕の表示・クリアを push する。

字幕は当初 OBS が読むファイル方式だったが、将来アバター・コメント・進捗も
同じオーバーレイで表現できるよう SSE 配信に変更した（OBS ソースを増やさない
方針）。音声・キュー・縮退・マスク・トレースのロジックは変更していない。

設計の正典: `docs/kai/design/00-system.md` §3(ADR-3) / §4「発話・字幕同期メカニズム」/
§5.1（共通トレースエンベロープ）/ §5.3（秘匿）。本 README は運用手順のみを記す。

## 依存

- Python 3（VM は 3.12 を想定）。**標準ライブラリのみ**（`http.server` /
  `http.client` / `urllib` 等）。追加パッケージのインストールは不要。
- `paplay`（`pulseaudio-utils`）。未インストールでも起動はするが、すべての発話が
  縮退（音声なし・字幕のみ）になる。

## 起動方法（手動）

```bash
cd kai-services/speechd
python3 speechd.py
```

systemd --user サービスとして常駐させる場合は後述の `install.sh` を使う。

## 環境変数

| 変数           | デフォルト                    | 意味                                                   |
| -------------- | ----------------------------- | ------------------------------------------------------ |
| `SPEECHD_PORT` | `8900`                        | HTTP サーバーの listen ポート（`/say` `/events` 共通） |
| `SPEECHD_BIND` | `127.0.0.1`                   | HTTP サーバーの bind アドレス                          |
| `TTS_URL`      | `http://100.106.136.117:8890` | Mac TTS サーバー（`aquestalk-server`）のベース URL     |
| `AUDIO_SINK`   | `kai_speaker`                 | `paplay --device` に渡す PipeWire sink 名              |
| `HERMES_HOME`  | （未設定なら `~/.hermes`）    | トレース JSONL の出力先ルート（後述）                  |

`HERMES_HOME` は hermes 本体（`hermes_constants.get_hermes_home()`）と同じ規約。
speechd はこのモジュールを import できればそれを使い、できなければ（別 venv・別
cwd で動くため import に失敗するのが通常）`HERMES_HOME` 環境変数を自前で読む
フォールバックにフォールバックする。hermes gateway が非デフォルトプロファイルで
動いている場合は、`speechd.service` にも同じ `HERMES_HOME` を明示すること
（さもないと speechd のトレースだけデフォルトプロファイルに書かれてしまう）。

## API

### `GET /health`

```text
200 {"ok": true}
```

### `POST /say`

```jsonc
{
  "text": "こんにちは、テストです", // 必須
  "voice": "F1", // 省略時 "F1"
  "speed": 120, // 省略時 120
  "source": "agent_response", // "agent_response" | "narrator" | "chat_reply" 等
  "priority": "normal", // "normal" | "low"（low はキュー滞留時に drop 可）
  "work_thread_id": "kai-agent#123", // 省略可（トレース相関用）
  "session_id": "cron_kai-tick_...", // 省略可（トレース相関用）
  "emotion": "happy", // 省略可。指定すれば SSE の subtitle イベントに乗る
}
```

即座に `202` を返す（合成・再生は非同期でワーカースレッドが行う）。

```jsonc
{"queued": true, "queue_depth": 2}
// または drop 時:
{"queued": false, "queue_depth": 1, "reason": "duplicate" | "dropped_low_priority_queue_full"}
```

### `GET /events`（SSE）

字幕（および将来のアバター・コメント等）を購読するための
Server-Sent Events エンドポイント。`kai-services/overlay/` の Web オーバーレイが
これを購読する。複数クライアントが同時購読でき、それぞれ独立にイベントを
受け取る。

```text
GET /events
Content-Type: text/event-stream
```

イベントは 1 件が `data: <json>\n\n` の形式。字幕イベントの形式:

```jsonc
// 表示
{"type": "subtitle", "text": "表示する文", "source": "agent_response", "emotion": "happy"}
// クリア（空文字）
{"type": "subtitle", "text": ""}
```

`source` / `emotion` は指定があった場合のみ付与される。`type` が
`"subtitle"` 以外のイベントは将来の拡張用（今は未使用）。

- **late-join**: 新規購読者が接続した瞬間、現在表示中の字幕状態
  （`_current_subtitle`）を即座に1件 push する。接続直後に何も表示されていない
  ("text": "") 状態でも1件は必ず届く。
- **keep-alive**: 15 秒間隔（`SSE_KEEPALIVE_INTERVAL`）で `: keep-alive\n\n`
  というコメント行を送出し、プロキシ等でのタイムアウト切断を防ぐ。
  `EventSource` はコメント行を無視するので、クライアント側の実装に影響しない。
- **切断検知**: クライアントへの書き込みが失敗した時点（`BrokenPipeError` /
  `ConnectionResetError` 等）で購読リストから除去する。SSE 配信の失敗は
  発話処理（TTS 呼び出し・paplay 再生・トレース）を止めない（best-effort）。

## 動作仕様（設計 §4 の実装）

- **FIFO 単一コンシューマ**: ワーカースレッド1本がキューから1件ずつ取り出し、
  直列処理する（発話の重なりなし）。
- **キュー制御**: 直近1件と同一 `text` なら drop（重複抑制）。`priority: "low"`
  の項目はキュー滞留数が5件を超えていたら drop（`LOW_PRIORITY_QUEUE_THRESHOLD`、
  `speechd.py` 内定数）。
- **1件の処理**:
  1. 秘匿マスク（`sk-...` / `ghp_...` / `github_pat_...` / `xox*-...` /
     `Bearer ...` トークン形式、および `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`PAT`/
     `CREDENTIAL` を含む環境変数名の値）を `text` に適用してから TTS へ送出する
     （producer 側マスクとの二層防御。方針は `plugins/kai_trace` の `_mask` と
     同一だが、実装はこのディレクトリ内で完結させている）。
  2. Mac TTS の `POST /synthesize` へ NDJSON ストリーミングで問い合わせる
     （接続タイムアウト3秒・全体タイムアウト30秒）。
  3. 各文（NDJSON の1行）ごとに:
     - `wav_base64` があれば: `/events` 購読者へ字幕表示イベントを push する →
       base64 をデコードして一時 WAV に書く → `paplay --device=$AUDIO_SINK`
       で**同期再生**（再生完了までブロック）→ **再生プロセスの終了を
       一次トリガーとして**字幕クリアイベントを push する。一時 WAV は
       使用後に削除。
     - `error`（その文の合成失敗）、または TTS 全体が不達/タイムアウトの場合:
       **縮退** = 字幕表示イベントを push する（文または全体テキスト）→
       `max(2.0, min(8.0, len(text)/8.5))` 秒スリープ → 字幕クリアイベントを
       push する（音声なしで字幕だけ出す。配信は止めない）。
  4. `speech_started` / `speech_finished` / `speech_failed` / `subtitle_cleared`
     を `beat_id`（発話単位で採番する UUID）・`session_id` / `work_thread_id`
     つきでトレースへ記録する（best-effort。失敗してもメイン処理は止めない）。
     `subtitle_cleared` はファイル方式だった頃と同じ kind 名のまま、
     イベントの送出手段だけを SSE に変更している。
- **トレース出力先**: `<HERMES_HOME>/kai_trace/YYYY-MM-DD.jsonl` に
  `plugins/kai_trace` と同じ共通エンベロープ
  `{v, ts, session_id, work_thread_id, component:"speechd", kind, payload}` で
  追記する。JSONL への書き込みはバックグラウンドスレッドで行い、キュー飽和時は
  黙って drop する（配信・発話処理を止めない）。

## 手動検証（curl）

TTS サーバー・paplay が実機に揃っている前提（VM 上での実行を想定）。

```bash
# 起動確認
curl -s http://127.0.0.1:8900/health
# => {"ok": true}

# SSE を購読しておく（別ターミナル。字幕イベントがリアルタイムに流れる）
curl -sN http://127.0.0.1:8900/events
# => 接続直後にまず現在の字幕状態が1件届く（例: {"type":"subtitle","text":""}）

# 通常発話（上の /events 購読側に表示イベント→クリアイベントが流れ、
# kai_speaker から音声が鳴ることを確認）
curl -s -X POST http://127.0.0.1:8900/say \
  -H 'Content-Type: application/json' \
  -d '{"text":"こんにちは、テストです","source":"agent_response"}'
# => {"queued": true, "queue_depth": 1}

# 重複抑制の確認（直前と同文は queued:false, reason:"duplicate"）
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"こんにちは、テストです"}'

# 秘匿マスクの確認（/events・トレースに «redacted» が入り、生の値が出ないこと）
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"key is sk-xxxxxxxxxxxxxxxxxxxxxxxx"}'

# TTS を意図的に落として縮退を確認（TTS_URL を無効な値にして再起動 → 字幕表示イベント→クリアが流れる）
TTS_URL=http://127.0.0.1:1 python3 speechd.py &
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"TTSが落ちていても字幕だけ出るはず"}'

# late-join の確認: 発話中（字幕表示中）に新しく /events へ接続すると、
# 接続直後に「現在表示中の字幕」がそのまま1件届くことを確認する

# トレースの確認
tail -f ~/.hermes/kai_trace/$(date +%F).jsonl
```

## systemd 常駐化（`install.sh`）

`kai-services/streaming/setup.sh`（`kai-x11vnc.service` の `@TAILSCALE_IP@`
置換と同じ作法）に倣い、`speechd.service` テンプレートの `@REPO_DIR@` を
実際のリポジトリパスに置換して `~/.config/systemd/user/` に配置する。

```bash
cd kai-services/speechd
bash install.sh
```

内部で行っていること:

1. `python3 -m py_compile speechd.py` で構文確認、`paplay` の有無を警告表示
2. `sed` で `speechd.service` の `@REPO_DIR@` を実パスに置換して
   `~/.config/systemd/user/speechd.service` に配置
3. `systemctl --user daemon-reload && systemctl --user enable --now speechd.service`
4. `sudo loginctl enable-linger "$USER"`（ログアウト後も user unit を維持）

再登録・設定変更後の反映も同じスクリプトを再実行すれば冪等に反映される。

### 運用コマンド

```bash
systemctl --user status speechd.service
journalctl --user -u speechd.service -f
systemctl --user restart speechd.service
systemctl --user stop speechd.service
```

## 制約・既知の割り切り

- コアファイル（hermes 本体）には触れていない。`kai-services/speechd/` 配下のみ。
- optional 機能（TTS 到達性・SSE 配信・トレース）の失敗は全体を止めない
  （warn ログ + 縮退/継続）。SSE クライアントが1つも接続していなくても
  発話処理（TTS 合成・paplay 再生）自体は問題なく動く。
- ストリーミング中に一部の文を再生した後で接続が切れた場合、それ以降の文は
  諦めてログのみ残す（部分的な縮退は行わない）。次の `POST /say` は独立して
  正常に処理される。
- `/events` の購読者ごとのバッファは `SSE_CLIENT_QUEUE_MAXSIZE`（既定50）件。
  クライアントの読み出しが追いつかずバッファが溢れた場合、新しいイベントを
  ブロックせず drop する（配信処理を止めないことを優先し、字幕表示の
  取りこぼしを許容する。通常運用でバッファが溢れることは想定していない）。
