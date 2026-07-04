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
- `style.css` — 字幕の見た目（白文字 + 黒縁取り、フェードイン/アウト）。
- `app.js` — `EventSource` で speechd の `/events` を購読し、`subtitle` イベントを
  字幕 DOM に反映する。外部依存なし（素の JS）。
- `overlay-window.py` — 上記ページを X11 デスクトップに**透過・枠なし・最前面・
  クリックスルー**で全画面表示する WebKitGTK ラッパー（表示の正典）。
- `show-overlay.sh` — 手動起動用ヘルパー（`overlay-window.py` を起動するだけ）。
- `kai-overlay.service` — systemd --user 常駐用 unit。

## 表示方式: WebKitGTK ラッパー（Chromium ではない理由）

当初は snap Chromium（`--enable-transparent-visuals --disable-gpu`）で透過表示する
設計だったが、**実機（Ubuntu 24.04 / GNOME / X11 / snap Chromium）では ARGB
ウィンドウの背景が白く塗られ、透過が効かなかった**（`--default-background-color=00000000`
や GPU 有効/無効の組み合わせも不成立。ウィンドウ自体は depth 32 の ARGB visual を
得ているが、Chromium がベースレイヤーを不透明に描画してしまう）。

`overlay-window.py`（WebKitGTK）は GTK の RGBA visual に素直に従い、以下がすべて
GTK API で確実に実現できるため、こちらを表示の正典とした:

- **背景透過** — RGBA visual + `WebView.set_background_color(alpha=0)`
- **枠なし** — `set_decorated(False)`
- **最前面** — `set_keep_above(True)`（wmctrl 不要）
- **クリックスルー** — 入力シェイプを空にする（`input_shape_combine_region`）。
  字幕は映像に映るが、マウス操作は下のデスクトップにそのまま抜ける。
  kai が自分のデスクトップで作業する際にオーバーレイが邪魔にならない

依存（`kai-services/streaming/vm/setup.sh` で導入済み）:

```bash
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

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

`EventSource` は標準の挙動として接続断からの自動再接続を行う
（speechd 再起動時などもオーバーレイ側の操作は不要）。`onerror` はログ
出力のみ行い、処理は止めない。

ページは `file://` で開くため、speechd 側は `/events` に
`Access-Control-Allow-Origin: *` を返す（これがないとブラウザエンジンが
CORS で購読をブロックする — 実機で確認済み）。WebKit 側でも
`allow-universal-access-from-file-urls` を有効にしている。

SSE エンドポイントは既定で `http://127.0.0.1:8900/events`
（`kai-services/speechd/speechd.py` の `SPEECHD_PORT` 既定値）を見る。
別ホスト・別ポートの speechd を見たい場合は `?sse=` クエリパラメータで
上書きできる（`OVERLAY_URL` 環境変数で URL ごと差し替え可能）:

```bash
OVERLAY_URL="file:///home/kai/kai-agent/kai-services/overlay/index.html?sse=http://127.0.0.1:9900/events" \
  DISPLAY=:0 python3 overlay-window.py
```

## 見た目

- 背景完全透過（`html, body { background: transparent; }` + WebView の透明背景）。
  デスクトップに透過ウィンドウで重ねてそのまま配信画面（OBS の XSHM 画面
  キャプチャ）に取り込む前提。OBS 側の追加設定は不要。
- 画面下部中央、白文字・太字・黒縁取り（`-webkit-text-stroke` +
  `text-shadow` の多重指定）、`Noto Sans CJK JP` 相当のフォント、48px。
- 表示/非表示は `opacity` の `transition`（200ms）でフェードする。
- 長文は `max-width: 1600px` で中央寄せしつつ折り返す。

## 常駐化（systemd --user）

```bash
install -D -m 644 ~/kai-agent/kai-services/overlay/kai-overlay.service \
  ~/.config/systemd/user/kai-overlay.service
systemctl --user daemon-reload
systemctl --user enable --now kai-overlay.service
```

運用:

```bash
systemctl --user status kai-overlay.service
journalctl --user -u kai-overlay.service -f
systemctl --user restart kai-overlay.service
```

手動起動（検証用）:

```bash
DISPLAY=:0 bash show-overlay.sh
```

## 手動検証

VM 上（speechd 稼働中）で:

```bash
# 1. オーバーレイを表示
DISPLAY=:0 bash show-overlay.sh

# 2. 発話を1件送って、字幕が表示 → フェードアウトすることを確認
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"オーバーレイの検証です"}'

# 3. スクリーンショットで確認（配信に乗る見た目と同じ）
DISPLAY=:0 scrot /tmp/overlay-check.png

# 4. クリックスルー確認: オーバーレイ越しに画面中央をクリックしても
#    下のウィンドウがアクティブになること
DISPLAY=:0 xdotool mousemove 960 540 click 1
DISPLAY=:0 xdotool getactivewindow getwindowname   # => overlay 以外の名前

# 5. speechd を再起動しても EventSource が自動再接続することを確認
systemctl --user restart speechd.service
curl -s -X POST http://127.0.0.1:8900/say -d '{"text":"再接続の検証です"}'
```

実機検証済み（2026-07-04、kai-vm）: 透過・枠なし・最前面・クリックスルー・
字幕表示/クリア・OBS 画面キャプチャへの映り込みをスクリーンショットで確認。

## 制約

- コアファイル（hermes 本体）には触れていない。`kai-services/overlay/` 配下のみ。
- 外部 CDN 等への依存なし（フォントは VM にインストール済みの
  `fonts-noto-cjk` 等のシステムフォントを使う）。
- 秘匿情報は speechd 側で送出前マスク済みの前提（overlay 側では追加のマスク
  処理は行わない。加工せずそのまま表示する）。
- VM は GPU なし（llvmpipe）のため WebKit のハードウェアアクセラレーションは
  明示的に無効化している（`HardwareAccelerationPolicy.NEVER`）。字幕程度の
  描画負荷では問題ない。
