"""Live-coding assistant coordination helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.live_overlay import publish_live_coding_state


_SECRET_VALUE_RE = re.compile(
    r"(?i)\b("
    r"[A-Z0-9_-]*(?:api[_-]?key|secret|token|password|passwd|authorization|bearer|client[_-]?secret)[A-Z0-9_-]*"
    r")\b\s*([:=]\s*|\s+)([^\s'\"`]+)"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"ghp_[A-Za-z0-9_]{16,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"ya29\.[A-Za-z0-9_-]{16,}"
    r")\b"
)
_ABS_PATH_RE = re.compile(r"(?<![\w./-])(/(?:Users|Volumes|home|var|tmp)/[^\s'\"`]+)")


@dataclass(frozen=True)
class LiveCodingConfig:
    delegate_to: str = "codex"
    codex_path: str = "codex"
    timeout_seconds: int = 900
    max_output_chars: int = 6000
    allow_file_edits: bool = True
    require_approval_for_commit: bool = True
    require_approval_for_push: bool = True
    require_approval_for_delete: bool = True
    block_secret_paths: bool = True
    overlay_show_diff_summary: bool = True
    overlay_show_error_summary: bool = True
    tts_max_chars: int = 180


def load_live_coding_config(config: dict[str, Any] | None) -> LiveCodingConfig:
    root = config if isinstance(config, dict) else {}
    stream = root.get("stream_assistant")
    stream = stream if isinstance(stream, dict) else {}
    coding = stream.get("coding")
    coding = coding if isinstance(coding, dict) else {}

    def _str(name: str, default: str) -> str:
        value = coding.get(name, default)
        return str(value).strip() if value is not None and str(value).strip() else default

    def _bool(name: str, default: bool) -> bool:
        value = coding.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int(name: str, default: int, *, minimum: int = 1) -> int:
        value = coding.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= minimum else default

    return LiveCodingConfig(
        delegate_to=_str("delegate_to", "codex").lower(),
        codex_path=_str("codex_path", "codex"),
        timeout_seconds=_int("timeout_seconds", 900, minimum=10),
        max_output_chars=_int("max_output_chars", 6000, minimum=500),
        allow_file_edits=_bool("allow_file_edits", True),
        require_approval_for_commit=_bool("require_approval_for_commit", True),
        require_approval_for_push=_bool("require_approval_for_push", True),
        require_approval_for_delete=_bool("require_approval_for_delete", True),
        block_secret_paths=_bool("block_secret_paths", True),
        overlay_show_diff_summary=_bool("overlay_show_diff_summary", True),
        overlay_show_error_summary=_bool("overlay_show_error_summary", True),
        tts_max_chars=_int("tts_max_chars", 180, minimum=40),
    )


def check_codex_available(config: dict[str, Any] | None = None) -> bool:
    cfg = load_live_coding_config(config)
    if cfg.delegate_to != "codex":
        return False
    return shutil.which(cfg.codex_path) is not None


def sanitize_for_stream(text: str, *, max_chars: int = 6000) -> str:
    sanitized = str(text or "")
    sanitized = _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", sanitized)
    sanitized = _KNOWN_TOKEN_RE.sub("<redacted-token>", sanitized)
    sanitized = _ABS_PATH_RE.sub(_shorten_path_match, sanitized)
    if len(sanitized) > max_chars:
        sanitized = sanitized[-max_chars:].lstrip()
    return sanitized


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _shorten_path_match(match: re.Match[str]) -> str:
    value = match.group(1)
    try:
        path = Path(value)
        name = path.name or "path"
        parent = path.parent.name
        if parent:
            return f"<path>/{parent}/{name}"
        return f"<path>/{name}"
    except Exception:
        return "<path>"


def _compose_codex_prompt(task: str, cfg: LiveCodingConfig) -> str:
    rules = [
        "You are working during a live-coding stream.",
        "Keep changes scoped to the requested task.",
        "Do not reveal secrets, API keys, tokens, .env contents, private keys, or credentials.",
        "Do not run git commit or git push.",
        "Do not delete files unless the user explicitly requested deletion.",
        "Run focused checks when practical and summarize the result.",
    ]
    if not cfg.allow_file_edits:
        rules.append("Do not modify files; inspect and report only.")
    if cfg.require_approval_for_delete:
        rules.append("Ask for explicit approval before destructive file deletion.")
    return "\n".join(["\n".join(f"- {rule}" for rule in rules), "", "Task:", task.strip()])


def build_codex_command(task: str, cfg: LiveCodingConfig) -> list[str]:
    if cfg.delegate_to != "codex":
        raise ValueError(f"unsupported live coding delegate: {cfg.delegate_to}")
    return [cfg.codex_path, "exec", _compose_codex_prompt(task, cfg)]


def run_codex_delegate(
    task: str,
    *,
    config: dict[str, Any] | None = None,
    workdir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    cfg = load_live_coding_config(config)
    cleaned_task = sanitize_for_stream(task, max_chars=500)
    if not cleaned_task:
        return {"success": False, "error": "task is required"}
    if cfg.delegate_to != "codex":
        return {"success": False, "error": f"unsupported delegate: {cfg.delegate_to}"}
    if shutil.which(cfg.codex_path) is None:
        return {"success": False, "error": f"Codex CLI not found: {cfg.codex_path}"}

    cwd = Path(workdir or os.getcwd()).expanduser().resolve()
    if not cwd.exists() or not cwd.is_dir():
        return {"success": False, "error": f"workdir does not exist: {sanitize_for_stream(str(cwd), max_chars=500)}"}

    command = build_codex_command(task, cfg)
    _publish_status(
        config,
        status="running",
        codex_status="running",
        current_task=cleaned_task,
        next_step="Codex が作業中です",
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=cfg.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        partial = sanitize_for_stream(
            _to_text(exc.stdout) + "\n" + _to_text(exc.stderr),
            max_chars=cfg.max_output_chars,
        )
        _publish_status(
            config,
            status="failed",
            codex_status="failed",
            current_task=cleaned_task,
            error_summary="Codex 実行が timeout しました",
            next_step="作業を小さく分けて再実行してください",
        )
        return {
            "success": False,
            "status": "timeout",
            "duration_ms": duration_ms,
            "output": partial,
            "error": "Codex execution timed out",
        }

    duration_ms = int((time.monotonic() - started) * 1000)
    output = sanitize_for_stream(
        "\n".join(part for part in [result.stdout, result.stderr] if part),
        max_chars=cfg.max_output_chars,
    )
    success = result.returncode == 0
    _publish_status(
        config,
        status="done" if success else "failed",
        codex_status="done" if success else "failed",
        current_task=cleaned_task,
        error_summary="" if success else _first_nonempty_line(output),
        next_step="結果を確認してください" if success else "Codex の出力を確認してください",
    )
    return {
        "success": success,
        "status": "done" if success else "failed",
        "delegate": "codex",
        "returncode": result.returncode,
        "duration_ms": duration_ms,
        "workdir": sanitize_for_stream(str(cwd), max_chars=500),
        "output": output,
    }


def _first_nonempty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:180]
    return "Codex execution failed"


def _publish_status(config: dict[str, Any] | None, **fields: Any) -> None:
    try:
        publish_live_coding_state(config, **fields)
    except Exception:
        return
