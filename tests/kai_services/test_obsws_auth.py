"""Issue #77 M-c: obs-websocket の認証設定検査（server_password_ok）の回帰。

未認証/弱設定の obs-websocket は tailnet/LAN から RTMP キー平文取得→配信ジャックの
経路になる。preflight の --check-auth が使う純関数を検証する。
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "obsws_test", REPO_ROOT / "kai-services" / "streaming" / "vm" / "obsws.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_password_set_and_enabled_is_ok():
    mod = _load()
    ok, _ = mod.server_password_ok(
        "[OBSWebSocket]\nServerEnabled=true\nServerPort=4455\nServerPassword=s3cr3tpass\n")
    assert ok


def test_empty_password_is_rejected():
    mod = _load()
    ok, reason = mod.server_password_ok(
        "[OBSWebSocket]\nServerEnabled=true\nServerPassword=\n")
    assert not ok
    assert "ServerPassword" in reason


def test_disabled_server_is_rejected():
    mod = _load()
    ok, reason = mod.server_password_ok(
        "[OBSWebSocket]\nServerEnabled=false\nServerPassword=s3cr3tpass\n")
    assert not ok
    assert "ServerEnabled" in reason


def test_missing_section_is_rejected():
    mod = _load()
    ok, reason = mod.server_password_ok("[General]\nName=kai\n")
    assert not ok
    assert "OBSWebSocket" in reason
