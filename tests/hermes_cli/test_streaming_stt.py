import json
import wave

from hermes_cli.streaming_stt import (
    DeepgramStreamingConfig,
    build_deepgram_listen_url,
    iter_wav_pcm_chunks,
    load_deepgram_streaming_config,
    parse_deepgram_message,
    transcript_event_to_json,
)
from hermes_cli.turn_detection import (
    TurnDetectionConfig,
    TurnDetectionSignals,
    classify_streaming_stt_turn,
)
from scripts.replay_voice_turns import replay_turns
from scripts.evaluate_voice_fixtures import _discover, _evaluate_one
from hermes_cli.turn_classifier import parse_turn_classifier_response


TURN_CFG = TurnDetectionConfig(min_chars=8, max_wait_ms=6000, turn_detection="rules")


def test_tui_voice_on_starts_always_on_streaming(monkeypatch):
    from tui_gateway import server

    started = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"streaming_stt": {"enabled": True, "provider": "deepgram", "always_on": True}},
    )
    monkeypatch.setattr(server, "_start_streaming_stt", lambda: started.append(True) or "recording")
    monkeypatch.setenv("HERMES_VOICE", "0")

    result = server._methods["voice.toggle"]("rid-1", {"action": "on", "session_id": "sid-1"})

    assert result["result"]["enabled"] is True
    assert result["result"]["streaming_always_on"] is True
    assert started == [True]
    assert server._voice_event_sid == "sid-1"


def test_tui_voice_on_starts_live_overlay_when_configured(monkeypatch):
    from tui_gateway import server

    started = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {"enabled": False},
            "live_overlay": {"enabled": True},
        },
    )
    monkeypatch.setattr(
        server,
        "_ensure_live_overlay_server",
        lambda: started.append(True) or "http://127.0.0.1:8765/overlay",
    )
    monkeypatch.setenv("HERMES_VOICE", "0")

    result = server._methods["voice.toggle"]("rid-1", {"action": "on", "session_id": "sid-1"})

    assert result["result"]["enabled"] is True
    assert result["result"]["overlay_url"] == "http://127.0.0.1:8765/overlay"
    assert started == [True]


def test_tui_streaming_stt_baseline_buffers_until_debounce_flush(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {"debounce_ms": 999999, "min_chars": 8, "joiner": " "},
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("私の話していることが")
    server._queue_streaming_stt_final("何か")
    server._queue_streaming_stt_final("ボイスで", speech_final=True)

    assert emitted == []

    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert emitted == [
        (
            "voice.transcript",
            {"text": "私の話していることが 何か ボイスで"},
        )
    ]
    server._cancel_streaming_stt_submit_buffer(flush=False)


def test_tui_streaming_stt_short_final_is_not_submitted(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {"debounce_ms": 999999, "min_chars": 8, "joiner": " "},
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("何か")
    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert emitted == []


def test_tui_streaming_stt_baseline_does_not_parse_incomplete_ending(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {
                    "debounce_ms": 999999,
                    "min_chars": 8,
                    "joiner": " ",
                    "max_wait_ms": 6000,
                    "turn_detection": "rules",
                },
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("私の話していることが", speech_final=True)
    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert emitted == [
        (
            "voice.transcript",
            {"text": "私の話していることが"},
        )
    ]
    server._cancel_streaming_stt_submit_buffer(flush=False)


def test_tui_streaming_stt_submits_after_incomplete_then_completion(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {
                    "debounce_ms": 999999,
                    "min_chars": 8,
                    "joiner": " ",
                    "max_wait_ms": 6000,
                    "turn_detection": "rules",
                },
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("私の話していることが")
    server._queue_streaming_stt_final("最後まで届くか見ています。", speech_final=True)
    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert emitted == [
        (
            "voice.transcript",
            {"text": "私の話していることが 最後まで届くか見ています。"},
        )
    ]


def test_tui_streaming_stt_commit_delay_holds_submit_until_commit(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {
                    "debounce_ms": 999999,
                    "commit_delay_ms": 1000,
                    "min_chars": 8,
                    "joiner": " ",
                    "max_wait_ms": 6000,
                },
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("こんにちは、テストしています。", speech_final=True)
    server._flush_streaming_stt_submit_buffer()

    assert emitted == []

    server._commit_pending_streaming_stt_submit()

    assert emitted == [
        (
            "voice.transcript",
            {"text": "こんにちは、テストしています。"},
        )
    ]
    server._cancel_streaming_stt_submit_buffer(flush=False)


def test_tui_streaming_stt_commit_delay_rebuffers_when_speech_continues(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {
                    "debounce_ms": 999999,
                    "commit_delay_ms": 1000,
                    "min_chars": 8,
                    "joiner": " ",
                    "max_wait_ms": 6000,
                },
            }
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("次の行動は右に行くべきか。")
    server._flush_streaming_stt_submit_buffer()
    server._handle_streaming_stt_partial("まだ答えないで")
    server._queue_streaming_stt_final("まだ答えないで。今なら答えてください。", speech_final=True)
    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert emitted == [
        ("voice.partial_transcript", {"text": "まだ答えないで"}),
        (
            "voice.transcript",
            {"text": "次の行動は右に行くべきか。 まだ答えないで。今なら答えてください。"},
        ),
    ]
    server._cancel_streaming_stt_submit_buffer(flush=False)


def test_tui_streaming_stt_publishes_partial_and_final_overlay_captions(monkeypatch):
    from tui_gateway import server

    emitted = []
    captions = []
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "streaming_stt": {
                "enabled": True,
                "provider": "deepgram",
                "submit": {
                    "debounce_ms": 999999,
                    "commit_delay_ms": 0,
                    "min_chars": 8,
                    "joiner": " ",
                    "max_wait_ms": 6000,
                },
            },
            "live_overlay": {"enabled": True},
        },
    )
    monkeypatch.setattr(server, "_voice_emit", lambda event, payload=None: emitted.append((event, payload)))
    monkeypatch.setattr(
        server,
        "_publish_live_overlay_caption",
        lambda text, *, final: captions.append((text, final)),
    )
    server._cancel_streaming_stt_submit_buffer(flush=False)

    server._queue_streaming_stt_final("ボス戦に入ります。", speech_final=True)
    server._handle_streaming_stt_partial("ボス戦に入ります")
    server._cancel_streaming_stt_submit_buffer(flush=True)

    assert captions == [
        ("ボス戦に入ります", False),
        ("ボス戦に入ります。", True),
    ]
    assert emitted == [
        ("voice.partial_transcript", {"text": "ボス戦に入ります"}),
        ("voice.transcript", {"text": "ボス戦に入ります。"}),
    ]
    server._cancel_streaming_stt_submit_buffer(flush=False)


def test_tui_streaming_stt_baseline_does_not_parse_explicit_hold(monkeypatch):
    from tui_gateway import server

    cfg = {
        "debounce_ms": 1800,
        "min_chars": 8,
        "joiner": " ",
        "max_wait_ms": 6000,
        "turn_detection": "rules",
    }
    decision, reason = server._classify_streaming_stt_turn(
        "最後まで反応しないでほしい",
        elapsed_ms=500,
        submit_cfg=cfg,
    )

    assert decision == "wait"
    assert reason == "awaiting_speech_final"


def test_tui_streaming_stt_baseline_does_not_parse_explicit_release(monkeypatch):
    from tui_gateway import server

    cfg = {
        "debounce_ms": 1800,
        "min_chars": 8,
        "joiner": " ",
        "max_wait_ms": 6000,
        "turn_detection": "rules",
    }
    decision, reason = server._classify_streaming_stt_turn(
        "ここまでの内容に答えて",
        elapsed_ms=500,
        submit_cfg=cfg,
    )

    assert decision == "wait"
    assert reason == "awaiting_speech_final"


def test_turn_detection_baseline_does_not_match_mid_clause_text():
    decision, reason = classify_streaming_stt_turn(
        "会話は配信に乗る前提なので、ご",
        elapsed_ms=1800,
        config=TURN_CFG,
        signals=TurnDetectionSignals(speech_final=True),
    )

    assert decision == "submit"
    assert reason == "speech_final"


def test_turn_detection_baseline_does_not_match_pause_punctuation():
    decision, reason = classify_streaming_stt_turn(
        "話をしてるんだけど、",
        elapsed_ms=1800,
        config=TURN_CFG,
        signals=TurnDetectionSignals(speech_final=True),
    )

    assert decision == "submit"
    assert reason == "speech_final"


def test_turn_detection_baseline_does_not_match_sentence_ending():
    decision, reason = classify_streaming_stt_turn(
        "私が今話していることが",
        elapsed_ms=1800,
        config=TURN_CFG,
        signals=TurnDetectionSignals(speech_final=True),
    )

    assert decision == "submit"
    assert reason == "speech_final"


def test_turn_detection_submits_basic_conversation_complete_sentence():
    decision, reason = classify_streaming_stt_turn(
        "この会話は配信に乗る前提なので、返答は一文か二文でお願いします。",
        elapsed_ms=1800,
        config=TURN_CFG,
        signals=TurnDetectionSignals(speech_final=True),
    )

    assert decision == "submit"
    assert reason == "speech_final"


def test_turn_detection_baseline_does_not_match_speculative_cancellation_phrase():
    decision, reason = classify_streaming_stt_turn(
        "次の行動は右に行くべきか いや、まだ答えないで。",
        elapsed_ms=1800,
        config=TURN_CFG,
        signals=TurnDetectionSignals(speech_final=True),
    )

    assert decision == "submit"
    assert reason == "speech_final"


def test_build_deepgram_listen_url_includes_streaming_options():
    url = build_deepgram_listen_url(
        DeepgramStreamingConfig(
            api_key="dg-test",
            model="nova-3",
            language="ja",
            sample_rate=16000,
            endpointing=250,
        )
    )

    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in url
    assert "language=ja" in url
    assert "encoding=linear16" in url
    assert "sample_rate=16000" in url
    assert "interim_results=true" in url
    assert "endpointing=250" in url


def test_iter_wav_pcm_chunks_reads_matching_pcm16_wav(tmp_path):
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)

    chunks = list(iter_wav_pcm_chunks(wav_path, sample_rate=16000, channels=1, chunk_ms=100))

    assert chunks == [b"\x00\x00" * 1600]


def test_iter_wav_pcm_chunks_rejects_wrong_sample_rate(tmp_path):
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x00\x00" * 4410)

    try:
        list(iter_wav_pcm_chunks(wav_path, sample_rate=16000, channels=1, chunk_ms=100))
    except ValueError as exc:
        assert "expected 16000 Hz" in str(exc)
    else:
        raise AssertionError("expected sample-rate mismatch to fail")


def test_transcript_event_to_json_is_stable():
    event = parse_deepgram_message(
        json.dumps(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": True,
                "channel": {"alternatives": [{"transcript": "こんにちは"}]},
            }
        )
    )

    assert event is not None
    assert transcript_event_to_json(event) == {
        "text": "こんにちは",
        "is_final": True,
        "speech_final": True,
    }


def test_replay_voice_turns_groups_final_events_by_speech_final():
    events = [
        {"text": "こんにちは", "is_final": True, "speech_final": True, "received_at_ms": 1000},
        {"text": "今日は音声入力のテストです。", "is_final": True, "speech_final": True, "received_at_ms": 1500},
        {"text": "ゲーム実況", "is_final": True, "speech_final": False, "received_at_ms": 5000},
        {
            "text": "用のアシスタントとして、短く返事してください。",
            "is_final": True,
            "speech_final": True,
            "received_at_ms": 5600,
        },
    ]

    assert replay_turns(events, min_chars=8, max_wait_ms=6000, debounce_ms=1800) == [
        "こんにちは 今日は音声入力のテストです。",
        "ゲーム実況 用のアシスタントとして、短く返事してください。"
    ]


def test_replay_voice_turns_partial_activity_extends_debounce():
    events = [
        {"text": "私が今話していることが", "is_final": True, "speech_final": False, "received_at_ms": 1000},
        {"text": "少し長くなると思うんですけれども", "is_final": True, "speech_final": True, "received_at_ms": 2000},
        {"text": "最後まで聞い", "is_final": False, "speech_final": False, "received_at_ms": 3200},
        {"text": "最後まで聞いてから返事して", "is_final": False, "speech_final": False, "received_at_ms": 4300},
        {"text": "最後まで聞いてから返事してほしいです。", "is_final": True, "speech_final": True, "received_at_ms": 5200},
    ]

    assert replay_turns(events, min_chars=8, max_wait_ms=6000, debounce_ms=1800) == [
        "私が今話していることが 少し長くなると思うんですけれども 最後まで聞いてから返事してほしいです。"
    ]


def test_replay_voice_turns_uses_classifier_when_enabled():
    events = [
        {"text": "私が今話していることが", "is_final": True, "speech_final": True, "received_at_ms": 1000},
        {"text": "続きです。", "is_final": True, "speech_final": True, "received_at_ms": 4000},
    ]

    def fake_classifier(config, item):
        from hermes_cli.turn_classifier import TurnClassifierResult

        if item.text == "私が今話していることが":
            return TurnClassifierResult(action="wait", reason="continuing")
        return TurnClassifierResult(action="submit", reason="done")

    from hermes_cli.turn_classifier import TurnClassifierConfig

    assert replay_turns(
        events,
        min_chars=8,
        max_wait_ms=6000,
        debounce_ms=1800,
        classifier_config=TurnClassifierConfig(enabled=True),
        classifier=fake_classifier,
    ) == ["私が今話していることが 続きです。"]


def test_replay_voice_turns_commit_delay_cancels_pending_turn_on_new_speech():
    events = [
        {"text": "次の行動は右に行くべきか。", "is_final": True, "speech_final": False, "received_at_ms": 1000},
        {"text": "、まだ答えないで", "is_final": False, "speech_final": False, "received_at_ms": 3000},
        {"text": "、まだ答えないで。", "is_final": True, "speech_final": False, "received_at_ms": 3500},
        {"text": "今なら答えてください。", "is_final": True, "speech_final": True, "received_at_ms": 4200},
    ]

    assert replay_turns(events, min_chars=8, max_wait_ms=6000, debounce_ms=1800, commit_delay_ms=1000) == [
        "次の行動は右に行くべきか。 、まだ答えないで。 今なら答えてください。"
    ]


def test_replay_voice_turns_commit_delay_preserves_separate_turns_after_window():
    events = [
        {"text": "こんにちは、テストです。", "is_final": True, "speech_final": True, "received_at_ms": 1000},
        {"text": "次の話題", "is_final": False, "speech_final": False, "received_at_ms": 3000},
        {"text": "次の話題について話します。", "is_final": True, "speech_final": True, "received_at_ms": 4500},
    ]

    assert replay_turns(events, min_chars=8, max_wait_ms=6000, debounce_ms=1800, commit_delay_ms=1000) == [
        "こんにちは、テストです。",
        "次の話題について話します。",
    ]


def test_evaluate_voice_fixtures_discovers_local_jsonl(tmp_path):
    jsonl = tmp_path / "sample.deepgram_events.jsonl"
    jsonl.write_text(
        json.dumps(
            {"text": "テストしています。", "is_final": True, "speech_final": True, "received_at_ms": 1000},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert _discover(tmp_path) == [jsonl]


def test_evaluate_voice_fixtures_compares_expected(tmp_path):
    jsonl = tmp_path / "sample.deepgram_events.jsonl"
    jsonl.write_text(
        json.dumps(
            {"text": "テストしています。", "is_final": True, "speech_final": True, "received_at_ms": 1000},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    expected = tmp_path / "sample.expected_turns.json"
    expected.write_text(
        json.dumps({"expected_user_messages": ["テストしています。"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    class Args:
        classifier = False
        classifier_base_url = "http://example.invalid/v1"
        classifier_model = "dummy"
        classifier_timeout_ms = 1
        min_chars = 8
        max_wait_ms = 6000
        debounce_ms = 1800
        llm_wait_debounce_ms = 3000
        commit_delay_ms = 0

    result = _evaluate_one(Args(), jsonl)

    assert result.passed is True
    assert result.turns == ["テストしています。"]
    assert result.expected == ["テストしています。"]


def test_parse_turn_classifier_response_accepts_fenced_json():
    result = parse_turn_classifier_response(
        '```json\n{"action":"wait","reason":"speaker is continuing"}\n```'
    )

    assert result is not None
    assert result.action == "wait"
    assert result.reason == "speaker is continuing"


def test_parse_turn_classifier_response_accepts_classification_alias():
    result = parse_turn_classifier_response('{"classification":"submit"}')

    assert result is not None
    assert result.action == "submit"


def test_parse_deepgram_message_returns_partial_event():
    event = parse_deepgram_message(
        json.dumps(
            {
                "type": "Results",
                "is_final": False,
                "speech_final": False,
                "channel": {"alternatives": [{"transcript": "こんにちは"}]},
            }
        )
    )

    assert event is not None
    assert event.text == "こんにちは"
    assert event.is_final is False
    assert event.speech_final is False


def test_parse_deepgram_message_returns_final_event():
    event = parse_deepgram_message(
        json.dumps(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": True,
                "channel": {"alternatives": [{"transcript": "ボス戦に入ります"}]},
            }
        )
    )

    assert event is not None
    assert event.text == "ボス戦に入ります"
    assert event.is_final is True
    assert event.speech_final is True


def test_parse_deepgram_message_ignores_empty_non_result_payloads():
    assert parse_deepgram_message('{"type":"Metadata"}') is None
    assert parse_deepgram_message("not json") is None
    assert (
        parse_deepgram_message(
            json.dumps(
                {
                    "type": "Results",
                    "is_final": False,
                    "channel": {"alternatives": [{"transcript": ""}]},
                }
            )
        )
        is None
    )


def test_load_deepgram_streaming_config_is_shape_safe(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env")

    cfg = load_deepgram_streaming_config(
        {
            "streaming_stt": {
                "deepgram": {
                    "model": "nova-3",
                    "language": "ja",
                    "sample_rate": "48000",
                    "interim_results": "true",
                    "smart_format": "false",
                    "endpointing": "300",
                }
            }
        }
    )

    assert cfg.api_key == "from-env"
    assert cfg.sample_rate == 48000
    assert cfg.interim_results is True
    assert cfg.smart_format is False
    assert cfg.endpointing == 300
