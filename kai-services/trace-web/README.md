# trace-web

kai のセッションログ（発話・実況・ツール実行・LLM 呼び出し）を **セッション単位**で
ブラウズし、**「操作」と「実況」を時刻で並べて対比**できる Web ビューア（Next.js）。
kai-vm 上で常駐し、Tailscale 内の別マシン（Mac 等）から
`http://<kai-vm の Tailscale IP>:8910/` で閲覧する。

- **リアルタイム更新**: SSE（`/api/stream`）で JSONL の追記分を push（配信中の監視に）
- **読み取り専用**: `<HERMES_HOME>/kai_trace/YYYY-MM-DD.jsonl` を読むだけ。書き込まない
- 旧 `trace-viewer`（Python 単一ファイル）の後継

## 画面

- `/` — 日付選択 + **セッション一覧**（開始時刻・タスク・ツール/発話/LLM 件数・実行中）
- `/sessions/[id]?date=` — **操作 vs 実況**（左=ツール・LLM / 右=発話）を時刻で対比

## API

| エンドポイント | 内容 |
| --- | --- |
| `GET /api/dates` | 利用可能な日付一覧 |
| `GET /api/sessions?date=` | セッション要約一覧 |
| `GET /api/sessions/[id]?date=&after=` | セッションのイベント |
| `GET /api/stream?date=&session=&after=` | SSE。追記イベントを push |

## セットアップ（VM 上）

```bash
bash ~/kai-agent/kai-services/trace-web/install.sh   # npm ci + build + systemd 常駐
```

## 環境変数

| 変数 | 既定 | 意味 |
| --- | --- | --- |
| `TRACE_VIEWER_PORT` / ポート | 8910 | `package.json` の start/dev で指定 |
| `HERMES_HOME` | `~/.hermes` | トレースの親。`kai_trace/` を読む |
| `TRACE_DIR` | `$HERMES_HOME/kai_trace` | 直接指定する場合 |
