"""Voice notifications for dev orchestrator task events (Phase 6).

Watches Kanban events on dev tasks (tenant="dev") and reads short
Japanese summaries aloud via the configured TTS, with per-repository
cooldown. The per-task cursor lives in the dev-task-meta block
(``last_reported_event_id``) so restarts never re-announce old events.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from hermes_cli.dev_orchestrator import (
    DEV_TENANT,
    _update_task_meta,
    parse_dev_task_metadata,
)

Speaker = Callable[[str], None]
OverlayPublisher = Callable[[str], None]

# Kanban event kind -> (config flag, short Japanese template).
_EVENT_TEMPLATES: dict[str, tuple[str, str]] = {
    "claimed": ("report_started", "{repo} のタスクを開始しました。"),
    "completed": ("report_completed", "{repo} のタスクが完了しました。"),
    "blocked": ("report_blocked", "{repo} のタスクが blocked です。"),
    "gave_up": ("report_failed", "{repo} のタスクが失敗しました。"),
    "crashed": ("report_failed", "{repo} のタスクが失敗しました。"),
    "timed_out": ("report_failed", "{repo} のタスクが timeout しました。"),
}


@dataclass(frozen=True)
class VoiceNotifyConfig:
    enabled: bool = True
    max_chars: int = 120
    cooldown_seconds: int = 10
    report_started: bool = True
    report_completed: bool = True
    report_blocked: bool = True
    report_failed: bool = True


def load_voice_notify_config(config: dict[str, Any] | None) -> VoiceNotifyConfig:
    root = config if isinstance(config, dict) else {}
    dev = root.get("dev_orchestrator")
    dev = dev if isinstance(dev, dict) else {}
    raw = dev.get("voice_notifications")
    raw = raw if isinstance(raw, dict) else {}

    def _bool(name: str, default: bool) -> bool:
        value = raw.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int(name: str, default: int, *, minimum: int = 1) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= minimum else default

    return VoiceNotifyConfig(
        enabled=_bool("enabled", True),
        max_chars=_int("max_chars", 120, minimum=20),
        cooldown_seconds=_int("cooldown_seconds", 10, minimum=0),
        report_started=_bool("report_started", True),
        report_completed=_bool("report_completed", True),
        report_blocked=_bool("report_blocked", True),
        report_failed=_bool("report_failed", True),
    )


def compose_notification(
    kind: str,
    repo_id: str,
    payload: dict[str, Any] | None,
    cfg: VoiceNotifyConfig,
) -> str:
    from hermes_cli.live_coding import sanitize_for_stream

    entry = _EVENT_TEMPLATES.get(kind)
    if entry is None:
        return ""
    text = entry[1].format(repo=repo_id or "リポジトリ")
    reason = str((payload or {}).get("reason") or "").strip()
    if kind in {"blocked", "gave_up"} and reason:
        text += f" {reason}"
    text = sanitize_for_stream(text, max_chars=4000).strip()
    if len(text) > cfg.max_chars:
        text = text[: cfg.max_chars - 1] + "…"
    return text


def collect_dev_notifications(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Collect unreported dev task events and advance each task's cursor.

    The cursor advances over skipped kinds too, so disabled report
    flags never cause an event backlog.
    """
    from hermes_cli import kanban_db as kb

    cfg = load_voice_notify_config(config)
    notifications: list[dict[str, Any]] = []
    try:
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn, tenant=DEV_TENANT)
            events_by_task = {task.id: kb.list_events(conn, task.id) for task in tasks}
    except Exception:
        return []
    for task in tasks:
        meta = parse_dev_task_metadata(task.body)
        if meta.get("kind") != "dev_task" or not meta.get("notify_voice", True):
            continue
        try:
            cursor = int(meta.get("last_reported_event_id") or 0)
        except (TypeError, ValueError):
            cursor = 0
        fresh = [e for e in events_by_task.get(task.id, []) if int(e.id) > cursor]
        if not fresh:
            continue
        repo_id = str(meta.get("repo_id") or "")
        for event in fresh:
            entry = _EVENT_TEMPLATES.get(event.kind)
            if entry is None or not getattr(cfg, entry[0]):
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            text = compose_notification(event.kind, repo_id, payload, cfg)
            if not text:
                continue
            notifications.append(
                {
                    "task_id": task.id,
                    "repo_id": repo_id,
                    "kind": event.kind,
                    "event_id": int(event.id),
                    "text": text,
                }
            )
        try:
            _update_task_meta(task.id, {"last_reported_event_id": int(fresh[-1].id)})
        except Exception:
            pass
    return notifications


def _default_speaker(text: str) -> None:
    from hermes_cli import voice as voice_mod

    # Wait for any in-flight classic voice-mode TTS so notifications
    # never talk over an agent response.
    playing = getattr(voice_mod, "_tts_playing", None)
    if playing is not None:
        try:
            playing.wait(timeout=60)
        except Exception:
            pass
    voice_mod.speak_text(text)


class DevVoiceNotifier:
    """Delivers notifications with a per-repository cooldown."""

    def __init__(
        self,
        config: dict[str, Any] | None,
        *,
        speaker: Speaker | None = None,
        overlay: OverlayPublisher | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.cfg = load_voice_notify_config(config)
        self._speaker = speaker
        self._overlay = overlay
        self._clock = clock or time.monotonic
        self._last_spoken: dict[str, float] = {}

    def _publish_overlay(self, text: str) -> None:
        if self._overlay is not None:
            self._overlay(text)
            return
        try:
            from hermes_cli.live_overlay import publish_caption

            publish_caption(self.config, text, final=True, speaker="assistant", ttl_seconds=8.0)
        except Exception:
            pass

    def deliver(self, notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Speak and display notifications; returns the ones delivered.

        Within one batch only the last notification per repository is
        spoken; across batches a per-repository cooldown applies.
        Cooldown-skipped notifications are dropped, not queued.
        """
        if not self.cfg.enabled:
            return []
        latest_per_repo: dict[str, dict[str, Any]] = {}
        for item in notifications:
            latest_per_repo[item.get("repo_id") or item["task_id"]] = item
        delivered: list[dict[str, Any]] = []
        for key, item in latest_per_repo.items():
            now = self._clock()
            last = self._last_spoken.get(key)
            if last is not None and (now - last) < self.cfg.cooldown_seconds:
                continue
            self._last_spoken[key] = now
            self._publish_overlay(item["text"])
            speak = self._speaker or _default_speaker
            try:
                speak(item["text"])
            except Exception:
                continue
            delivered.append(item)
        return delivered

    def poll_once(self) -> list[dict[str, Any]]:
        return self.deliver(collect_dev_notifications(self.config))


def start_dev_voice_watcher(
    config: dict[str, Any] | None,
    *,
    interval_seconds: float = 5.0,
    stop_event: threading.Event | None = None,
    notifier: DevVoiceNotifier | None = None,
) -> threading.Thread | None:
    """Start the background watcher thread; returns None when disabled."""
    cfg = load_voice_notify_config(config)
    root = config if isinstance(config, dict) else {}
    dev = root.get("dev_orchestrator")
    dev = dev if isinstance(dev, dict) else {}
    if not cfg.enabled or not dev.get("enabled", True):
        return None
    worker = notifier or DevVoiceNotifier(config)
    stop = stop_event or threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                worker.poll_once()
            except Exception:
                pass
            stop.wait(max(0.5, float(interval_seconds)))

    thread = threading.Thread(target=_loop, name="dev-voice-notifier", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
