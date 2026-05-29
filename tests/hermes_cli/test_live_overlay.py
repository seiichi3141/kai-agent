import json
import time
import urllib.request

from hermes_cli.live_overlay import (
    LiveOverlayConfig,
    LiveOverlayServer,
    LiveOverlayState,
    load_live_overlay_config,
)


def test_live_overlay_config_is_shape_safe():
    cfg = load_live_overlay_config(
        {
            "live_overlay": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": "9001",
                "caption": {
                    "max_chars": "24",
                    "partial_ttl_seconds": "1.5",
                    "final_ttl_seconds": "9",
                },
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.port == 9001
    assert cfg.caption_max_chars == 24
    assert cfg.partial_ttl_seconds == 1.5
    assert cfg.final_ttl_seconds == 9.0


def test_live_overlay_state_cleans_truncates_and_expires_caption():
    state = LiveOverlayState(caption_max_chars=8)

    snapshot = state.publish_caption("  abc\n\r\tdef\x00ghi  ", final=False, ttl_seconds=0.1)

    assert snapshot["caption"]["text"] == "c defghi"
    assert snapshot["caption"]["kind"] == "partial"

    time.sleep(0.12)

    assert state.snapshot()["caption"]["text"] == ""


def test_live_overlay_http_serves_overlay_and_state():
    server = LiveOverlayServer(LiveOverlayConfig(enabled=True, port=0))
    server.start()
    try:
        server.publish_caption("ボス戦に入ります。", final=True)
        with urllib.request.urlopen(server.overlay_url, timeout=2) as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(server.overlay_url.replace("/overlay", "/state.json"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert "Hermes Live Overlay" in html
    assert state["caption"]["text"] == "ボス戦に入ります。"
    assert state["caption"]["kind"] == "final"
