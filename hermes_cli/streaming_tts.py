"""Streaming text-to-speech helpers for low-latency voice mode."""

from __future__ import annotations

import asyncio
import logging
import queue
import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterable, Callable, Iterable, Optional

from hermes_cli.config import get_env_value, load_config

logger = logging.getLogger(__name__)


DEFAULT_FISH_AUDIO_WS_URL = "wss://api.fish.audio/v1/tts/live"
DEFAULT_FISH_AUDIO_MODEL = "s2-pro"
DEFAULT_FISH_AUDIO_FORMAT = "mp3"
DEFAULT_FISH_AUDIO_SAMPLE_RATE = 44100
DEFAULT_FISH_AUDIO_OPUS_SAMPLE_RATE = 48000
DEFAULT_FISH_AUDIO_MP3_BITRATE = 128
DEFAULT_FISH_AUDIO_OPUS_BITRATE = 32000
DEFAULT_FISH_AUDIO_LATENCY = "balanced"
DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS = 180.0


AudioChunkCallback = Callable[[bytes], None]


@dataclass(frozen=True)
class FishAudioStreamingTTSConfig:
    api_key: str
    reference_id: str
    model: str = DEFAULT_FISH_AUDIO_MODEL
    url: str = DEFAULT_FISH_AUDIO_WS_URL
    format: str = DEFAULT_FISH_AUDIO_FORMAT
    latency: str = DEFAULT_FISH_AUDIO_LATENCY
    chunk_length: int = 200
    sample_rate: int = DEFAULT_FISH_AUDIO_SAMPLE_RATE
    mp3_bitrate: int = DEFAULT_FISH_AUDIO_MP3_BITRATE
    opus_bitrate: int = DEFAULT_FISH_AUDIO_OPUS_BITRATE
    temperature: float = 0.7
    top_p: float = 0.7
    speed: float = 1.0
    volume: float = 0.0
    normalize: bool = True
    normalize_loudness: bool = True
    timeout_seconds: float = 30.0
    playback_drain_timeout_seconds: float = DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS


@dataclass(frozen=True)
class FishAudioStreamingTTSResult:
    audio_bytes: int
    chunks: int
    first_audio_ms: int | None
    elapsed_ms: int
    finish_reason: str = ""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def load_fish_audio_streaming_tts_config(
    config: Optional[dict[str, Any]] = None,
) -> FishAudioStreamingTTSConfig:
    root = config if isinstance(config, dict) else load_config()
    tts = root.get("tts") if isinstance(root, dict) else None
    tts = tts if isinstance(tts, dict) else {}
    fish = tts.get("fish_audio")
    fish = fish if isinstance(fish, dict) else {}

    fmt = str(fish.get("stream_format") or fish.get("format") or DEFAULT_FISH_AUDIO_FORMAT).strip().lower()
    if fmt not in {"mp3", "opus", "wav", "pcm"}:
        fmt = DEFAULT_FISH_AUDIO_FORMAT
    default_sample_rate = DEFAULT_FISH_AUDIO_OPUS_SAMPLE_RATE if fmt == "opus" else DEFAULT_FISH_AUDIO_SAMPLE_RATE

    return FishAudioStreamingTTSConfig(
        api_key=str(fish.get("api_key") or get_env_value("FISH_AUDIO_API_KEY") or "").strip(),
        reference_id=str(
            fish.get("reference_id")
            or fish.get("voice_id")
            or tts.get("voice")
            or ""
        ).strip(),
        model=str(fish.get("model") or DEFAULT_FISH_AUDIO_MODEL).strip() or DEFAULT_FISH_AUDIO_MODEL,
        url=str(fish.get("stream_url") or fish.get("ws_url") or DEFAULT_FISH_AUDIO_WS_URL).strip()
        or DEFAULT_FISH_AUDIO_WS_URL,
        format=fmt,
        latency=str(fish.get("stream_latency") or fish.get("latency") or DEFAULT_FISH_AUDIO_LATENCY).strip()
        or DEFAULT_FISH_AUDIO_LATENCY,
        chunk_length=_as_int(fish.get("stream_chunk_length", fish.get("chunk_length")), 200, minimum=1),
        sample_rate=_as_int(fish.get("sample_rate"), default_sample_rate, minimum=8000),
        mp3_bitrate=_as_int(fish.get("mp3_bitrate"), DEFAULT_FISH_AUDIO_MP3_BITRATE, minimum=8),
        opus_bitrate=_as_int(fish.get("opus_bitrate"), DEFAULT_FISH_AUDIO_OPUS_BITRATE, minimum=8000),
        temperature=_as_float(fish.get("temperature"), 0.7),
        top_p=_as_float(fish.get("top_p"), 0.7),
        speed=_as_float(fish.get("speed"), 1.0),
        volume=_as_float(fish.get("volume"), 0.0, minimum=-100.0),
        normalize=_as_bool(fish.get("normalize"), True),
        normalize_loudness=_as_bool(fish.get("normalize_loudness"), True),
        timeout_seconds=_as_float(
            fish.get("stream_timeout_seconds", fish.get("timeout_seconds")),
            30.0,
            minimum=1.0,
        ),
        playback_drain_timeout_seconds=_as_float(
            fish.get("stream_playback_drain_timeout_seconds"),
            DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS,
            minimum=5.0,
        ),
    )


def _default_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _pack_event(event: dict[str, Any]) -> bytes:
    try:
        import msgpack
    except ImportError as exc:
        raise RuntimeError("Fish Audio streaming TTS requires msgpack") from exc
    return msgpack.packb(event, use_bin_type=True)


def _unpack_event(message: str | bytes) -> dict[str, Any]:
    if isinstance(message, str):
        return {"event": "text", "text": message}
    try:
        import msgpack
    except ImportError as exc:
        raise RuntimeError("Fish Audio streaming TTS requires msgpack") from exc
    event = msgpack.unpackb(message, raw=False)
    if not isinstance(event, dict):
        return {"event": "audio", "audio": bytes(message)}
    return event


def _start_event(config: FishAudioStreamingTTSConfig) -> dict[str, Any]:
    request: dict[str, Any] = {
        "text": "",
        "format": config.format,
        "chunk_length": config.chunk_length,
        "reference_id": config.reference_id,
        "latency": config.latency,
        "sample_rate": config.sample_rate,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "prosody": {
            "speed": config.speed,
            "volume": config.volume,
            "normalize_loudness": config.normalize_loudness,
        },
        "normalize": config.normalize,
    }
    if config.format == "mp3":
        request["mp3_bitrate"] = config.mp3_bitrate
    elif config.format == "opus":
        request["opus_bitrate"] = config.opus_bitrate
    return {"event": "start", "request": request}


async def _send_text_events(ws: Any, text_chunks: AsyncIterable[str] | Iterable[str]) -> None:
    if hasattr(text_chunks, "__aiter__"):
        async for chunk in text_chunks:  # type: ignore[union-attr]
            if chunk:
                await ws.send(_pack_event({"event": "text", "text": str(chunk)}))
    else:
        for chunk in text_chunks:
            if chunk:
                await ws.send(_pack_event({"event": "text", "text": str(chunk)}))
    await ws.send(_pack_event({"event": "flush"}))
    await ws.send(_pack_event({"event": "stop"}))


async def stream_fish_audio_tts(
    text_chunks: AsyncIterable[str] | Iterable[str],
    *,
    config: Optional[FishAudioStreamingTTSConfig] = None,
    on_audio_chunk: Optional[AudioChunkCallback] = None,
) -> FishAudioStreamingTTSResult:
    """Stream text chunks into Fish Audio WebSocket TTS and consume audio chunks."""
    cfg = config or load_fish_audio_streaming_tts_config()
    if not cfg.api_key:
        raise ValueError("FISH_AUDIO_API_KEY not set")
    if not cfg.reference_id:
        raise ValueError("tts.fish_audio.reference_id is required")

    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Fish Audio streaming TTS requires websockets") from exc

    start = time.monotonic()
    first_audio_ms: int | None = None
    audio_bytes = 0
    chunks = 0
    finish_reason = ""

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "model": cfg.model,
    }
    logger.info(
        "Fish Audio streaming TTS connecting: model=%s format=%s latency=%s",
        cfg.model,
        cfg.format,
        cfg.latency,
    )
    async with websockets.connect(
        cfg.url,
        additional_headers=headers,
        ssl=_default_ssl_context(),
        max_size=16 * 1024 * 1024,
    ) as ws:
        await ws.send(_pack_event(_start_event(cfg)))
        sender = asyncio.create_task(_send_text_events(ws, text_chunks))
        try:
            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=cfg.timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("Fish Audio streaming TTS timed out waiting for audio") from exc
                event = _unpack_event(message)
                kind = str(event.get("event") or "")
                if kind == "audio":
                    audio = event.get("audio", b"")
                    if isinstance(audio, str):
                        audio = audio.encode("latin1")
                    if not isinstance(audio, (bytes, bytearray)):
                        continue
                    data = bytes(audio)
                    if not data:
                        continue
                    if first_audio_ms is None:
                        first_audio_ms = int((time.monotonic() - start) * 1000)
                        logger.info("Fish Audio streaming TTS first audio: %d ms", first_audio_ms)
                    chunks += 1
                    audio_bytes += len(data)
                    if on_audio_chunk is not None:
                        on_audio_chunk(data)
                    continue
                if kind == "finish":
                    finish_reason = str(event.get("reason") or "")
                    if finish_reason == "error":
                        raise RuntimeError(f"Fish Audio streaming TTS error: {event}")
                    break
        finally:
            if not sender.done():
                sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass

    return FishAudioStreamingTTSResult(
        audio_bytes=audio_bytes,
        chunks=chunks,
        first_audio_ms=first_audio_ms,
        elapsed_ms=int((time.monotonic() - start) * 1000),
        finish_reason=finish_reason,
    )


def stream_fish_audio_tts_to_file(
    text_chunks: Iterable[str],
    output_path: str | Path,
    *,
    config: Optional[FishAudioStreamingTTSConfig] = None,
) -> FishAudioStreamingTTSResult:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        return asyncio.run(
            stream_fish_audio_tts(
                text_chunks,
                config=config,
                on_audio_chunk=f.write,
            )
        )


class _FFplayAudioSink:
    def __init__(self, *, drain_timeout_seconds: float = DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS) -> None:
        ffplay = shutil.which("ffplay")
        if not ffplay:
            raise RuntimeError("ffplay is required for streaming TTS playback")
        self._drain_timeout_seconds = max(5.0, float(drain_timeout_seconds))
        self._proc = subprocess.Popen(
            [
                ffplay,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, data: bytes) -> None:
        if not data or self._proc.stdin is None or self._proc.poll() is not None:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except BrokenPipeError:
            return

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=self._drain_timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning(
                "ffplay did not finish within %.1fs after streaming TTS ended; terminating playback",
                self._drain_timeout_seconds,
            )
            self._proc.terminate()

    def cancel(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()


class FishAudioStreamingTTSWorker:
    """Thread-friendly Fish Audio streaming TTS worker.

    The TUI gateway is synchronous around ``AIAgent.run_conversation`` but
    receives assistant deltas incrementally. This worker bridges that sync
    callback into an async Fish Audio WebSocket session and streams MP3 bytes
    into ffplay as they arrive.
    """

    def __init__(self, config: Optional[FishAudioStreamingTTSConfig] = None) -> None:
        self.config = config or load_fish_audio_streaming_tts_config()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._sink: _FFplayAudioSink | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._cancelled = threading.Event()
        self.error: Exception | None = None
        self.result: FishAudioStreamingTTSResult | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="fish-audio-streaming-tts", daemon=True)
        self._thread.start()

    def feed(self, text: str) -> None:
        if not text or self._cancelled.is_set():
            return
        self._queue.put(text)

    def finish(self, *, wait: bool = False, timeout: float | None = None) -> None:
        self._queue.put(None)
        if wait:
            self._done.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def cancel(self) -> None:
        self._cancelled.set()
        self._queue.put(None)
        if self._sink is not None:
            self._sink.cancel()

    def _run(self) -> None:
        try:
            self._sink = _FFplayAudioSink(
                drain_timeout_seconds=self.config.playback_drain_timeout_seconds,
            )

            async def chunks() -> AsyncIterable[str]:
                while not self._cancelled.is_set():
                    item = await asyncio.to_thread(self._queue.get)
                    if item is None:
                        break
                    if item:
                        yield item

            self.result = asyncio.run(
                stream_fish_audio_tts(
                    chunks(),
                    config=self.config,
                    on_audio_chunk=self._sink.write,
                )
            )
        except Exception as exc:
            self.error = exc
            logger.warning("Fish Audio streaming TTS worker failed: %s", exc, exc_info=True)
        finally:
            if self._sink is not None and not self._cancelled.is_set():
                self._sink.close()
            self._done.set()


def fish_audio_streaming_tts_available(config: Optional[dict[str, Any]] = None) -> bool:
    try:
        cfg = load_fish_audio_streaming_tts_config(config)
    except Exception:
        return False
    return bool(cfg.api_key and cfg.reference_id and shutil.which("ffplay"))
