# aquestalk-server

Mac 上で常駐する汎用日本語 TTS HTTP サーバー。AquesTalk10（`aquestalk_cli`）を使い
「日本語テキスト → 音声」を提供する自己完結サービス。kai 固有ロジックは持たない、
純粋な TTS API。

依存は [kuromoji](https://www.npmjs.com/package/kuromoji) のみ。TypeScript は使わず
素の Node.js ESM（`.mjs`）でビルド不要。

## セットアップ

```bash
cd kai-services/aquestalk-server
npm install
cp .env.example .env
# .env を編集して AQUESTALK_CLI_PATH / AQUESTALK_SDK_DIR / ライセンスキーを設定する
npm start
```

デフォルトでは `http://127.0.0.1:8890` で待ち受ける。

## 環境変数

| 変数名                | 必須 | 説明                                                              | デフォルト  |
| ---------------------- | ---- | ----------------------------------------------------------------- | ----------- |
| `AQUESTALK_CLI_PATH`   | ○    | `aquestalk_cli` バイナリのパス                                     | -           |
| `AQUESTALK_SDK_DIR`    | ○    | `libAquesTalk10.dylib` のあるディレクトリ（`DYLD_LIBRARY_PATH` に設定） | -           |
| `AQUESTALK_DEV_KEY`    | -    | AquesTalk10 開発者ライセンスキー（subprocess の env にそのまま継承） | -           |
| `AQUESTALK_USR_KEY`    | -    | AquesTalk10 ユーザーライセンスキー（同上）                          | -           |
| `BIND_ADDR`            | -    | HTTP サーバーの bind アドレス                                      | `127.0.0.1` |
| `PORT`                 | -    | HTTP サーバーのポート                                              | `8890`      |

`.env` はプロセス起動時に `process.loadEnvFile()`（Node 20.12+ / 21.7+）で読み込まれる。
`.env` が存在しない場合は無視され、シェルや launchd 等で別途設定済みの環境変数がそのまま使われる。

## API

### `GET /health`

```json
{ "ok": true }
```

### `POST /synthesize`

リクエストボディ（JSON）:

```json
{
  "text": "変換したいテキスト",
  "voice": "F1",
  "speed": 120
}
```

- `text`: 必須。日本語テキスト（漢字かな交じり可）。
- `voice`: 省略可。AquesTalk10 の声種（デフォルト `"F1"`）。
- `speed`: 省略可。話速（デフォルト `120`）。

処理の流れ:

1. `text` を文単位に分割する（句点「。」「！」「？」の直後・改行で分割）。
2. 各文を `toKoe()` で AquesTalk10 音声記号列（koe）に変換し、`synthesize()` で
   `aquestalk_cli` を呼び出して WAV を生成する。
3. **各文を 1 行の NDJSON として逐次ストリーミング**する
   （`Content-Type: application/x-ndjson`、文の処理が終わるたびに `res.write`）。
4. ある文の koe 変換・音声合成が失敗しても、その文のみ `error` を出して次の文へ進む
   （リクエスト全体は中断しない）。
5. 全文の処理が終わると接続をクローズする。

レスポンス（NDJSON、1 行 1 文）:

成功した行:

```json
{ "seq": 0, "text": "こんにちは。", "koe": "こんにちは。", "wav_base64": "UklGRi..." }
```

失敗した行:

```json
{ "seq": 1, "text": "変換に失敗した文", "error": "AQUESTALK_CLI_PATH environment variable is not set" }
```

エラーレスポンス（バリデーション失敗時、通常の JSON）:

- `400` — `text` が空、または JSON ボディが不正
  例: `{ "error": "text is required" }`
- `404` — 未定義のパス
  例: `{ "error": "not found" }`

## 手動検証手順

`AQUESTALK_CLI_PATH` / `AQUESTALK_SDK_DIR` が実環境に設定された状態でサーバーを起動し、
以下のように `curl` で確認する（実際の音声合成の実機確認はオーケストレーター側で行う）。

```bash
# ヘルスチェック
curl -s http://127.0.0.1:8890/health

# 音声合成（NDJSON が1行ずつ届く。--no-buffer でストリーミングを確認しやすい）
curl -s --no-buffer -X POST http://127.0.0.1:8890/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text": "こんにちは。今日はいい天気ですね。", "voice": "F1", "speed": 120}'

# 1 文目の wav_base64 を取り出して実際に音声ファイルへデコードする例（jq が必要）
curl -s --no-buffer -X POST http://127.0.0.1:8890/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text": "こんにちは。"}' \
  | head -n1 | jq -r '.wav_base64' | base64 -d > /tmp/out.wav
afplay /tmp/out.wav
```

`AQUESTALK_CLI_PATH` 等が未設定の場合、`/synthesize` は各行に `error` を含んだ NDJSON を
返す（全体は 200 で開始し、文ごとにエラーを継続出力する）。

## テスト

```bash
npm test
```

`src/converter.test.mjs` で `toKoe` / `preprocessSymbols` / `formatKoe` /
`splitSentences` の基本ケース（ひらがな入力、技術用語変換、句点分割等）を検証する。
kuromoji の辞書初期化を待ってからテストを実行するため、初回実行は数秒かかることがある。
`aquestalk_cli` を実際に呼び出す音声合成のテストは環境依存のため含めていない
（上記の手動検証手順を使うこと）。

## 構成

```
kai-services/aquestalk-server/
├── package.json
├── README.md
├── .env.example
├── .gitignore
└── src/
    ├── server.mjs            # HTTP サーバー本体
    ├── converter.mjs         # テキスト → AquesTalk10 音声記号列（koe）変換
    ├── converter.test.mjs    # converter / text-splitter のユニットテスト
    ├── technical-terms.json  # 技術用語読み辞書
    ├── aquestalk.mjs         # aquestalk_cli 呼び出し（koe → WAV）
    └── text-splitter.mjs     # テキストの文単位分割
```
