#!/usr/bin/env python3
"""trace-shots — 操作ごとに OBS のスクショを撮ってトレースに紐づける。

kai_trace の当日 JSONL を tail し、`tool_call` イベントを見つけるたびに OBS の
スクリーンショット（既定はシーン全体）を撮って
`<HERMES_HOME>/kai_trace/shots/<session_id>/<行番号>.jpg` に保存する。
trace-web はこの行番号 <n> で操作イベントと画像を対応づけて表示する。

- 読み取り + 画像保存のみ（トレース JSONL は書き換えない）
- OBS（obs-websocket）が起動している時だけ撮る。未起動・失敗は黙ってスキップ
  （best-effort。kai の実行に一切影響しない独立プロセス）
- obs-websocket の接続は kai-services/streaming/vm/obsws.py を再利用する

使い方（VM 上、OBS 起動中に）:
  python3 shot_daemon.py

環境変数:
  SHOT_SOURCE       撮る OBS ソース名（既定「シーン」= 配信画面全体）
  SHOT_WIDTH        保存幅 px（既定 960。高さは縦横比維持）
  SHOT_QUALITY      JPEG 品質 1-100（既定 70）
  SHOT_POLL_MS      トレース tail の間隔 ms（既定 700）
  HERMES_HOME       トレースの親（既定 ~/.hermes）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# obsws.py（接続 + リクエスト）を再利用する
_VM_DIR = Path(__file__).resolve().parent.parent / "streaming" / "vm"
sys.path.insert(0, str(_VM_DIR))
import obsws  # noqa: E402

SHOT_SOURCE = os.environ.get("SHOT_SOURCE", "シーン")
SHOT_WIDTH = int(os.environ.get("SHOT_WIDTH", "960"))
SHOT_QUALITY = int(os.environ.get("SHOT_QUALITY", "70"))
POLL_MS = int(os.environ.get("SHOT_POLL_MS", "700"))


def _hermes_home() -> Path:
    v = os.environ.get("HERMES_HOME", "").strip()
    return Path(v) if v else Path(os.path.expanduser("~/.hermes"))


TRACE_DIR = Path(os.environ.get("TRACE_DIR", str(_hermes_home() / "kai_trace")))
SHOTS_DIR = TRACE_DIR / "shots"


def _utc_date() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _request(ws, rtype, data=None):
    ws.send(json.dumps({"op": 6, "d": {"requestType": rtype, "requestId": "s",
                                       "requestData": data or {}}}))
    while True:
        m = json.loads(ws.recv())
        if m["op"] == 7 and m["d"]["requestId"] == "s":
            return m["d"]["requestStatus"], (m["d"].get("responseData") or {})


def _capture(ws, out: Path) -> bool:
    """OBS スクショを撮って out（.jpg）に保存。失敗は False。"""
    try:
        st, rd = _request(ws, "GetSourceScreenshot", {
            "sourceName": SHOT_SOURCE, "imageFormat": "jpg",
            "imageWidth": SHOT_WIDTH, "imageCompressionQuality": SHOT_QUALITY,
        })
        if not st.get("result"):
            return False
        data = rd.get("imageData") or ""
        if data.startswith("data:"):
            data = data.split(",", 1)[1]
        import base64
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(data))
        return True
    except Exception:
        return False


def _connect():
    try:
        return obsws.connect()
    except Exception:
        return None


def main() -> None:
    print(f"[trace-shots] source={SHOT_SOURCE!r} → {SHOTS_DIR} (poll {POLL_MS}ms)")
    ws = _connect()
    date = _utc_date()
    path = TRACE_DIR / f"{date}.jsonl"
    # 起動時点までの既存イベントはスキップする（過去の操作は今の画面と合わないため、
    # 撮るのは起動後に発生した新しい tool_call だけ）。
    pos = 0  # 読んだ行数（1 始まりの行番号 = pos）
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as f:
            pos = sum(1 for _ in f)
    while True:
        try:
            today = _utc_date()
            if today != date:  # 日付ロールオーバー
                date, pos = today, 0
                path = TRACE_DIR / f"{date}.jsonl"
            if path.is_file():
                with path.open(encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                if len(lines) < pos:  # truncate/rotate されたら先頭から
                    pos = 0
                for n in range(pos + 1, len(lines) + 1):
                    line = lines[n - 1].strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("kind") == "tool_call" and e.get("session_id"):
                        if ws is None:
                            ws = _connect()  # OBS が後から起動した場合に拾う
                        if ws is not None:
                            out = SHOTS_DIR / str(e["session_id"]) / f"{n}.jpg"
                            if not _capture(ws, out):
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                                ws = None  # 次回再接続
                pos = len(lines)
            time.sleep(POLL_MS / 1000)
        except KeyboardInterrupt:
            return
        except Exception:
            time.sleep(POLL_MS / 1000)


if __name__ == "__main__":
    main()
