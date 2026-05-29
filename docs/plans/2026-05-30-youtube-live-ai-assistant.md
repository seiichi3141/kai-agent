# YouTube ライブ配信用 AI アシスタント計画

状態: planning
担当: TBD
作成日: 2026-05-30

## 目的

YouTube のゲーム実況ライブ配信で使う Hermes ベースの AI アシスタントを作る。

目標は、配信者と音声で会話できる共同司会者のような体験である。配信者の発話を
聞き取り、YouTube ライブチャットを読み、必要に応じて TTS で話し、OBS オーバー
レイに字幕・攻略メモ・ガイド情報を表示できるようにする。

重要な前提として、アシスタントの音声は配信にも乗せる。配信者とアシスタントの
掛け合い自体をコンテンツにする。

## 目標機能

1. STT/TTS による配信者との音声コミュニケーション。
2. YouTube ライブチャットの取得と、必要なチャットへの返信。
3. OBS オーバーレイへの、アシスタント発話または配信者発話の字幕表示。
4. ゲーム実況向けの、安全で面白い雑談・実況・攻略補助の人格。
5. 攻略メモ、ルート、アイテムチェックリスト、ボス攻略などのオンデマンド表示。
6. 配信者向けの私的フィードバック、YouTube チャット返信、TTS 音声、OBS 表示を
   明確に振り分ける仕組み。
7. 選別した YouTube チャットを OBS に表示し、それに対してアシスタントが反応する。
8. obs-websocket を使った OBS 操作。将来的にはシーン切り替えも行う。

## 現状の Hermes で使えるもの

Hermes には、今回の用途に使える部品が既にある。

- STT は `tools/transcription_tools.py` に実装されている。
- TTS は `tools/tts_tool.py` に実装され、`text_to_speech` tool として公開されている。
- CLI/TUI の音声モードは `tools/voice_mode.py`、`hermes_cli/voice.py`、
  `tui_gateway/server.py` に実装されている。
- Gateway platform adapter は、各プラットフォームからの受信メッセージを
  `gateway.platforms.base.MessageEvent` に正規化する。
- `PluginContext.register_platform()` を使えば、core を直接編集せずに platform plugin
  を追加できる。
- `send_message` は、プラットフォーム横断の送信パターンとして使える。
- 人格や振る舞いは、skill、personality、channel prompt で表現できる。
- Web search、browser、memory、skills、file tools は、ゲーム攻略調査や再利用可能な
  ガイドメモに使える。

不足しているもの:

- YouTube Live 専用の platform adapter はまだない。
- OBS オーバーレイまたは obs-websocket 用 tool はまだない。
- streaming STT 抽象はまだない。既存の Hermes STT は主にファイル単位で、
  音声を録音してファイル保存し、`transcribe_audio(path)` を呼ぶ形である。
- 返答を「喋る」「画面に出す」「YouTube チャットに投稿する」「内部だけに留める」
  などへ振り分ける、配信用 response router はまだない。
- ゲーム実況向け人格・skill はまだない。

## 推奨アーキテクチャ

### 0. 既存 Hermes STT の入力フロー

現行の Hermes には、TUI/CLI 用の音声入力経路がある。ただしこれはライブ配信用の
streaming STT ではなく、push-to-talk と VAD による録音完了後に 1 回の transcript を
作る方式である。

```text
配信者が /voice on
  -> Ctrl+B で録音開始
  -> マイク音声をローカルで WAV 録音
  -> 無音検知、または Ctrl+B で録音停止
  -> transcribe_audio(wav_path)
  -> voice.transcript event
  -> TUI が transcript を submitRef.current(text) に渡す
  -> prompt.submit
  -> Hermes agent の通常の user message として処理
```

関連ファイル:

- `ui-tui/src/app/useInputHandlers.ts`: `voice.record` JSON-RPC を送る。
- `tui_gateway/server.py`: `voice.record` を受け、`start_continuous()` /
  `stop_continuous()` を呼ぶ。
- `hermes_cli/voice.py`: 録音 lifecycle と callback を管理する。
- `tools/voice_mode.py`: WAV 録音と `transcribe_recording()`。
- `tools/transcription_tools.py`: `stt.provider` に従って STT provider を選ぶ。
- `ui-tui/src/app/createGatewayEventHandler.ts`: `voice.transcript` を受け取り、
  transcript を次の prompt として送信する。

初期設定では `stt.provider: local` で、`faster-whisper` の `base` model を使う。
これは安価に試せるが、partial transcript は出ない。ライブ配信の字幕や会話遅延を
詰めるには、次節の Deepgram streaming STT を別経路として追加する。

### 1. Deepgram Streaming STT

ライブ配信用の音声入力には Deepgram streaming STT を使う。

既存の Hermes STT provider interface はファイル指向で、gateway の音声メモや
push-to-talk には合っている。

```text
record audio -> write wav/ogg -> transcribe_audio(path) -> transcript
```

ライブ字幕や低遅延の音声会話では、イベント指向の形が望ましい。

```text
microphone chunks -> Deepgram WebSocket -> partial/final transcript events
```

責務:

- マイク音声を継続的、または push-to-talk で取得する。
- 音声 chunk を Deepgram WebSocket に送る。
- partial transcript を public caption として OBS 字幕へ流す。
- final transcript を確定字幕として表示し、stream response router / Hermes prompt path へ渡す。
- 発話終端、無音処理、再接続に対応する。
- 現行の `transcribe_audio(path)` は fallback として残す。

Deepgram の推奨初期設定:

- `model: nova-3`: 汎用のライブ配信用。
- `language: ja`: 日本語中心の配信向け。
- `interim_results: true`: ライブ字幕用の partial transcript を受け取る。
- `smart_format: true`: 読みやすい transcript にする。
- `endpointing: 800`: 録音 fixture の比較では、300/500 より誤分割と誤認識が少なかった。
- 会話の turn detection や interruption を重視する場合は、後で `flux` も検討する。

設定例:

```yaml
streaming_stt:
  enabled: true
  provider: deepgram
  always_on: true
  submit:
    debounce_ms: 1800
    llm_wait_debounce_ms: 3000
    min_chars: 8
    joiner: " "
    max_wait_ms: 6000
    turn_detection: hybrid
    require_speech_final: true
    classifier:
      enabled: false
      base_url: http://100.94.173.74:8001/v1
      model: gemma-4-e4b
      timeout_ms: 800
  deepgram:
    model: nova-3
    language: ja
    sample_rate: 16000
    channels: 1
    interim_results: true
    smart_format: true
    endpointing: 800
    vad_events: true
    chunk_ms: 100
```

API key:

- `DEEPGRAM_API_KEY` を `~/.hermes/.env` に設定する。

初期実装:

- `hermes_cli/streaming_stt.py`
  - Deepgram WebSocket URL の構築。
  - `sounddevice.RawInputStream` による 16-bit PCM マイク入力。
  - Deepgram `Results` payload から partial / final transcript event への正規化。
  - background thread + asyncio loop で TUI gateway から開始・停止できる session。
- `tui_gateway/server.py`
  - `streaming_stt.enabled: true` かつ `provider: deepgram` の場合、
    既存の `voice.record` を Deepgram streaming に切り替える。
  - partial transcript は `voice.partial_transcript` event として TUI に出す。
  - Deepgram final transcript はすぐには agent に渡さず、短時間バッファする。
  - バッファ後のまとまった発話だけを既存の `voice.transcript` event として出す。
    そのため既存の TUI 経路により、まとまった発話が通常の prompt として
    Hermes agent に渡る。
- `ui-tui/src/app/createGatewayEventHandler.ts`
  - `voice.partial_transcript` を system line として表示する。

### 1.1 会話ターン終端判定

常時STTでは、Deepgram の `final transcript` と「人間が話し終わった」は同じではない。
実際の会話では、相手は次の複数の手がかりを組み合わせて、割り込むべきか待つべきかを
判断している。

- 無音の長さ。短いポーズは文中の間であり、必ずしも発話終了ではない。
- 文として完結しているか。「私の話していることが」「こっちが」のような断片では待つ。
- 語尾・接続表現。「けど」「なので」「それで」「まず」などは続きやすい。
- 音声の調子。語尾が上がる、言い淀む、伸ばす、考え込む場合は続きやすい。
- 会話の役割。配信者が実況中なら、独り言・読み上げ・ゲーム内反応を全部拾わない。
- 直前の指示。「話し終わるまで反応しないでほしい」は turn-taking の方針として保持する。

したがって、配信用アシスタントでは `final transcript -> 即 agent submit` ではなく、
`TurnController` のような層を置く。

```text
Deepgram partial/final
  -> transcript buffer
  -> turn boundary detector
  -> public caption update
  -> agent submit / wait / ignore
```

初期版の判定:

- `debounce_ms`: 最後の final から一定時間待つ。
- `min_chars`: 短すぎる断片は agent に送らない。
- `max_wait_ms`: いつまでも保留し続けないための上限にする。
- この段階では、語句、語尾、句読点による固定マッチは入れない。

中期版では、発話バッファを小さな LLM または音声イベント+LLM の hybrid に渡し、
`submit / wait / ignore / backchannel` を返させる。

```json
{
  "action": "wait",
  "reason": "文が未完了で、接続助詞で終わっている",
  "buffer": "私の話していることが 何か 今の ボイスで"
}
```

`backchannel` は「うん」「聞いています」のような相づちで、配信に乗せるかどうかは
別設定にする。初期MVPでは、勝手な相づちは出さず、明確に話し終わったときだけ返答する。

方針として、固定文字列マッチによる判定は最初から入れない。日本語の言い回し、STT 誤認識、
ゲーム実況中の独り言に対して脆く、安全網としても信頼しづらいためである。本命は次の信号を
組み合わせる。

- Deepgram の `speech_final`、VAD event、無音時間、final snippet の間隔。
- 発話バッファ全体を入力にした小型 LLM / main model とは別の turn classifier。
- 直前の会話状態。アシスタントへの質問中か、実況中の独り言か、待機指示中か。
- 投機的生成の cancel/reveal 状態。

turn classifier は JSON で `submit / wait / ignore / backchannel` と理由を返す。
固定語句の列挙は、実運用で必要だと判断した場合だけ、明示的な追加機能として検討する。

現時点の実装方針:

- `turn_detection: hybrid` を標準候補にする。
- Deepgram の `speech_final`、final/partial event の時刻、`debounce_ms` を baseline とする。
- 未送信 buffer がある状態で partial transcript が来たら、発話継続 activity として
  debounce timer を延長する。
- OpenAI-compatible なローカル LLM classifier を任意で使う。
  初期接続先は `http://100.94.173.74:8001/v1`、model は `gemma-4-e4b`。
- classifier が timeout / failure の場合は baseline に fallback する。
- classifier が一度 `wait` を返した buffer は、`llm_wait_debounce_ms` まで debounce を延長する。
  通常の `debounce_ms` を全体的に伸ばすと、独立した短い発話まで結合されるためである。
- `max_wait_ms` 到達時は classifier の `wait` で上書きせず baseline submit する。
  無限待機を防ぐためである。
- `backchannel` は現時点では `wait` 扱いにし、TTS/OBS routing が整ってから別出力にする。

### 1.2 投機的な返答生成

低遅延の会話では、発話終端を完全に確定してから初めて LLM を呼ぶと、返答開始が遅くなる。
人間同士の会話でも、相手は話の途中から返答候補を内側で準備し、相手が続けたらその候補を
捨てて聞き直している。配信用アシスタントでも同じ発想を使う。

```text
Deepgram final snippets
  -> transcript buffer
  -> probable endpoint
  -> hidden speculative generation
  -> input continues: cancel/discard
  -> stable endpoint: commit/reveal/TTS
```

重要な制約:

- 投機中の返答は、確定するまで TUI、OBS、TTS、YouTube chat に出さない。
- 投機中は副作用のある tool call を許可しない。OBS 操作、ファイル編集、投稿、外部API更新は
  確定後の通常ターンだけで実行する。
- 新しい transcript が来たら、既存の投機生成を cancel し、バッファに追記して待つ。
- 確定前に TTS を開始しない。キャンセルされた返答が配信に乗るのを防ぐ。
- 確定前に会話履歴へ保存しない。捨てた draft が以後の文脈を汚さないようにする。

初期実装案:

- `TurnController` が `probable_submit` を出した時点で、hidden/no-tools の draft 生成を開始する。
- `commit_after_ms` の間に新しい final transcript が来なければ、その draft を確定返答として表示し、
  TTS と OBS 字幕へ渡す。
- 新しい final transcript が来た場合は `session.interrupt` 相当で生成を止め、draft を破棄する。
- hidden draft path が安定するまでは、確定時に通常の `prompt.submit` を再実行する fallback を残す。
- Scenario 9 の録音評価では、確定直後に「まだ答えないで」が続くケースがあった。
  replay 上は `commit_delay_ms: 1000` で cancel/rebuffer でき、`3000` では通常の独立発話まで
  結合した。初期 runtime 実装は 1000ms 程度の短い pending submit window から始める。

設定案:

```yaml
streaming_stt:
  speculative:
    enabled: false
    start_after_ms: 900
    commit_delay_ms: 1000
    commit_after_ms: 500
    cancel_on_new_transcript: true
    allow_tools: false
    reveal_draft: false
```

`reveal_draft: false` を初期値にする。開発中に挙動を見たい場合だけ、キャンセルされ得る draft を
TUI の debug line に出す。配信中は常に非表示にする。

現時点の制約:

- ローカル overlay server は初期実装済み。`live_overlay.enabled: true` で
  `http://127.0.0.1:8765/overlay` を OBS Browser Source として使える。
- Deepgram streaming は `always_on: false` では `/voice on` + `Ctrl+B` の
  push-to-talk 操作に接続する。
- `always_on: true` では `/voice on` で聞き取りを開始し、`/voice off` まで
  常時ストリーミングする。
- `voice.partial_transcript` はまずTUIに出すだけで、エージェントには渡さない。
  エージェントに渡すのは final transcript のみ。

参考:

- https://developers.deepgram.com/reference/speech-to-text/listen-streaming
- https://developers.deepgram.com/docs/live-streaming-audio
- https://deepgram.com/pricing

### 2. Fish Audio TTS

配信用アシスタントの主音声には Fish Audio を使う。

Hermes には `tools/tts_tool.py` に組み込み TTS provider があるが、Fish Audio は現時点で
組み込みではない。ただし既存の TTS 拡張 surface に自然に追加できる。

- `tts.providers.<name>: type: command`: 単純な shell/CLI 連携向け。
- `PluginContext.register_tts_provider()`: Python SDK/API 連携向け。

ライブ配信用では、command provider より Python plugin を優先する。Fish Audio は
streaming audio や voice/model metadata を扱えるため、単発の shell command よりも
コードで扱う方が拡張しやすい。

責務:

- Fish Audio TTS でアシスタント音声を生成する。
- 設定された voice / `reference_id` に対応する。
- `s1`、`speech-1.6`、またはより新しい Fish Audio model name を設定で使えるようにする。
- 汎用再生用に `mp3`、低遅延や voice-message delivery 用に必要なら `opus` に対応する。
- 再生経路が chunk を直接消費できる場合は streaming output を優先する。
- Hermes や platform が完全な音声ファイルを期待する場合は file output に fallback する。

設定例:

```yaml
tts:
  provider: fishaudio
  fishaudio:
    model: s1
    reference_id: ""
    format: mp3
    latency: balanced
    mp3_bitrate: 128
    chunk_length: 200
```

実装案:

- `plugins/tts/fishaudio/`
  - `plugin.yaml`
  - `__init__.py`
  - `agent.tts_provider.TTSProvider` を継承した provider class
  - `synthesize()` で完全な音声を `output_path` に書き込む。
  - 必要に応じて、音声 chunk を返す `stream()` を実装する。
- 環境変数:
  - `FISH_API_KEY`

参考:

- https://docs.fish.audio/docs/core-features/text-to-speech
- https://docs.fish.audio/api-reference/introduction
- https://api.fish.audio/openapi.json

### 3. YouTube Live Chat Ingestion

YouTube Live のチャット取得を追加する。初期段階ではチャットへの投稿は扱わない。
配信上で反応すべきチャットだけを選別し、OBS overlay にそのチャットを表示してから
アシスタントが音声で反応する。

最初は standalone bridge として作り、後で `plugins/platforms/youtube_live/` の
platform plugin に移す。

責務:

- Google OAuth で YouTube Live Streaming API に認証する。
- active broadcast と `liveChatId` を解決する。
- YouTube Live Chat API からチャットメッセージを受信する。
- チャットメッセージを `MessageEvent` に変換する。
- 反応対象のチャットを選別する。
- 選別したチャットを OBS overlay に表示する。
- 選別したチャットだけを Hermes に渡し、アシスタントの反応対象にする。
- 初期版では YouTube チャットへの投稿はしない。
- cron / `send_message` 形式の送信用に `home_channel` をサポートする。
- rate limit、moderation state、重複抑制、cooldown に対応する。

実装構成:

- `plugin.yaml`
- `__init__.py`
- `adapter.py`
- 必要なら `oauth.py`
- 必要なら `hermes gateway setup` 用 setup helper

関連 API:

- YouTube `liveChatMessages.list`
- YouTube `liveChatMessages.streamList`
- YouTube `liveChatMessages.insert` は後続フェーズで検討する。

参考:

- https://developers.google.com/youtube/v3/live/docs/liveChatMessages
- https://developers.google.com/youtube/v3/live/docs/liveChatMessages/list

### 4. Stream Overlay / OBS Control Plugin

OBS オーバーレイ用の plugin または toolset を追加する。配置候補は
`plugins/stream_overlay/` または `plugins/obs_overlay/`。

推奨は、Browser Source overlay と obs-websocket の併用である。

- 字幕、チャット表示、攻略 panel は Browser Source overlay を primary display path にする。
- シーン切り替え、source visibility、OBS text/source 操作は obs-websocket で行う。

責務:

- `http://127.0.0.1:<port>/overlay` のようなローカル overlay page を提供する。
- WebSocket または SSE で overlay state を push する。
- agent に以下のような tool を公開する。
  - `overlay_set_caption(text, ttl_seconds)`
  - `overlay_commit_caption(text, ttl_seconds)`
  - `overlay_show_selected_chat(author, message, ttl_seconds)`
  - `overlay_set_panel(title, body, ttl_seconds)`
  - `overlay_set_image(path_or_url, ttl_seconds)`
  - `overlay_clear(kind)`
  - `overlay_set_mode(mode)`
- obs-websocket 経由の OBS 操作 tool を公開する。
  - `obs_switch_scene(scene_name)`
  - `obs_set_source_visible(source_name, visible)`
  - `obs_set_text_source(source_name, text)`
  - `obs_get_scene_list()`
- OBS に Browser Source として overlay URL を追加するための簡潔な setup guide を用意する。

Browser Source を先に選ぶ理由:

- OBS の plain text source よりもレイアウト自由度が高い。
- 字幕、panel、timer、icon、guide card、chat callout を実装しやすい。
- 表示変更のたびに OBS を直接操作しなくてよい。

想定用途:

- `SetInputSettings` で text source を作成・更新する。
- scene を切り替える。
- source visibility を切り替える。
- browser source URL を更新する。

参考:

- https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md#setinputsettings

### 5. Stream Response Router

アシスタントの返答を無条件に全経路へ流さないように、配信用の routing を追加する。

出力 channel 候補:

- `public_tts`: 配信に乗せる音声として喋る。
- `overlay_caption`: 短い字幕として表示する。
- `overlay_selected_chat`: 選別した YouTube チャットを表示する。
- `overlay_panel`: 長めの攻略情報や状態表示として表示する。
- `obs_action`: scene switch や source 操作を行う。
- `internal_note`: memory / planning 用の内部情報に留める。

routing rule は明示的かつ保守的にする。

- 初期版では YouTube チャットへ投稿しない。
- 選別したチャットに反応する場合は、先にそのチャットを overlay に表示する。
- TTS は短く保つ。長い回答は overlay panel に回す。
- 同じジョークやチャット acknowledgement を繰り返さない。
- チャット返信と TTS には cooldown を入れる。
- チャット spam より配信者の発話を優先する。
- Super Chat / membership は優先してよいが、それでも rate limit する。
- ネタバレ回避は必須。攻略情報を出す前に、現在地点・進行度・許可範囲を確認する。
- scene switch や source visibility の変更は、明示的な意図がある場合だけ行う。

実装候補:

- Hermes API server の前後に小さな決定的 wrapper を置く。
- Gateway plugin hook で final response を書き換え・フィルタする。
- 明示的な stream output tools を持つ新 toolset を追加する。

長期的には、明示的な toolset が最もきれいである。モデルが出力先を意図的に選べるため。

### 6. Game Assistant Skill

ライブ配信用の skill を作る。

skill の責務:

- 声・人格を定義する。
- コメントを短く、配信上安全に保つ。
- ネタバレを必ず避ける。配信者が明示的に許可した範囲を超える攻略情報は出さない。
- 構造化された攻略情報には overlay panel を使う。
- 現在性や未知のゲーム情報が必要な場合だけ web search を使う。
- 現在のゲーム、ルート、ビルド、ボス、目的、配信者の好みを session memory に維持する。
- 配信者との掛け合いをコンテンツとして成立させる。無理に喋り続けず、短いツッコミ・補足・反応を優先する。
- 選別したチャットに反応するときは、チャット本文を overlay に出したうえで話す。

振る舞い例:

- 「配信者に一文だけヒントを出す」
- 「ボスの弱点表を overlay に出す」
- 「この YouTube チャット質問に一文で答える」
- 「配信者から聞かれるか、チャットに直接質問が来るまでは黙る」

設定例:

```yaml
stream_assistant:
  game: ""
  persona: "calm_strategy_cohost"
  spoiler_policy: "strict"
  tts_max_chars: 180
  chat_reply_cooldown_seconds: 20
  overlay_default_ttl_seconds: 12
  allow_public_jokes: true
  allow_chat_replies: false
  show_selected_chat_on_overlay: true
  allow_obs_scene_switching: true
```

## MVP 範囲

最初の有用な MVP では、YouTube platform integration を完全実装する必要はない。

1. OpenAI-compatible なローカルまたはクラウドモデルで Hermes を動かす。
2. アシスタント音声には Fish Audio TTS を使う。Fish Audio plugin 実装中は、既存の Hermes TTS を fallback として使う。
3. まず既存 Hermes voice mode で基本的な push-to-talk を試し、その後 Deepgram streaming STT を追加して、ライブ字幕と低遅延 final transcript に対応する。
4. 小さな外部 YouTube chat bridge を作る。
   - live chat message を取得する。
   - 選別した message を Hermes API server に送る。
   - 選別した message を OBS overlay に表示する。
   - YouTube への reply 投稿は初期版では行わない。
5. ローカル overlay server を作る。
   - `caption.json` または WebSocket state を使う。
   - OBS Browser Source が現在の caption/panel を表示する。
6. stream assistant skill/persona を追加する。

この流れで、first-class Hermes plugin にする前に end-to-end loop を検証する。

## 実装フェーズ

### Phase 1: Deepgram Streaming STT Proof

- [x] 小さな Deepgram streaming client prototype を追加する。
- [x] マイク音声を取得し、chunk を Deepgram に流す。
- [x] partial transcript event を出す。
- [x] final transcript event を出す。
- [x] event contract が固まるまでは、main Hermes STT registry の外側に置く。
- [x] 実際の Deepgram API key とマイクで手動テストする。
- [x] `/voice on` で開始し `/voice off` で停止する常時ストリーミング設定を追加する。
- [x] Deepgram final transcript を短時間バッファして、断片ごとの即応答を抑える。
- [x] 内容マッチをしない debounce/min_chars ベースの会話ターン終端判定を追加する。
- [x] 録音 fixture の replay で pending submit の commit/cancel/rebuffer を検証する。
- [x] TUI runtime に pending submit の commit/cancel/rebuffer path を追加する。
- [x] partial/final transcript をローカル OBS Browser Source overlay に接続する。
- [ ] 投機的な hidden/no-tools draft 生成の設計を固める。
- [ ] 新しい transcript が来たら投機生成を cancel/discard する。
- [ ] draft を確定するまで TUI/OBS/TTS に出さない commit/reveal path を追加する。
- [ ] 小さな LLM または hybrid 判定で `backchannel` も選べるようにする。

検証:

- partial caption が OBS 表示に十分な速度で届く。
- final transcript が Hermes に送れる程度に安定している。
- ゲーム実況中の silence / endpointing 挙動が自然である。
- 接続断でアシスタントが落ちない。

### Phase 2: Local Overlay Proof

- [x] ローカル overlay server を追加する。
- [x] Browser Source 用 HTML/CSS を追加する。
- [x] Deepgram partial transcript を caption に反映する。
- [x] final transcript で partial caption を置き換える。
- [ ] OBS でローカルテストする。
- [ ] `overlay_*` tools を追加する。
- [ ] Hermes tool call から caption と guide panel を表示する。

検証:

- OBS Browser Source が 200-500 ms 程度で更新される。
- Deepgram partial subtitle が過度にちらつかない。
- final transcript が partial caption を自然に置き換える。
- caption が正しく expire する。
- panel text が 1080p / 1440p で自然に折り返される。

### Phase 3: Fish Audio TTS Provider

- Fish Audio TTS plugin を追加する。
- 設定された `reference_id`、model、output format、latency mode に対応する。
- `synthesize()` で完全な音声ファイルを生成する。
- `stream()` をすぐ実装するか、overlay/voice loop が安定してから実装するか評価する。

検証:

- Hermes の `text_to_speech` から Fish Audio 音声を生成できる。
- 生成音声が配信用 audio path で再生される。
- 長い text は chunk するか overlay に回し、長すぎる独白にしない。
- `FISH_API_KEY` がない場合に分かりやすい setup error を出す。

### Phase 4: Stream Persona Skill

- `skills/media/youtube-live-assistant/SKILL.md` などを追加する。
- TTS、overlay、chat reply routing の例を入れる。
- ネタバレと安全性のルールを入れる。
- game-state memory の規約を入れる。
- 選別チャット表示とアシスタント反応の手順を入れる。
- obs scene switching を行う条件を入れる。

検証:

- 長い攻略情報では model が overlay を選ぶ。
- model が TTS を短く保つ。
- YouTube チャットには投稿しない。
- ネタバレを避ける。
- 選別チャットに反応するとき、overlay にそのチャットを表示する。

### Phase 5: YouTube Live Chat Bridge

- まず standalone bridge として実装する。
- YouTube chat message を poll または stream する。
- 直接質問や高優先度イベントを選別して Hermes に転送する。
- 選別したチャットを overlay に表示する。
- dedupe と cooldown state を持つ。
- YouTube API での返信投稿は行わない。

検証:

- reconnect に対応する。
- message を重複処理しない。
- YouTube の polling interval と quota を尊重する。
- 複数時間の配信で動かせる。
- 選別チャットだけが overlay と Hermes に流れる。

### Phase 6: First-Class YouTube Platform Plugin

- bridge を `plugins/platforms/youtube_live` に移す。
- `ctx.register_platform()` で登録する。
- setup/config support を追加する。
- 初期版では inbound と overlay 表示を優先し、`send_message` による YouTube 投稿は後回しにする。
- message normalization と send behavior のテストを追加する。

検証:

- `hermes gateway start` で YouTube Live に接続する。
- incoming chat が `MessageEvent` になる。
- 選別した incoming chat を overlay に表示できる。
- Gateway status に YouTube platform health が表示される。

### Phase 7: OBS Control

- optional な obs-websocket tool を追加する。
- source visibility、scene switching、direct text source update に対応する。
- Browser Source overlay を primary display path として維持する。

検証:

- OBS 接続エラーでアシスタントが壊れない。
- obs-websocket がなくても overlay は動く。
- source update は明示的で rate limit されている。
- scene switch は明示的な条件でのみ実行される。

## モデル検討

RX 9070 XT / Windows のローカルテスト:

- LM Studio を OpenAI-compatible local server として使うところから始める。
- 16 GB メモリと tool-use の相性を考え、まず `gpt-oss-20b` を試す。
- より強い汎用 agent 挙動が必要なら `Qwen3-30B-A3B-GGUF:Q4_K_M` を試す。ただし memory/context が許容できるか確認する。
- code-heavy な作業には `Qwen3-Coder-30B-A3B-Instruct` を試す。ただし 16 GB VRAM では
  `Q3_K_M` など小さめの quant を使う。

本番配信:

- ローカルモデルの tool call 失敗や reasoning 不足に備えて、安価なクラウド fallback model を用意する。
- 配信中の stall を減らすため、ローカルでは大きすぎる context size を避ける。
- Fish Audio TTS 生成は、可能なら main model path から分離する。
- Deepgram streaming STT を live-caption / voice-input の primary path とし、
  local faster-whisper は offline または emergency fallback にする。

## リスク

- YouTube API quota と OAuth が複雑。
- live chat volume が大きいと、厳しい throttling なしでは agent が圧倒される。
- Streaming STT は、配信中ずっと長時間の network dependency を追加する。
- Partial STT caption は、強く表示しすぎるとちらつきや誤認識を晒す。
- 投機的な返答生成は、確定前に外へ出すとキャンセル不能な配信事故になる。
- 投機中に tool call を許すと、キャンセルしたはずの OBS 操作や投稿が実行される。
- Fish Audio TTS の latency と streaming/file-output の挙動は、全発話に使う前に実配信に近い条件で検証が必要。
- TTS latency が大きいと、アシスタントの返答が配信上不自然になる。
- ローカル LLM の tool-calling reliability は model と quantization に左右される。
- OBS overlay design では、text wrapping と expiration behavior を慎重に扱う必要がある。
- public voice output では、危険なチャットを読み上げない安全ルールが必要。
- scene switch の誤操作は配信事故につながるため、許可条件と cooldown が必要。

## 未決事項

- public TTS と字幕の音量・表示頻度をどの程度にするか。
- chat の選別ルールをどうするか。mention、質問、Super Chat、NG ワード除外など。
- YouTube chat posting を将来入れるか。その場合、完全自動か配信者承認付きか。
- 初期ターゲットのゲームは何か。
- Deepgram final transcript は直接 Hermes に渡すか、wake-word / intent filter を挟むか。
- partial transcript の見せ方をどうするか。薄い表示、点滅抑制、final で置換など。
- Fish Audio playback はまず full-file generation にするか、最初から chunked streaming playback を作るか。
- Fish Audio のどの voice / `reference_id` をデフォルトの stream persona にするか。
- obs-websocket の scene switch をどこまで agent に許可するか。
- これは core、bundled plugin、別 plugin repo のどこに置くべきか。

## 推奨される次の作業

まず Phase 1 から Phase 4 までを作る。

1. Deepgram streaming STT prototype。
2. Overlay tool + local Browser Source。
3. Fish Audio TTS provider。
4. Stream assistant skill/persona。
5. ローカルモデル、OBS、Fish Audio TTS、Deepgram captions を使った手動テスト。

voice/overlay loop の感触が良くなってから、YouTube chat ingestion を追加する。
