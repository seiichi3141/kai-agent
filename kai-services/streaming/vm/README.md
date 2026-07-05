# kai-services/streaming/vm

kai-vm（UTM / Ubuntu 24.04 Desktop arm64）上で動かす、配信ステージ用のセットアップ・運用スクリプト群です。
詳しい運転手順は `docs/kai/m4-runbook.md` に置き、この README は各ファイルの入口と使い分けだけをまとめます。

## ファイルの役割

- `setup.sh`
  - kai 配信スタックのセットアップスクリプトです。UTM VM 内で apt パッケージ、Tailscale、PipeWire null-sink、GDM の自動ログイン + X11 固定、画面ロック無効化、SSH 有効化などを冪等に設定します。
  - Ubuntu Desktop を標準インストール済みで、ネットワーク接続がある VM 内ターミナルから実行する前提です。

- `build-obs-browser.sh`
  - Ubuntu apt 版 OBS arm64 で欠けている obs-browser（CEF ブラウザソース）だけをビルドし、OBS のユーザープラグインとして配置します。
  - 字幕オーバーレイを OBS のブラウザソースで配信映像に合成するための補助です。CEF 取得とビルドで 20〜40 分、ディスク約 1GB を使います。

- `setup-vscode.sh`
  - kai-vm に配信ステージ用の VSCode を導入・設定します。
  - VSCode 本体、配信向け `settings.json` / `argv.json`、空パスワードのデフォルトキーリング、`kai-typewriter` 拡張を冪等に配置します。

- `broadcast.sh`
  - OBS の起動、配信開始・停止、録画、状態確認、スクリーンショット、シーン切替、冒頭スライドの agenda 更新をまとめた配信制御 CLI です。
  - OBS の終了はシーン保存のため、このスクリプトの `stop` 経由でクリーン終了する前提です。

- `obsws.py`
  - kai 用の最小 obs-websocket v5 クライアントです。
  - OBS 設定から接続情報を読み、単発リクエストまたは JSON 配列の batch を実行して JSON Lines で結果を出します。

- `vscode/kai-typewriter/`
  - kai のファイル編集を受信し、VSCode 上でタイピング風アニメーションとして再生する配信演出拡張です。
  - `POST http://127.0.0.1:8920/edit` で編集済みファイル一覧を受け取り、最終的にはディスク上の内容へ収束する best-effort の演出として動きます。

## 典型的な流れ

### 初回セットアップ

1. VM に Ubuntu 24.04 Desktop arm64 を標準インストールします。
2. このリポジトリを VM のホーム配下へ clone します。
3. `setup.sh` で配信スタックの土台を入れ、必要に応じて再起動と Tailscale 認証を行います。
4. `build-obs-browser.sh` で字幕用の OBS ブラウザソースを有効化します。
5. `setup-vscode.sh` で VSCode 配信ステージと `kai-typewriter` 拡張を入れます。
6. OBS の初期シーン、字幕ブラウザソース、冒頭スライドなどの細かい確認は `docs/kai/m4-runbook.md` を見ます。

### 配信当日

1. `docs/kai/m4-runbook.md` の事前チェックリストで、機密・認証・サービス疎通を確認します。
2. VSCode + tmux の配信ステージを起動します。
3. `broadcast.sh agenda` で冒頭スライドの予定を設定します。
4. `broadcast.sh start` で OBS を起動し、websocket 疎通と画面を確認します。
5. 配信開始、シーン切替、状態確認、終了手順は `docs/kai/m4-runbook.md` の運転台本に従います。

配信中の実装タスクでは、事故防止のため `broadcast.sh status` や `screenshot` 以外の配信制御コマンドを不用意に実行しないでください。
