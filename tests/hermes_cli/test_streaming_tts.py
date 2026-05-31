import asyncio
import sys
import types

import pytest


def test_load_fish_audio_streaming_tts_config(monkeypatch):
    from hermes_cli.streaming_tts import load_fish_audio_streaming_tts_config

    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-key")
    cfg = load_fish_audio_streaming_tts_config(
        {
            "tts": {
                "provider": "fish_audio",
                "fish_audio": {
                    "reference_id": "voice-1",
                    "model": "s2-pro",
                    "format": "opus",
                    "latency": "low",
                    "chunk_length": 120,
                    "stream_playback_drain_timeout_seconds": 240,
                },
            }
        }
    )

    assert cfg.api_key == "test-key"
    assert cfg.reference_id == "voice-1"
    assert cfg.model == "s2-pro"
    assert cfg.format == "opus"
    assert cfg.sample_rate == 48000
    assert cfg.latency == "low"
    assert cfg.chunk_length == 120
    assert cfg.playback_drain_timeout_seconds == 240


def test_start_event_contains_fish_audio_request():
    from hermes_cli.streaming_tts import FishAudioStreamingTTSConfig, _start_event

    event = _start_event(
        FishAudioStreamingTTSConfig(
            api_key="test-key",
            reference_id="voice-1",
            format="mp3",
            latency="balanced",
            chunk_length=200,
        )
    )

    assert event["event"] == "start"
    request = event["request"]
    assert request["text"] == ""
    assert request["reference_id"] == "voice-1"
    assert request["format"] == "mp3"
    assert request["latency"] == "balanced"
    assert request["mp3_bitrate"] == 128


@pytest.mark.asyncio
async def test_stream_fish_audio_tts_with_fake_websocket(monkeypatch):
    import msgpack

    from hermes_cli.streaming_tts import (
        FishAudioStreamingTTSConfig,
        stream_fish_audio_tts,
    )

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.messages = [
                msgpack.packb({"event": "audio", "audio": b"abc"}, use_bin_type=True),
                msgpack.packb({"event": "audio", "audio": b"def"}, use_bin_type=True),
                msgpack.packb({"event": "finish", "reason": "stop"}, use_bin_type=True),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, message):
            self.sent.append(msgpack.unpackb(message, raw=False))

        async def recv(self):
            await asyncio.sleep(0)
            return self.messages.pop(0)

    fake_ws = FakeWebSocket()

    def fake_connect(*args, **kwargs):
        assert kwargs["additional_headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["additional_headers"]["model"] == "s2-pro"
        return fake_ws

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        types.SimpleNamespace(connect=fake_connect),
    )

    chunks = []
    result = await stream_fish_audio_tts(
        ["Hello, ", "world."],
        config=FishAudioStreamingTTSConfig(
            api_key="test-key",
            reference_id="voice-1",
            model="s2-pro",
            timeout_seconds=1,
        ),
        on_audio_chunk=chunks.append,
    )

    assert chunks == [b"abc", b"def"]
    assert result.audio_bytes == 6
    assert result.chunks == 2
    assert result.first_audio_ms is not None
    assert result.finish_reason == "stop"
    assert fake_ws.sent[0]["event"] == "start"
    assert fake_ws.sent[1] == {"event": "text", "text": "Hello, "}
    assert fake_ws.sent[2] == {"event": "text", "text": "world."}
    assert fake_ws.sent[3] == {"event": "flush"}
    assert fake_ws.sent[4] == {"event": "stop"}
