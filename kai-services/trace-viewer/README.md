# trace-viewer

kai のセッションログ（発話・字幕・ツール実行・LLM 呼び出し）をブラウザで
時系列表示する Web ビューア。kai-vm 上で常駐し、Tailscale 内の別マシン
（Mac 等）から `http://<kai-vm の Tailscale IP>:8910/` で閲覧する。

データソースは `<HERMES_HOME>/kai_trace/YYYY-MM-DD.jsonl`
（`plugins/kai_trace` と `kai-services/speechd` が書く共通エンベロープ）。
**読み取り専用**で、トレースへの書き込み・変更は一切しない。

## 機能

- 日付選択（トレースファイルの UTC 日付。時刻表示はブラウザのローカル時刻）
- 種別フィルタ: 🗣 発話（テキスト・source 付き）/ 💬 字幕詳細（再生完了・クリア）/
  🔧 ツール実行（コマンド・所要時間・エラー）/ 🤖 LLM 呼び出し・応答 / ⏻ セッション
- ライブ追従: 2 秒間隔のポーリングで追記分だけを取得（配信中の監視に使える）

## 依存

Python 3 標準ライブラリのみ。追加パッケージ・外部 CDN なし（HTML/JS/CSS は自己完結）。

## セットアップ（VM 上）

```bash
bash ~/kai-agent/kai-services/trace-viewer/install.sh
```

## 環境変数

| 変数                | デフォルト                 | 意味                                  |
| ------------------- | -------------------------- | ------------------------------------- |
| `TRACE_VIEWER_PORT` | `8910`                     | HTTP サーバーの listen ポート         |
| `TRACE_VIEWER_BIND` | `0.0.0.0`                  | bind アドレス（下記セキュリティ参照） |
| `TRACE_DIR`         | `<HERMES_HOME>/kai_trace`  | トレース JSONL のディレクトリ         |
| `HERMES_HOME`       | （未設定なら `~/.hermes`） | speechd と同じプロファイル規約        |

## API

```text
GET /                 ビューア HTML
GET /api/dates        利用可能な日付一覧（JSON 配列）
GET /api/events?date=YYYY-MM-DD&after=N
                      after 行目より後のイベントと次カーソル
                      {"events": [{"n", "ts", "component", "kind", "session_id", "payload"}], "next": N}
```

## セキュリティ

- 既定で `0.0.0.0` に bind するが、VM は UTM の NAT 配下 + Tailscale のみで
  公開ポートは持たない（到達できるのはホスト Mac と tailnet 内のみ）
- トレース本文は書き込み側の三層マスク（kai_trace / kai_narrator / speechd）で
  秘匿処理済み。本サーバーは新たな秘匿情報を扱わない
- 読み取り専用（GET のみ）。`date` パラメータは `YYYY-MM-DD` 形式のみ受理
  （パストラバーサル防止）

## 手動検証

```bash
# VM 上で
curl -s http://127.0.0.1:8910/api/dates
curl -s "http://127.0.0.1:8910/api/events?date=$(date -u +%F)&after=0" | head -c 300
# 別マシンのブラウザから http://<kai-vm の Tailscale IP>:8910/ を開き、
# 日付選択・フィルタ・ライブ追従（/say を叩いて発話行が増えること）を確認
```
