# trace-shots

kai の**操作ごとに OBS のスクリーンショットを撮り**、trace-web に紐づけて表示する
ための独立プロセス。kai_trace の当日 JSONL を tail し、`tool_call` イベントを
見つけるたびに OBS のスクショを撮って
`<HERMES_HOME>/kai_trace/shots/<session_id>/<行番号>.jpg` に保存する。

trace-web はこの**行番号**で操作イベントと画像を対応づけ、セッション詳細の
「操作」列にサムネイル表示する（`/api/shot?session=&n=`）。

## 特徴

- **読み取り + 画像保存のみ**。トレース JSONL は書き換えない
- **OBS 起動時のみ**撮る（obs-websocket 経由）。未起動・失敗は黙ってスキップ（best-effort）
- kai の実行に影響しない**独立プロセス**（hook に遅延を足さない）
- 起動後に発生した新しい `tool_call` だけ撮る（過去は今の画面と合わないため撮らない）
- obs-websocket 接続は `kai-services/streaming/vm/obsws.py` を再利用

## 使い方（VM 上、OBS 起動中に）

```bash
python3 ~/kai-agent/kai-services/trace-shots/shot_daemon.py
# または systemd 常駐:
cp kai-trace-shots.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now kai-trace-shots
```

## 環境変数

| 変数           | 既定        | 意味                              |
| -------------- | ----------- | --------------------------------- |
| `SHOT_SOURCE`  | `シーン`    | 撮る OBS ソース名（配信画面全体） |
| `SHOT_WIDTH`   | `960`       | 保存幅 px（縦横比維持）           |
| `SHOT_QUALITY` | `70`        | JPEG 品質 1–100                   |
| `SHOT_POLL_MS` | `700`       | トレース tail 間隔 ms             |
| `HERMES_HOME`  | `~/.hermes` | トレースの親                      |
