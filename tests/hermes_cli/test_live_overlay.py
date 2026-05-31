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
    assert snapshot["captions"]["host"]["text"] == "c defghi"
    assert snapshot["caption"]["kind"] == "partial"

    time.sleep(0.12)

    assert state.snapshot()["captions"]["host"]["text"] == ""


def test_live_overlay_state_keeps_host_and_assistant_lanes_separate():
    state = LiveOverlayState(caption_max_chars=80)

    snapshot = state.publish_caption("実況者の字幕", final=False, ttl_seconds=3, speaker="host")
    snapshot = state.publish_caption("AIの字幕", final=True, ttl_seconds=3, speaker="assistant")

    assert snapshot["captions"]["host"]["text"] == "実況者の字幕"
    assert snapshot["captions"]["host"]["speaker"] == "host"
    assert snapshot["captions"]["assistant"]["text"] == "AIの字幕"
    assert snapshot["captions"]["assistant"]["speaker"] == "assistant"


def test_live_overlay_server_publish_caption_accepts_ttl_override():
    config = LiveOverlayConfig(enabled=True, final_ttl_seconds=8)
    server = LiveOverlayServer(config)

    before = time.time()
    snapshot = server.publish_caption(
        "長いアシスタント字幕",
        final=True,
        speaker="assistant",
        ttl_seconds=120,
    )

    expires_at = snapshot["captions"]["assistant"]["expires_at"]
    assert expires_at >= before + 119


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
    assert state["captions"]["host"]["text"] == "ボス戦に入ります。"
    assert state["captions"]["host"]["kind"] == "final"
