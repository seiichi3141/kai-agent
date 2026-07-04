# kai-overlay

kai の配信画面に重ねる Web オーバーレイ。字幕を表示する（将来的にはアバターや
コメント表示なども同じオーバーレイに追加していく想定の土台）。

設計の正典: `docs/kai/design/00-system.md` §4「発話・字幕同期メカニズム」。
字幕は当初 OBS の text ソース（ファイル読み込み）方式だったが、汎用性
（将来アバター・コメント・進捗も同じ overlay で表現し、OBS ソースを増やさない）
のため **Web オーバーレイ + SSE 購読方式**に変更した。字幕データの生成元は
`kai-services/speechd/`（`GET /events` で SSE 配信）。

## 構成

- `index.html` — オーバーレイのページ本体。背景完全透過。
- `style.css` — 字幕の見た目（白文字+ 黒縁取り、フェードイン/アウト）。
- `app.js` — `EventSource` で speechd の `/events` を購読し、`subtitle` イベントを
  字幕 DOM に反映する。外部依存なし（素の JS）。
- `show-overlay.sh` — VM の X11 デスクトップ（`:0`）に、このページを透過・
  最前面のウィンドウとして開くスクリプト。

## 仕組み

`app.js` は起動時に `new EventSource("http://127.0.0.1:8900/events")`
（speechd の SSE エンドポイント）へ接続する。イベントは

```jsonc
{
  "type": "subtitle",
  "text": "表示する文",
  "source": "agent_response",
  "emotion": "happy",
}
```

の形式で届く。`text` が空文字なら字幕をフェードアウトさせる。`type` が
`"subtitle"` 以外（未知の値）のイベントは無視して落ちない設計にしてあり、
将来アバターの表情通知やコメント表示などを同じ `/events` に追加しても
この overlay 側の変更なしに安全に無視できる（対応するハンドラを
`app.js` の `handlers` オブジェクトに追加すれば拡張できる）。

`EventSource` はブラウザ標準の挙動として接続断からの自動再接続を行う
（speechd 再起動時などもオーバーレイ側の操作は不要）。`onerror` はログ
出力のみ行い、処理は止めない。

SSE エンドポイントは既定で `http://127.0.0.1:8900/events`
（`kai-services/speechd/speechd.py` の `SPEECHD_PORT` 既定値）を見る。
別ホスト・別ポートの speechd を見たい場合は `?sse=` クエリパラメータで
上書きできる:

```text
file:///home/kai/kai-agent/kai-services/overlay/index.html?sse=http://127.0.0.1:9900/events
```

## 見た目

- 背景完全透過（`html, body { background: transparent; }`）。デスクトップに
  透過ウィンドウで重ねてそのまま配信画面に取り込む前提（OBS 側で追加の
  カラーキー処理は不要）。
- 画面下部中央、白文字・太字・黒縁取り（`-webkit-text-stroke` +
  `text-shadow` の多重指定）、`Noto Sans CJK JP` 相当のフォント、48px。
- 表示/非表示は `opacity` の `transition`（200ms）でフェードする。
- 長文は `max-width: 1600px` で中央寄せしつつ折り返す。

## VM デスクトップへの表示（`show-overlay.sh`）

前提: `DISPLAY=:0` の X11 セッションが起動済み（`kai-services/streaming/`
の `kai-desktop.service` 等でセットアップ済みのデスクトップ）。

```bash
cd kai-services/overlay
DISPLAY=:0 bash show-overlay.sh
```

内部で行っていること:

1. Chromium を `--app=file://.../index.html` で起動（URL バー等の chrome UI
   なし）。`--window-size=1920,1080 --window-position=0,0` でデスクトップ
   全面に配置。
2. **透過表示のための Chromium フラグ**: `--enable-transparent-visuals`
   （X11 の ARGB visual を使わせ、ページの `background: transparent` が
   実際にデスクトップへ透過するようにする）と `--disable-gpu`
   （GPU 合成パスが透過を上書きしてしまうことがあるため無効化。X11 +
   software 合成のほうが透過ウィンドウでは安定する）。
   `--enable-features` や `--force-dark-mode` の類は使っていない（それらは
   透過とは無関係、または副作用でページの色を変えてしまう）。
3. `xdotool search --class kai-overlay` でウィンドウを検出し、
   `wmctrl -i -r <id> -b add,above` で最前面固定を試みる（完璧でなくてよい。
   まず overlay が最前面に透過表示されてデスクトップキャプチャに入ることが
   目標）。

**透過が効かない場合の確認ポイント**（多くは compositor 側の問題）:

- ウィンドウマネージャのコンポジタが有効か（XFCE は既定で `xfwm4` の
  compositor が有効。無効化されている場合は
  `xfconf-query -c xfwm4 -p /general/use_compositing -s true`）。
- `wmctrl` / `xdotool` が未インストールなら
  `sudo apt-get install -y wmctrl xdotool`（`kai-services/streaming/vm/setup.sh`
  では `xdotool` は導入済み、`wmctrl` は未導入なら追加する）。

### 運用

```bash
# 起動
DISPLAY=:0 bash show-overlay.sh

# ログ
cat /tmp/kai-overlay-chromium.log

# 停止（show-overlay.sh 実行時に表示された PID を使う）
kill <pid>

# ウィンドウ一覧・手動での最前面固定
wmctrl -l
wmctrl -r <window> -b add,above
```

## 手動検証

speechd が起動していれば、ブラウザで直接 `index.html` を開いて字幕が
反映されることを確認できる（VM でなくても、`speechd.py` をローカルで
起動して `TTS_URL` をダミーサーバーに向ければ検証可能）。

```bash
# 1. speechd を起動（別ターミナル。kai-services/speechd/README.md 参照）
cd kai-services/speechd && python3 speechd.py

# 2. ブラウザで overlay を開く（file:// で直接開いてよい）
open kai-services/overlay/index.html   # macOS の場合
# または: xdg-open kai-services/overlay/index.html （Linux）

# 3. ブラウザの開発者コンソールに [kai-overlay] connecting / connected が出ることを確認

# 4. 発話を1件送って、字幕が表示 → フェードアウトすることを確認
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"オーバーレイの検証です"}'

# 5. speechd を再起動しても、EventSource が自動再接続し
#    再びイベントを受け取れることを確認（コンソールに reconnect のログが出る）
```

## 制約

- コアファイル（hermes 本体）には触れていない。`kai-services/overlay/` 配下のみ。
- 外部 CDN 等への依存なし（フォントは VM にインストール済みの
  `fonts-noto-cjk` 等のシステムフォントを使う。`kai-services/streaming/vm/setup.sh`
  参照）。
- 秘匿情報は speechd 側で送出前マスク済みの前提（overlay 側では追加のマスク
  処理は行わない。加工せずそのまま表示する）。
