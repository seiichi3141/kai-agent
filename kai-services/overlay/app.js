// kai 配信オーバーレイ: speechd の SSE (`GET /events`) を購読して字幕を表示する。
// 設計: docs/kai/design/00-system.md §4。詳細は kai-services/overlay/README.md。
//
// 外部依存なし（素の JS・ブラウザ標準の EventSource のみ）。
// EventSource は仕様上、接続が切れると自動で再接続する（onerror はログ用）。
// SSE の `:` から始まる行（keep-alive コメント）は EventSource が自動で無視する。

(() => {
  "use strict";

  // SSE エンドポイントは既定で speechd のローカルポートを見る。
  // `?sse=http://host:port/events` クエリで上書き可能（別ホストの speechd を
  // 見に行きたい場合や、手元での動作確認に使う）。
  const DEFAULT_SSE_URL = "http://127.0.0.1:8900/events";
  const params = new URLSearchParams(window.location.search);
  const SSE_URL = params.get("sse") || DEFAULT_SSE_URL;

  const subtitleEl = document.getElementById("subtitle");

  function log(...args) {
    // eslint-disable-next-line no-console
    console.log("[kai-overlay]", ...args);
  }

  function warn(...args) {
    // eslint-disable-next-line no-console
    console.warn("[kai-overlay]", ...args);
  }

  function setSubtitle(text) {
    const value = typeof text === "string" ? text : "";
    if (value) {
      subtitleEl.textContent = value;
      subtitleEl.classList.add("visible");
    } else {
      // フェードアウトさせてから空にする（transition は CSS 側で定義）。
      subtitleEl.classList.remove("visible");
    }
  }

  // 将来の拡張ポイント: type ごとのハンドラ。今は "subtitle" のみ実装。
  // 未知の type は無視して落ちない（アバター・コメント等は後日ここに追加する）。
  const handlers = {
    subtitle(payload) {
      setSubtitle(payload.text);
    },
  };

  function handleEvent(raw) {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (err) {
      warn("SSE payload の JSON parse に失敗:", raw, err);
      return;
    }
    if (!payload || typeof payload.type !== "string") {
      warn("SSE payload に type がありません:", payload);
      return;
    }
    const handler = handlers[payload.type];
    if (!handler) {
      // 未知のイベント種別は無視するだけ（将来のアバター/コメント等）。
      return;
    }
    try {
      handler(payload);
    } catch (err) {
      warn(`イベントハンドラでエラー (type=${payload.type}):`, err);
    }
  }

  function connect() {
    log("connecting to", SSE_URL);
    const es = new EventSource(SSE_URL);

    es.onopen = () => {
      log("connected");
    };

    es.onmessage = (ev) => {
      handleEvent(ev.data);
    };

    es.onerror = (err) => {
      // EventSource はブラウザ標準の挙動で自動再接続する。ここではログのみ。
      warn("SSE error（自動再接続を待機）:", err);
    };
  }

  connect();
})();
