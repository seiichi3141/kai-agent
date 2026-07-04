#!/usr/bin/env python3
"""最小 obs-websocket v5 クライアント（kai 用ユーティリティ）。

使い方:
  obsws.py <requestType> ['<requestData JSON>']   # 単発リクエスト
  obsws.py --batch <file|->                       # JSON 配列を順に実行

いずれも結果を JSON Lines で出力する。いずれかのリクエストが失敗
（requestStatus.result が false）なら exit 1（検証器から使うため）。

接続情報は OBS の設定から読む。ポート/パスワードの正典は global.ini の
[OBSWebSocket]（OBS 30 の実挙動）。旧 plugin_config/obs-websocket/config.json
はフォールバックとして残す。
"""
import base64
import configparser
import hashlib
import json
import pathlib
import sys

import websocket  # python3-websocket


def load_config():
    """(port, password) を global.ini 優先で返す。"""
    base = pathlib.Path.home() / ".config/obs-studio"
    ini = base / "global.ini"
    if ini.exists():
        cp = configparser.ConfigParser()
        cp.read(ini)
        if cp.has_section("OBSWebSocket"):
            sec = cp["OBSWebSocket"]
            port = sec.getint("ServerPort", 4455)
            password = sec.get("ServerPassword", "")
            if password:
                return port, password
    legacy = base / "plugin_config/obs-websocket/config.json"
    cfg = json.loads(legacy.read_text())
    return cfg["server_port"], cfg["server_password"]


def auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def connect():
    port, password = load_config()
    ws = websocket.create_connection(f"ws://127.0.0.1:{port}", timeout=10)
    hello = json.loads(ws.recv())
    ident = {"op": 1, "d": {"rpcVersion": 1}}
    if "authentication" in hello["d"]:
        a = hello["d"]["authentication"]
        ident["d"]["authentication"] = auth_string(password, a["salt"], a["challenge"])
    ws.send(json.dumps(ident))
    identified = json.loads(ws.recv())
    assert identified["op"] == 2, identified
    return ws


def run_requests(ws, reqs):
    ok = True
    for i, req in enumerate(reqs):
        rid = f"req-{i}"
        ws.send(json.dumps({"op": 6, "d": {"requestType": req["requestType"], "requestId": rid,
                                           "requestData": req.get("requestData", {})}}))
        while True:
            msg = json.loads(ws.recv())
            if msg["op"] == 7 and msg["d"]["requestId"] == rid:
                status = msg["d"]["requestStatus"]
                print(json.dumps({"requestType": req["requestType"],
                                  "status": status,
                                  "responseData": msg["d"].get("responseData")},
                                 ensure_ascii=False))
                ok = ok and status.get("result", False)
                break
    return ok


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    if sys.argv[1] == "--batch":
        src = sys.stdin if sys.argv[2] == "-" else open(sys.argv[2])
        reqs = json.load(src)
    else:
        data = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        reqs = [{"requestType": sys.argv[1], "requestData": data}]
    ws = connect()
    try:
        ok = run_requests(ws, reqs)
    finally:
        ws.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
