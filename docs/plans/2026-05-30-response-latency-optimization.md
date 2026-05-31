# 配信アシスタント応答レイテンシ最適化設計

作成日: 2026-05-30

対象ブランチ: `feature/youtube-live-ai-assistant`

## 目的

YouTube ゲーム実況の AI アシスタントとして、配信者が話し終えてからアシスタントが
話し始めるまでの体感待ち時間を極限まで短くする。

目標は「自然な掛け合い」に近づけること。正確な最終回答を待ってから音声を出すのではなく、
人間同士の会話に近い形で、聞き終わりを予測し、短い初動を素早く返し、必要な詳細は後続の
字幕・overlay・追加発話に分離する。

## 現状の経路

```text
microphone
  -> Deepgram streaming STT
  -> TUI gateway turn detection
  -> prompt.submit
  -> AIAgent.run_conversation(stream_callback=...)
  -> message.delta / OBS assistant caption
  -> message.complete
  -> hermes_cli.voice.speak_text()
  -> tools.tts_tool.text_to_speech_tool()
  -> Fish Audio REST TTS
  -> MP3 file
  -> local playback
```

現状すでにできていること:

- Deepgram の partial transcript を OBS host caption に出せる。
- final transcript を TUI gateway に渡して agent turn を開始できる。
- assistant の `message.delta` を OBS assistant caption に出せる。
- Fish Audio REST TTS で assistant 音声を生成・再生できる。

現状の主なボトルネック:

- `streaming_stt.submit.debounce_ms`: final transcript 後の沈黙待ち。
- `streaming_stt.submit.llm_wait_debounce_ms`: LLM classifier が wait した後の追加待ち。
- `streaming_stt.submit.commit_delay_ms`: 誤確定回避のための遅延。現在は 1000 ms。
- `speak_text()`: `message.complete` 後に全文を TTS に渡すため、LLM が全部出し終わるまで発話開始できない。
- Fish Audio REST TTS: MP3 ファイルが完成するまで再生できない。

## レイテンシ予算

まずは以下を目標値にする。

| 区間 | 現状 | 目標 |
|---|---:|---:|
| 発話終了予測 | 約 800-3000 ms | 300-800 ms |
| LLM first token | model依存 | 300-900 ms |
| 字幕 first assistant text | LLM first token後ほぼ即時 | 維持 |
| TTS first audio | final response後 | LLM first sentence後 300-800 ms |
| 体感 first voice | 数秒 | 1.0-2.0 秒以内 |

品質優先時は 2 秒台でもよいが、掛け合いコンテンツとしては「短い一言」が 1 秒台で返ることを
最優先にする。

## 方針

### 0. STT -> LLM -> TTS を全て streaming でつなぐ

最終形は、各段階が「前段の完了」を待たない pipeline にする。

```text
Deepgram partial/final stream
  -> turn state machine
  -> LLM streaming request
  -> assistant text delta stream
  -> Fish Audio WebSocket TTS
  -> audio chunk playback
  -> OBS captions / overlay
```

重要なのは、streaming といっても全てを即公開するわけではないこと。

- STT partial は public caption に出してよい。
- LLM speculative delta は commit まで非公開 buffer に入れる。
- assistant caption と TTS は、user turn が commit された後だけ公開する。
- commit 後は LLM delta を TUI/OBS/TTS へ同時に流す。
- user が話し始めたら、LLM generation、TTS generation、audio playback を同じ `turn_id` で止める。

これにより、内部は常時 streaming だが、配信に乗るものは安全に制御できる。

### Streaming Turn Pipeline

各 voice turn は `turn_id` を持ち、以下の channel を流れる。

```text
VoiceTurn(turn_id)
  input:
    stt_partial
    stt_final
    speech_activity

  internal:
    possible_user_text
    committed_user_text
    speculative_llm_delta
    committed_llm_delta
    tts_audio_chunk

  public:
    host_caption
    assistant_caption
    assistant_audio
    overlay_panel
```

この設計では「テキストを文字列として渡す」のではなく、「event stream を接続する」。

最小 event 例:

```python
@dataclass
class VoiceTurnEvent:
    turn_id: str
    kind: Literal[
        "stt_partial",
        "stt_final",
        "turn_possible_end",
        "turn_commit",
        "turn_cancel",
        "llm_delta",
        "llm_complete",
        "tts_audio",
        "tts_complete",
    ]
    text: str = ""
    audio: bytes = b""
    final: bool = False
```

TUI gateway はこの stream の orchestrator になる。Deepgram、AIAgent、Fish Audio は
provider adapter として扱う。

### Partial と Final の扱い

STT:

- partial: 字幕には出す。LLM には直接公開しない。
- final: turn buffer に追加する。
- speech_final / VAD: possible end の signal にする。

LLM:

- speculative mode: delta は buffer する。OBS/TTS には出さない。
- committed mode: delta を OBS assistant caption と TTS に即時 fan-out する。
- tool call が出た場合、speech hot path では止めるか、短い「確認するね」を先に返す。

TTS:

- committed LLM delta を受け取る。
- Fish Audio WebSocket に text event として送る。
- audio chunk を受け取ったら即再生する。
- Barge-in で playback と WebSocket generation を止める。

### Commit/Revert の境界

streaming pipeline でも、公開境界は必要。

```text
private stream:
  STT partial -> possible transcript -> speculative LLM

public stream:
  committed user transcript -> committed LLM delta -> TTS audio / OBS caption
```

`commit_delay_ms` は「LLM を始めるまでの待ち」ではなく「公開するまでの待ち」に変える。
つまり、待っている間にも LLM は裏で走れる。

理想的には:

1. Deepgram が speech_final または silence を出す。
2. すぐ speculative LLM を開始する。
3. 500 ms 程度だけ追加 speech を待つ。
4. 続きがなければ buffered LLM delta を公開し、以後 streaming で TTS に流す。
5. 続きがあれば speculative LLM と TTS queue を破棄し、STT buffer に戻す。

### 1. 応答を2系統に分ける

配信で重要なのは、最初の音声が速いこと。完全な解説を音声で待つ必要はない。

出力を以下に分ける。

- `speech`: すぐ話す短い返答。1-2文、長くても 80-120 文字程度。
- `overlay`: 攻略情報、箇条書き、補足、参照情報。OBS の表示に回す。

最初の実装では構造化 JSON までは強制せず、system/developer prompt で「音声返答は短く」と
誘導する。安定しなければ、後で `speech` / `overlay` の structured response にする。

### 2. LLM 生成と TTS を直列にしない

現在は `message.complete` 後に `speak_text(raw)` を呼ぶ。これをやめ、`message.delta` を
TTS pipeline にも流す。

新しい経路:

```text
LLM delta
  -> TUI / OBS assistant caption
  -> speech chunker
  -> Fish Audio WebSocket TTS
  -> audio chunk playback
```

speech chunker は句点、読点、改行、一定文字数で小さな単位を作る。Fish Audio WebSocket TTS は
LLM streaming text をそのまま受けられ、内部で自然な長さになるまで buffer するため、過度な
手動 batch は不要。必要なところだけ flush する。

### 3. Fish Audio WebSocket TTS を使う

Fish Audio の公式 WebSocket TTS は `wss://api.fish.audio/v1/tts/live` を使い、
`start`、`text`、`flush` などの event を MessagePack で送る。`StartEvent.request.text` は
空にして、その後の `TextEvent` で LLM delta を送る。

設計上の扱い:

- `/voice tts` が on になった時点で WebSocket を preconnect する。
- assistant turn 開始時に session を start する。
- `message.delta` を text event として投入する。
- 最初の一文、または 40-80 文字程度で flush する。
- 音声 chunk は到着し次第再生する。
- turn 終了時に final flush する。

REST TTS は fallback として残す。

### 4. 投機的 LLM 生成

STT の確定待ちを短くしすぎると、配信者が文中 pause しただけで agent が割り込む。
これを避けるため、確定前に hidden draft を作る。

状態:

```text
listening
  -> possible_end
  -> speculative_generating
  -> committed
  -> speaking
  -> idle
```

動作:

- `possible_end`: 一定時間 speech が止まったら、final commit 前に draft LLM を開始する。
- `speculative_generating`: TUI/OBS/TTS にはまだ出さない。
- 新しい partial/final が来たら draft をキャンセルし、buffer に戻す。
- `commit_delay_ms` を過ぎても新しい speech がなければ、draft を reveal する。
- draft が間に合っていなければ、そのまま通常 turn として継続する。

重要な制約:

- 確定前の draft は TUI、OBS、TTS、YouTube chat に出さない。
- history にも書かない。commit 時だけ正式 turn にする。
- cancel できない provider の場合は、結果を破棄するだけでもよい。

実装上は `AIAgent.run_conversation()` が同期で history を返すため、最初は別 agent instance または
history snapshot を使った hidden worker にする。正式 commit 時に同じ結果を流用できるように
`speculation_id` と transcript hash を持つ。

### 5. Barge-in

人間同士の会話では、相手が話し始めたらこちらは止まる。配信用でも同じにする。

条件:

- assistant TTS 再生中に Deepgram partial が来たら、再生を停止する。
- TTS WebSocket session も cancel/close する。
- OBS assistant caption は必要なら薄く残すが、音声は止める。
- user transcript buffer を優先する。

これにより、誤って長い返答を始めても配信者がすぐ割り込める。

### 6. LLM hot path を軽くする

音声の初動では tool call や長い推論を避ける。

候補:

- voice turn 用の `max_output_tokens` を短めにする。
- spoken first response では tool call を原則禁止し、必要なら「調べながら補足する」と短く返す。
- 攻略情報やチャット選別は background task で先読みし、hot path では cache を読む。
- persona prompt に「最初は短く、詳細は overlay」と明記する。

ゲーム攻略の正確性は重要だが、発話開始を遅らせるべきではない。攻略情報は overlay 側に逃がす。

## 実装フェーズ

### Phase 1: 計測

まず各区間の timestamp をログに出す。

- `stt.partial.first`
- `stt.final`
- `turn.possible_end`
- `turn.commit_scheduled`
- `turn.committed`
- `llm.request_start`
- `llm.first_delta`
- `llm.complete`
- `tts.request_start`
- `tts.first_audio`
- `tts.play_start`
- `tts.play_end`

ログは `agent.log` に session_id / turn_id / transcript hash 付きで出す。
この計測なしに調整すると、体感だけで regress しやすい。

### Phase 2: sentence-level TTS

Fish Audio WebSocket の前に、現行 REST TTS でもできる改善を入れる。

- `message.delta` を集めて文単位に分割する。
- 1文目ができた時点で REST TTS を開始する。
- 2文目以降を queue して順次再生する。
- final response まで待たない。

これは WebSocket 実装より簡単で、効果を早く確認できる。

### Phase 3: Fish Audio WebSocket TTS

`hermes_cli/streaming_tts.py` を追加する。

責務:

- Fish Audio WebSocket 接続。
- MessagePack event encode/decode。
- text chunk queue。
- audio chunk playback。
- cancel / close。
- fallback to REST TTS。

実装メモ:

- 2026-05-30 時点で `hermes_cli/streaming_tts.py` に最小 adapter を追加済み。
- 実 API 接続で `first_audio_ms=894`、`chunks=5`、`audio_bytes=78993` を確認。
- TUI gateway は `tts.fish_audio.streaming_enabled: true` のときだけ、
  `message.delta` を `FishAudioStreamingTTSWorker` に流す。
- 音声再生は初期実装では `ffplay` の stdin に MP3 chunk を流す。
- 既存の REST TTS は fallback として残す。

試験用設定:

```yaml
tts:
  provider: fish_audio
  fish_audio:
    model: s2-pro
    reference_id: "<voice model id>"
    latency: balanced
    format: mp3
    streaming_enabled: true
```

依存:

- 既存依存に `websockets` はある。
- Fish Audio WebSocket は MessagePack を使うため、`msgpack==1.1.2` を core dependency に追加。

### Phase 4: speculative response

TUI gateway に speculation manager を追加する。

最小構成:

- transcript snapshot。
- history snapshot。
- hidden worker。
- cancel flag。
- commit/reveal。

初期版では、hidden worker の結果を一括 reveal してよい。次に hidden streaming delta を buffer し、
commit されたら即時に buffer を TUI/OBS/TTS へ流す。

### Phase 5: speech / overlay 分離

assistant response を短い speech と詳細 overlay に分ける。

候補:

- prompt のみで運用。
- `speech:` / `overlay:` の軽いテキスト convention。
- structured output。
- dedicated tool `live_overlay_panel`。

最初は prompt convention で始め、破綻したら structured output に進む。

## 初期設定案

まず試す値:

```yaml
streaming_stt:
  submit:
    debounce_ms: 900
    llm_wait_debounce_ms: 1400
    commit_delay_ms: 500
    max_wait_ms: 3500
    require_speech_final: false
  deepgram:
    endpointing: 400
    chunk_ms: 80

tts:
  provider: fish_audio
  fish_audio:
    model: s2-pro
    latency: balanced
```

注意:

- `require_speech_final: false` は割り込みリスクが上がる。投機的生成が入るまでは慎重にする。
- `commit_delay_ms` を 0 にすると速いが、文中 pause に弱くなる。
- `latency: low` は品質や自然さとの tradeoff を確認してから使う。

## リスク

- 速くしすぎると、配信者の文中 pause に割り込む。
- 投機 draft が古い transcript に基づいて話すと不自然。
- TTS chunk が細かすぎると音声が不自然。
- barge-in が弱いと agent が自分の TTS を STT して feedback loop する。
- tool call を hot path で許すと、ゲーム中の会話テンポが崩れる。

## 次にやること

1. レイテンシ timestamp ログを入れる。
2. `message.delta` ベースの sentence-level TTS queue を作る。
3. Fish Audio WebSocket TTS の最小実装を追加する。
4. Barge-in で再生を止める。
5. 投機的 LLM 生成を hidden/reveal 方式で入れる。

## 参考

- Fish Audio WebSocket TTS: https://docs.fish.audio/api-reference/endpoint/websocket/tts-live
- Fish Audio Python WebSocket guide: https://docs.fish.audio/sdk-reference/python/websocket
- Fish Audio Text-to-Speech guide: https://docs.fish.audio/sdk-reference/python/text-to-speech
