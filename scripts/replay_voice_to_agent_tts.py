#!/usr/bin/env python3
"""Replay a recording through STT -> Hermes LLM -> Fish Audio streaming TTS.

This is a manual end-to-end probe for the live voice pipeline. It converts the
input recording to Deepgram-compatible PCM WAV when needed, streams it to
Deepgram, sends the recognized text to Hermes, and streams assistant deltas
into Fish Audio WebSocket TTS while saving the audio chunks to a file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.streaming_stt import (  # noqa: E402
    _default_ssl_context,
    build_deepgram_listen_url,
    iter_wav_pcm_chunks,
    load_deepgram_streaming_config,
    parse_deepgram_message,
)
from hermes_cli.streaming_tts import (  # noqa: E402
    FishAudioStreamingTTSResult,
    stream_fish_audio_tts,
)


def _convert_to_wav(input_path: Path, sample_rate: int, channels: int) -> Path:
    if input_path.suffix.lower() == ".wav":
        return input_path
    out = Path(tempfile.gettempdir()) / f"hermes_voice_replay_{int(time.time() * 1000)}.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-sample_fmt",
        "s16",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


async def _transcribe_recording(path: Path, *, realtime: bool) -> tuple[str, dict[str, Any]]:
    import websockets

    config = load_deepgram_streaming_config()
    if not config.api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is required")

    wav_path = _convert_to_wav(path, config.sample_rate, config.channels)
    chunks = list(
        iter_wav_pcm_chunks(
            wav_path,
            sample_rate=config.sample_rate,
            channels=config.channels,
            chunk_ms=config.chunk_ms,
        )
    )
    uri = build_deepgram_listen_url(config)
    headers = {"Authorization": f"Token {config.api_key}"}
    finals: list[str] = []
    partials = 0
    started_at = time.monotonic()
    first_partial_ms: int | None = None
    first_final_ms: int | None = None

    async with websockets.connect(
        uri,
        additional_headers=headers,
        ssl=_default_ssl_context(),
    ) as ws:

        async def send_audio() -> None:
            delay = config.chunk_ms / 1000
            for chunk in chunks:
                await ws.send(chunk)
                if realtime:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "CloseStream"}))

        async def receive_events() -> None:
            nonlocal partials, first_partial_ms, first_final_ms
            async for message in ws:
                event = parse_deepgram_message(message)
                if event is None:
                    continue
                now_ms = int((time.monotonic() - started_at) * 1000)
                if event.is_final:
                    first_final_ms = first_final_ms if first_final_ms is not None else now_ms
                    if event.text.strip():
                        finals.append(event.text.strip())
                    print(f"[stt final speech_final={event.speech_final}] {event.text}", flush=True)
                else:
                    partials += 1
                    first_partial_ms = first_partial_ms if first_partial_ms is not None else now_ms
                    print(f"[stt partial] {event.text}", flush=True)

        sender = asyncio.create_task(send_audio())
        receiver = asyncio.create_task(receive_events())
        await sender
        try:
            await asyncio.wait_for(receiver, timeout=10)
        except asyncio.TimeoutError:
            receiver.cancel()

    transcript = " ".join(text for text in finals if text).strip()
    return transcript, {
        "wav_path": str(wav_path),
        "chunks": len(chunks),
        "partials": partials,
        "finals": len(finals),
        "first_partial_ms": first_partial_ms,
        "first_final_ms": first_final_ms,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


class _StreamingTTSFileWorker:
    def __init__(self, output_path: Path, *, timeout: float = 60.0) -> None:
        self.output_path = output_path
        self.timeout = timeout
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.result: FishAudioStreamingTTSResult | None = None
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def feed(self, text: str) -> None:
        if text:
            self.queue.put(text)

    def finish(self) -> None:
        self.queue.put(None)
        self.thread.join(timeout=self.timeout)
        if self.thread.is_alive():
            raise TimeoutError("Fish Audio streaming TTS did not finish")

    def _run(self) -> None:
        try:

            async def chunks() -> AsyncIterable[str]:
                while True:
                    item = await asyncio.to_thread(self.queue.get)
                    if item is None:
                        break
                    yield item

            with open(self.output_path, "wb") as f:
                self.result = asyncio.run(
                    stream_fish_audio_tts(
                        chunks(),
                        on_audio_chunk=f.write,
                    )
                )
        except Exception as exc:
            self.error = exc


def _run_hermes_agent_with_streaming_tts(
    transcript: str,
    output_path: Path,
    *,
    tts_timeout: float,
) -> tuple[str, dict[str, Any]]:
    from run_agent import AIAgent

    tts = _StreamingTTSFileWorker(output_path, timeout=tts_timeout)
    tts.start()
    started_at = time.monotonic()
    first_delta_ms: int | None = None
    deltas = 0

    def on_delta(delta: str) -> None:
        nonlocal first_delta_ms, deltas
        if first_delta_ms is None:
            first_delta_ms = int((time.monotonic() - started_at) * 1000)
        deltas += 1
        print(delta, end="", flush=True)
        tts.feed(delta)

    agent = AIAgent(skip_context_files=True, skip_memory=True, enabled_toolsets=[])
    system_message = (
        "あなたはYouTubeゲーム実況の配信用AIアシスタントです。"
        "音声で自然に掛け合うため、最初の返答は短く、ネタバレは避けてください。"
        "回答は日本語で、配信にそのまま乗せられる口調にしてください。"
    )
    result = agent.run_conversation(
        transcript,
        system_message=system_message,
        conversation_history=[],
        stream_callback=on_delta,
    )
    print()
    raw = result.get("final_response", "") if isinstance(result, dict) else str(result)
    tts.finish()
    if tts.error:
        raise tts.error

    return raw, {
        "first_delta_ms": first_delta_ms,
        "llm_elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "deltas": deltas,
        "tts": tts.result.__dict__ if tts.result else None,
        "tts_output_path": str(output_path),
        "tts_output_bytes": output_path.stat().st_size if output_path.exists() else 0,
    }


def _run_openai_compatible_with_streaming_tts(
    transcript: str,
    output_path: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    tts_timeout: float,
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    from openai import OpenAI

    tts = _StreamingTTSFileWorker(output_path, timeout=tts_timeout)
    tts.start()
    started_at = time.monotonic()
    first_delta_ms: int | None = None
    deltas = 0
    parts: list[str] = []

    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "あなたはYouTubeゲーム実況の配信用AIアシスタントです。"
                    "音声で自然に掛け合うため、最初の返答は短く、ネタバレは避けてください。"
                    "回答は日本語で、配信にそのまま乗せられる口調にしてください。"
                ),
            },
            {"role": "user", "content": transcript},
        ],
        stream=True,
        temperature=0.7,
        max_tokens=max_tokens,
    )
    for event in stream:
        choices = getattr(event, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        text = getattr(delta, "content", None) if delta is not None else None
        if not text:
            continue
        if first_delta_ms is None:
            first_delta_ms = int((time.monotonic() - started_at) * 1000)
        deltas += 1
        parts.append(text)
        print(text, end="", flush=True)
        tts.feed(text)
    print()
    tts.finish()
    if tts.error:
        raise tts.error
    close = getattr(client, "close", None)
    if callable(close):
        close()

    raw = "".join(parts)
    return raw, {
        "mode": "openai-compatible",
        "base_url": base_url,
        "model": model,
        "first_delta_ms": first_delta_ms,
        "llm_elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "deltas": deltas,
        "tts": tts.result.__dict__ if tts.result else None,
        "tts_output_path": str(output_path),
        "tts_output_bytes": output_path.stat().st_size if output_path.exists() else 0,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", help="Input recording, e.g. .aifc or .wav")
    parser.add_argument("--out", default="/tmp/hermes_voice_replay_tts.mp3", help="TTS output audio path")
    parser.add_argument("--summary", help="Optional JSON summary output")
    parser.add_argument("--realtime", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--llm-mode",
        choices=("hermes", "openai-compatible"),
        default="hermes",
        help="LLM path to use after STT",
    )
    parser.add_argument("--llm-base-url", default="http://100.94.173.74:8001/v1")
    parser.add_argument("--llm-model", default="gemma-4-e4b")
    parser.add_argument("--llm-api-key", default="not-needed")
    parser.add_argument("--tts-timeout", type=float, default=60.0)
    parser.add_argument("--max-tokens", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    recording = Path(args.recording).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()

    stt_started = time.monotonic()
    transcript, stt_summary = asyncio.run(_transcribe_recording(recording, realtime=args.realtime))
    if not transcript:
        raise RuntimeError("STT produced no final transcript")
    print(f"\n[transcript] {transcript}\n", flush=True)

    if args.llm_mode == "openai-compatible":
        response, llm_tts_summary = _run_openai_compatible_with_streaming_tts(
            transcript,
            out,
            base_url=args.llm_base_url,
            model=args.llm_model,
            api_key=args.llm_api_key,
            tts_timeout=args.tts_timeout,
            max_tokens=args.max_tokens,
        )
    else:
        response, llm_tts_summary = _run_hermes_agent_with_streaming_tts(
            transcript,
            out,
            tts_timeout=args.tts_timeout,
        )
    summary = {
        "recording": str(recording),
        "transcript": transcript,
        "response": response,
        "stt": stt_summary,
        "llm_tts": llm_tts_summary,
        "total_elapsed_ms": int((time.monotonic() - stt_started) * 1000),
    }
    print("\n[summary]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.summary:
        summary_path = Path(args.summary).expanduser().resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
