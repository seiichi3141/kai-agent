#!/usr/bin/env python3
"""最小 obs-websocket v5 クライアント（kai 用ユーティリティ）。

使い方:
  obsws.py <requestType> ['<requestData JSON>']   # 単発リクエスト
  obsws.py --batch <file|->                       # JSON 配列を順に実行
  obsws.py --check-auth                            # ws サーバの認証設定を検査（#77 M-c）

いずれも結果を JSON Lines で出力する。いずれかのリクエストが失敗
（requestStatus.result が false）なら exit 1（検証器から使うため）。
--check-auth は obs-websocket が有効かつ ServerPassword が非空なら exit 0、
そうでなければ exit 1（配信前 preflight でストリームキー流出リスクを弾く）。

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

# websocket（python3-websocket）は接続時のみ必要。--check-auth や
# server_password_ok は ws 接続しないので、遅延 import で未インストール環境
# （CI のユニットテスト等）でもこれらを使えるようにする。


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


def server_password_ok(global_ini_text):
    """global.ini の [OBSWebSocket] が有効かつ ServerPassword 非空か（#77 M-c）。

    obs-websocket は既定で全 IF 待受。パスワードが弱い/未設定だと tailnet/LAN から
    OBS を乗っ取り、GetStreamServiceSettings で RTMP キーを平文取得→配信ジャック
    できる。純関数（ini テキストを受ける）にしてテストと CLI 検査で共用する。
    """
    cp = configparser.ConfigParser()
    cp.read_string(global_ini_text)
    if not cp.has_section("OBSWebSocket"):
        return False, "global.ini に [OBSWebSocket] が無い（ws 無効の可能性）"
    sec = cp["OBSWebSocket"]
    if not sec.getboolean("ServerEnabled", fallback=False):
        return False, "ServerEnabled が false"
    if not sec.get("ServerPassword", "").strip():
        return False, "ServerPassword が空（認証なし）"
    return True, "OK"


def check_auth():
    """--check-auth: preflight 用。認証設定が安全なら 0、危険なら 1。"""
    ini = pathlib.Path.home() / ".config/obs-studio/global.ini"
    if not ini.exists():
        print("NG: global.ini が無い", file=sys.stderr)
        return 1
    ok, reason = server_password_ok(ini.read_text())
    print("OK: obs-websocket 認証あり" if ok else f"NG: {reason}",
          file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


def auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def connect():
    import websocket  # python3-websocket（接続時のみ必要）
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
    if sys.argv[1] == "--check-auth":
        return check_auth()
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
