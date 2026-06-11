"""Development orchestrator helpers for multi-repository work."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ConfigSaver = Callable[[str, Any], bool]
Opener = Callable[[list[str]], subprocess.CompletedProcess[str]]

_GITHUB_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")

KNOWN_WORKERS = ("codex", "claude", "hermes")

_WORKER_ALIASES = {
    "claude_code": "claude",
    "claude-code": "claude",
    "claudecode": "claude",
}


def normalize_worker(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    return _WORKER_ALIASES.get(cleaned, cleaned)


DEV_TENANT = "dev"
_DEV_META_RE = re.compile(r"```dev-task-meta\s*\n(\{.*?\})\s*\n```", re.DOTALL)
_TITLE_MAX_CHARS = 80
_DEFAULT_WORKER_TIMEOUT_SECONDS = 1800
_OUTPUT_TAIL_CHARS = 4000
_GH_TIMEOUT_SECONDS = 60
_ISSUE_BODY_MAX_CHARS = 2000

# Live worker subprocesses by task id, so /dev stop can terminate them.
_workers_lock = threading.Lock()
_RUNNING_WORKERS: dict[str, subprocess.Popen] = {}
_STOPPED_TASKS: set[str] = set()


@dataclass(frozen=True)
class RepositoryInfo:
    repo_id: str
    local_path: str
    github: str = ""
    default_branch: str = ""
    worktree_root: str = ""
    worker: str = ""
    exists: bool = False
    is_git_repo: bool = False


def save_config_value(key_path: str, value: Any) -> bool:
    """Persist one config value using the round-trip YAML updater."""
    return _write_config_key(key_path, value, delete=False)


def delete_config_value(key_path: str) -> bool:
    """Remove one config key using the round-trip YAML updater."""
    return _write_config_key(key_path, None, delete=True)


def _write_config_key(key_path: str, value: Any, *, delete: bool) -> bool:
    from hermes_cli.config import ensure_hermes_home, get_config_path, is_managed, managed_error
    from utils import atomic_roundtrip_yaml_update

    if is_managed():
        managed_error("save dev orchestrator config")
        return False
    ensure_hermes_home()
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_roundtrip_yaml_update(config_path, key_path, value, delete=delete)
        return True
    except Exception:
        return False


def _expand_path(path: str) -> Path:
    raw = str(path or "").strip()
    raw = raw.replace("$HERMES_HOME", str(_hermes_home()))
    raw = raw.replace("${HERMES_HOME}", str(_hermes_home()))
    return Path(raw).expanduser()


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _repo_config(config: dict[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, dict) else {}
    repos = root.get("repositories")
    return repos if isinstance(repos, dict) else {}


def _dev_config(config: dict[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, dict) else {}
    dev = root.get("dev_orchestrator")
    return dev if isinstance(dev, dict) else {}


def _git_remote_github(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    match = _GITHUB_RE.search((proc.stdout or "").strip())
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _git_default_branch(path: Path) -> str:
    commands = (
        ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        value = (proc.stdout or "").strip()
        if value.startswith("origin/"):
            value = value.split("/", 1)[1]
        if value and value != "HEAD":
            return value
    return ""


def load_repositories(config: dict[str, Any] | None) -> list[RepositoryInfo]:
    repos: list[RepositoryInfo] = []
    dev = _dev_config(config)
    # Callers like the TUI gateway pass the raw config.yaml (no
    # DEFAULT_CONFIG merge), so fall back to the documented defaults here.
    default_worktree_root = str(dev.get("worktree_root") or "") or str(_hermes_home() / "dev-worktrees")
    default_worker = str(dev.get("default_worker") or "") or "codex"
    for repo_id, raw in sorted(_repo_config(config).items()):
        if not isinstance(raw, dict):
            continue
        local_path = str(raw.get("local_path") or raw.get("path") or "").strip()
        if not local_path:
            continue
        path = _expand_path(local_path)
        exists = path.is_dir()
        is_git_repo = (path / ".git").exists()
        github = str(raw.get("github") or "").strip()
        default_branch = str(raw.get("default_branch") or "").strip()
        if exists and is_git_repo:
            github = github or _git_remote_github(path)
            default_branch = default_branch or _git_default_branch(path)
        worktree_root = str(raw.get("worktree_root") or "").strip()
        if not worktree_root:
            worktree_root = str(_expand_path(default_worktree_root) / str(repo_id))
        repos.append(
            RepositoryInfo(
                repo_id=str(repo_id),
                local_path=str(path),
                github=github,
                default_branch=default_branch,
                worktree_root=worktree_root,
                worker=normalize_worker(raw.get("worker") or default_worker or ""),
                exists=exists,
                is_git_repo=is_git_repo,
            )
        )
    return repos


def get_repository(config: dict[str, Any] | None, repo_id: str) -> RepositoryInfo | None:
    target = str(repo_id or "").strip()
    if not target:
        return None
    for repo in load_repositories(config):
        if repo.repo_id == target:
            return repo
    return None


def format_repositories(repos: list[RepositoryInfo]) -> str:
    if not repos:
        return (
            "Development repositories\n"
            "  (none configured)\n\n"
            "Add one with: /dev repo add <repo_id> <local_path> [--github owner/repo]"
        )
    lines = ["Development repositories"]
    for repo in repos:
        state = "ok" if repo.exists and repo.is_git_repo else "missing" if not repo.exists else "not-git"
        details = []
        if repo.github:
            details.append(repo.github)
        if repo.default_branch:
            details.append(f"branch={repo.default_branch}")
        if repo.worker:
            details.append(f"worker={repo.worker}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"  - {repo.repo_id}: {repo.local_path} [{state}]{suffix}")
    return "\n".join(lines)


def format_repository(repo: RepositoryInfo | None, repo_id: str = "") -> str:
    if repo is None:
        return f"Repository not found: {repo_id}"
    return "\n".join(
        [
            f"Repository: {repo.repo_id}",
            f"  Path:          {repo.local_path}",
            f"  Exists:        {'yes' if repo.exists else 'no'}",
            f"  Git repo:      {'yes' if repo.is_git_repo else 'no'}",
            f"  GitHub:        {repo.github or '-'}",
            f"  Default branch:{' ' + repo.default_branch if repo.default_branch else ' -'}",
            f"  Worktree root: {repo.worktree_root or '-'}",
            f"  Worker:        {repo.worker or '-'}",
        ]
    )


def add_repository(
    repo_id: str,
    local_path: str,
    *,
    github: str = "",
    default_branch: str = "",
    worker: str = "",
    saver: ConfigSaver | None = None,
) -> dict[str, Any]:
    clean_id = str(repo_id or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", clean_id):
        return {"success": False, "error": "repo_id must contain only letters, numbers, underscore, or dash"}
    clean_worker = normalize_worker(worker)
    if clean_worker and clean_worker not in KNOWN_WORKERS:
        return {"success": False, "error": f"unknown worker: {worker} (expected one of: {', '.join(KNOWN_WORKERS)})"}
    path = _expand_path(local_path)
    value: dict[str, Any] = {"local_path": str(path)}
    if github:
        value["github"] = github
    if default_branch:
        value["default_branch"] = default_branch
    if clean_worker:
        value["worker"] = clean_worker
    writer = saver or save_config_value
    ok = writer(f"repositories.{clean_id}", value)
    return {"success": ok, "repo_id": clean_id, "repository": value, "error": "" if ok else "failed to save config"}


def _compose_dev_task_body(task_text: str, meta: dict[str, Any]) -> str:
    return "\n".join(
        [
            task_text.strip(),
            "",
            "```dev-task-meta",
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def parse_dev_task_metadata(body: str | None) -> dict[str, Any]:
    match = _DEV_META_RE.search(str(body or ""))
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def assign_dev_task(
    config: dict[str, Any] | None,
    repo_id: str,
    task_text: str,
    *,
    worker: str = "",
    requested_by: str = "cli",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cleaned = str(task_text or "").strip()
    if not cleaned:
        return {"success": False, "error": "task description is required"}
    repo = get_repository(config, repo_id)
    if repo is None:
        return {"success": False, "error": f"repository not found: {repo_id}"}
    if not repo.exists or not repo.is_git_repo:
        return {"success": False, "error": f"repository is not a usable git checkout: {repo.local_path}"}
    dev = _dev_config(config)
    clean_worker = normalize_worker(worker or repo.worker or str(dev.get("default_worker") or ""))
    if clean_worker not in KNOWN_WORKERS:
        return {"success": False, "error": f"unknown worker: {clean_worker or '(empty)'} (expected one of: {', '.join(KNOWN_WORKERS)})"}

    meta = {
        "kind": "dev_task",
        "repo_id": repo.repo_id,
        "github": repo.github,
        "local_path": repo.local_path,
        "worktree_path": "",
        "branch": "",
        "issue": None,
        "pr": None,
        "worker": clean_worker,
        "requested_by": requested_by,
        "notify_voice": True,
        "last_reported_event_id": None,
    }
    if extra_meta:
        meta.update(extra_meta)
    title = cleaned.splitlines()[0]
    title = title if len(title) <= _TITLE_MAX_CHARS else title[: _TITLE_MAX_CHARS - 3] + "..."
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            # The worker name doubles as the assignee so the kanban
            # dispatcher's default_assignee never adopts dev tasks; the dev
            # orchestrator runs them itself (Phase 4).
            task_id = kb.create_task(
                conn,
                title=title,
                body=_compose_dev_task_body(cleaned, meta),
                assignee=clean_worker,
                created_by="dev-orchestrator",
                tenant=DEV_TENANT,
            )
    except Exception as exc:
        return {"success": False, "error": f"failed to create dev task: {exc}"}
    return {
        "success": True,
        "task_id": task_id,
        "repo_id": repo.repo_id,
        "worker": clean_worker,
        "title": title,
    }


def list_dev_tasks(
    config: dict[str, Any] | None,
    *,
    repo_id: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    del config
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn, tenant=DEV_TENANT, limit=limit)
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for task in tasks:
        meta = parse_dev_task_metadata(task.body)
        if meta.get("kind") != "dev_task":
            continue
        if repo_id and str(meta.get("repo_id") or "") != repo_id:
            continue
        items.append(
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "repo_id": str(meta.get("repo_id") or ""),
                "worker": str(meta.get("worker") or task.assignee or ""),
                "branch": str(meta.get("branch") or ""),
                "pr": meta.get("pr"),
                "issue": meta.get("issue"),
                "created_at": task.created_at,
            }
        )
    return items


def format_dev_tasks(items: list[dict[str, Any]], repo_id: str = "") -> str:
    scope = f" ({repo_id})" if repo_id else ""
    if not items:
        return (
            f"Dev tasks{scope}\n"
            "  (none)\n\n"
            "Create one with: /dev assign <repo_id> <task description>"
        )
    lines = [f"Dev tasks{scope}"]
    for item in items:
        details = [item["repo_id"], f"worker={item['worker']}"]
        if item.get("branch"):
            details.append(f"branch={item['branch']}")
        if item.get("pr"):
            details.append(f"pr={item['pr']}")
        lines.append(f"  - {item['task_id']} [{item['status']}] {item['title']} ({', '.join(d for d in details if d)})")
    return "\n".join(lines)


def summarize_dev_tasks(items: list[dict[str, Any]]) -> str:
    if not items:
        return "no tasks"
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    return f"{len(items)} total ({', '.join(parts)})"


def _run_git(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def create_task_worktree(repo: RepositoryInfo, task_id: str) -> dict[str, Any]:
    """Create (or reuse) a dedicated git worktree for a dev task."""
    if not repo.worktree_root:
        return {"success": False, "error": f"no worktree_root configured for repository: {repo.repo_id}"}
    if not repo.exists or not repo.is_git_repo:
        return {"success": False, "error": f"repository is not a usable git checkout: {repo.local_path}"}
    branch = f"dev/{task_id}"
    path = _expand_path(repo.worktree_root) / task_id
    if path.is_dir():
        return {"success": True, "worktree_path": str(path), "branch": branch, "reused": True}
    path.parent.mkdir(parents=True, exist_ok=True)

    add = ["-C", repo.local_path, "worktree", "add", "-b", branch, str(path)]
    if repo.default_branch:
        add.append(repo.default_branch)
    try:
        proc = _run_git(add)
        if proc.returncode != 0 and "already exists" in (proc.stderr or ""):
            # Branch left over from an earlier attempt — attach to it.
            proc = _run_git(["-C", repo.local_path, "worktree", "add", str(path), branch])
    except Exception as exc:
        return {"success": False, "error": f"failed to create worktree: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {"success": False, "error": f"failed to create worktree: {detail}"}
    return {"success": True, "worktree_path": str(path), "branch": branch, "reused": False}


def _worktree_change_summary(worktree_path: str, default_branch: str) -> str:
    parts: list[str] = []
    try:
        status = _run_git(["-C", worktree_path, "status", "--porcelain"])
        changed = [line for line in (status.stdout or "").splitlines() if line.strip()]
        parts.append(f"{len(changed)} uncommitted file(s)")
        if default_branch:
            commits = _run_git(["-C", worktree_path, "rev-list", "--count", f"{default_branch}..HEAD"])
            if commits.returncode == 0:
                parts.append(f"{(commits.stdout or '0').strip()} new commit(s)")
        diff = _run_git(["-C", worktree_path, "diff", "--stat", "HEAD"])
        stat_tail = (diff.stdout or "").strip().splitlines()
        if stat_tail:
            parts.append(stat_tail[-1].strip())
    except Exception:
        pass
    return ", ".join(parts) if parts else "no change information"


def _compose_worker_prompt(task_text: str, repo: RepositoryInfo, config: dict[str, Any] | None) -> str:
    dev = _dev_config(config)
    rules = [
        "You are a development worker for the Hermes dev orchestrator.",
        "Work only inside the current working directory; it is a dedicated git worktree for this task.",
        "Keep changes scoped to the requested task.",
        "Do not reveal secrets, API keys, tokens, .env contents, private keys, or credentials.",
        "Do not run git push.",
        "Do not create GitHub issues or pull requests.",
        "Run the project's focused tests when practical and summarize the result.",
    ]
    if dev.get("require_approval_for_commit", False):
        rules.append("Do not run git commit; leave changes uncommitted for review.")
    else:
        rules.append("You may create focused local git commits on the current branch.")
    return "\n".join(
        [
            "Follow these rules for this delegated task:",
            "\n".join(f"- {rule}" for rule in rules),
            "",
            f"Repository: {repo.repo_id}" + (f" ({repo.github})" if repo.github else ""),
            "",
            "Task:",
            task_text.strip(),
        ]
    )


def _worker_argv(config: dict[str, Any] | None, worker: str, prompt: str) -> list[str]:
    from hermes_cli.live_coding import delegate_argv, load_live_coding_config

    return delegate_argv(load_live_coding_config(config), prompt, delegate=worker)


def _worker_timeout_seconds(config: dict[str, Any] | None) -> int:
    value = _dev_config(config).get("worker_timeout_seconds", _DEFAULT_WORKER_TIMEOUT_SECONDS)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_WORKER_TIMEOUT_SECONDS
    return parsed if parsed >= 10 else _DEFAULT_WORKER_TIMEOUT_SECONDS


def _task_text_without_meta(body: str | None) -> str:
    return _DEV_META_RE.sub("", str(body or "")).strip()


def _update_task_meta(task_id: str, meta: dict[str, Any]) -> None:
    """Merge keys into the dev-task-meta block in the task body.

    Merging (rather than overwriting) keeps concurrent writers safe:
    the worker lane writes worktree/branch while the voice notifier
    advances last_reported_event_id on the same task.
    """
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            return
        merged = parse_dev_task_metadata(task.body)
        merged.update(meta)
        body = _compose_dev_task_body(_task_text_without_meta(task.body), merged)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))


def _write_worker_log(task_id: str, text: str) -> str:
    try:
        from hermes_cli import kanban_db as kb

        path = kb.worker_log_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)
    except Exception:
        return ""


def _prepare_dev_run(
    config: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """Validate, create the worktree, and claim the task (ready -> running)."""
    from hermes_cli import kanban_db as kb

    clean_id = str(task_id or "").strip()
    if not clean_id:
        return {"success": False, "error": "usage: /dev run <task_id>"}
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, clean_id)
    if task is None:
        return {"success": False, "error": f"task not found: {clean_id}"}
    meta = parse_dev_task_metadata(task.body)
    if meta.get("kind") != "dev_task":
        return {"success": False, "error": f"not a dev task: {clean_id}"}
    if task.status != "ready":
        return {"success": False, "error": f"task is not ready (status: {task.status})"}

    repo = get_repository(config, str(meta.get("repo_id") or ""))
    if repo is None:
        return {"success": False, "error": f"repository not found: {meta.get('repo_id')}"}
    worker = normalize_worker(str(meta.get("worker") or "")) or repo.worker
    if worker == "hermes":
        return {"success": False, "error": "hermes worker lane is not implemented yet; use codex or claude"}
    if worker not in KNOWN_WORKERS:
        return {"success": False, "error": f"unknown worker: {worker}"}

    worktree = create_task_worktree(repo, clean_id)
    if not worktree.get("success"):
        return {"success": False, "error": worktree.get("error")}
    worktree_path = str(worktree["worktree_path"])
    branch = str(worktree["branch"])

    prompt = _compose_worker_prompt(_task_text_without_meta(task.body), repo, config)
    try:
        command = _worker_argv(config, worker, prompt)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if shutil.which(command[0]) is None:
        return {"success": False, "error": f"worker CLI not found: {command[0]}"}

    with kb.connect_closing() as conn:
        claimed = kb.claim_task(conn, clean_id, claimer="dev-orchestrator")
    if claimed is None:
        return {"success": False, "error": f"could not claim task (already claimed?): {clean_id}"}

    meta.update({"worktree_path": worktree_path, "branch": branch, "worker": worker})
    try:
        _update_task_meta(clean_id, meta)
    except Exception:
        pass
    return {
        "success": True,
        "task_id": clean_id,
        "worker": worker,
        "branch": branch,
        "worktree_path": worktree_path,
        "command": command,
        "repo": repo,
        "meta": meta,
    }


def run_dev_task(
    config: dict[str, Any] | None,
    task_id: str,
    *,
    runner: Opener | None = None,
) -> dict[str, Any]:
    """Run one dev task to completion in a dedicated worktree (Phase 4)."""
    prepared = _prepare_dev_run(config, task_id)
    if not prepared.get("success"):
        return prepared
    return _execute_dev_run(config, prepared, runner=runner)


def start_dev_task(
    config: dict[str, Any] | None,
    task_id: str,
    *,
    runner: Opener | None = None,
) -> dict[str, Any]:
    """Claim a dev task and run the worker in a background thread.

    Returns immediately; progress lands on the Kanban task (and the
    voice notifier announces claimed/completed/blocked events).
    """
    import threading

    prepared = _prepare_dev_run(config, task_id)
    if not prepared.get("success"):
        return prepared

    thread = threading.Thread(
        target=_execute_dev_run,
        args=(config, prepared),
        kwargs={"runner": runner},
        name=f"dev-worker-{prepared['task_id']}",
        daemon=True,
    )
    thread.start()
    return {
        "success": True,
        "status": "started",
        "task_id": prepared["task_id"],
        "worker": prepared["worker"],
        "branch": prepared["branch"],
        "worktree_path": prepared["worktree_path"],
        "thread": thread,
    }


def _execute_dev_run(
    config: dict[str, Any] | None,
    prepared: dict[str, Any],
    *,
    runner: Opener | None = None,
) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.live_coding import sanitize_for_stream

    clean_id = str(prepared["task_id"])
    worker = str(prepared["worker"])
    branch = str(prepared["branch"])
    worktree_path = str(prepared["worktree_path"])
    command = list(prepared["command"])
    repo = prepared["repo"]

    run = runner or _default_worker_run
    timeout = _worker_timeout_seconds(config)
    started = time.monotonic()
    try:
        proc = _invoke_worker(run, command, worktree_path, timeout, clean_id)
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - started) * 1000)
        reason = f"worker timed out after {timeout}s (worker={worker}, branch={branch})"
        with kb.connect_closing() as conn:
            kb.block_task(conn, clean_id, reason=reason)
        return {"success": False, "status": "blocked", "error": reason, "duration_ms": duration_ms}
    except Exception as exc:
        reason = f"worker failed to start: {exc}"
        with kb.connect_closing() as conn:
            kb.block_task(conn, clean_id, reason=reason)
        return {"success": False, "status": "blocked", "error": reason}

    duration_ms = int((time.monotonic() - started) * 1000)
    with _workers_lock:
        was_stopped = clean_id in _STOPPED_TASKS
        _STOPPED_TASKS.discard(clean_id)
    output = sanitize_for_stream(
        "\n".join(part for part in [proc.stdout, proc.stderr] if part),
        max_chars=_OUTPUT_TAIL_CHARS,
    )
    log_path = _write_worker_log(clean_id, output)
    if was_stopped:
        reason = "stopped by user"
        with kb.connect_closing() as conn:
            kb.block_task(conn, clean_id, reason=reason)
        return {
            "success": False,
            "status": "blocked",
            "task_id": clean_id,
            "worker": worker,
            "branch": branch,
            "worktree_path": worktree_path,
            "duration_ms": duration_ms,
            "log_path": log_path,
            "error": reason,
            "output": output,
        }
    changes = _worktree_change_summary(worktree_path, repo.default_branch)
    run_meta = {
        "kind": "dev_task",
        "worker": worker,
        "branch": branch,
        "worktree_path": worktree_path,
        "returncode": proc.returncode,
        "duration_ms": duration_ms,
        "change_summary": changes,
        "log_path": log_path,
    }

    if proc.returncode == 0:
        summary = f"worker={worker} done: {changes}"
        with kb.connect_closing() as conn:
            kb.complete_task(conn, clean_id, result=output[-1000:], summary=summary, metadata=run_meta)
        result = {
            "success": True,
            "status": "done",
            "task_id": clean_id,
            "worker": worker,
            "branch": branch,
            "worktree_path": worktree_path,
            "change_summary": changes,
            "duration_ms": duration_ms,
            "log_path": log_path,
            "output": output,
        }
        if bool(_dev_config(config).get("auto_create_pr", False)):
            try:
                pr = create_dev_pr(config, clean_id, confirm=True, runner=runner)
            except Exception as exc:
                pr = {"success": False, "error": str(exc)}
            result["pr_success"] = bool(pr.get("success"))
            result["pr_url"] = str(pr.get("pr_url") or "")
            result["pr_error"] = str(pr.get("error") or "")
        return result

    reason = f"worker={worker} failed with exit code {proc.returncode}"
    with kb.connect_closing() as conn:
        kb.block_task(conn, clean_id, reason=reason)
    return {
        "success": False,
        "status": "blocked",
        "task_id": clean_id,
        "worker": worker,
        "branch": branch,
        "worktree_path": worktree_path,
        "change_summary": changes,
        "duration_ms": duration_ms,
        "log_path": log_path,
        "error": reason,
        "output": output,
    }


def _invoke_worker(
    run: Opener,
    command: list[str],
    worktree_path: str,
    timeout: int,
    task_id: str = "",
) -> subprocess.CompletedProcess[str]:
    if run is _default_worker_run:
        return _default_worker_run(command, cwd=worktree_path, timeout=timeout, task_id=task_id)
    return run(command)


def _default_worker_run(
    command: list[str],
    *,
    cwd: str = "",
    timeout: int = _DEFAULT_WORKER_TIMEOUT_SECONDS,
    task_id: str = "",
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        cwd=cwd or None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    if task_id:
        with _workers_lock:
            _RUNNING_WORKERS[task_id] = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        if task_id:
            with _workers_lock:
                _RUNNING_WORKERS.pop(task_id, None)
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def stop_dev_task(config: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    """Stop a running dev task.

    Terminates the live worker subprocess when this process owns it;
    otherwise just clears the Kanban state (running -> blocked) — a
    worker started by another gateway process is not killed.
    """
    del config
    from hermes_cli import kanban_db as kb

    clean_id = str(task_id or "").strip()
    if not clean_id:
        return {"success": False, "error": "usage: /dev stop <task_id>"}
    with _workers_lock:
        proc = _RUNNING_WORKERS.get(clean_id)
        if proc is not None:
            _STOPPED_TASKS.add(clean_id)
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        return {
            "success": True,
            "status": "stopping",
            "task_id": clean_id,
            "output": f"Worker terminated for {clean_id}; the task will be marked blocked (stopped by user).",
        }

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, clean_id)
        if task is None:
            return {"success": False, "error": f"task not found: {clean_id}"}
        if task.status != "running":
            return {"success": False, "error": f"task is not running (status: {task.status})"}
        kb.block_task(conn, clean_id, reason="stopped by user")
    return {
        "success": True,
        "status": "blocked",
        "task_id": clean_id,
        "output": (
            f"Task {clean_id} marked blocked (stopped by user).\n"
            "  Note: no live worker in this process — if another process started it, "
            "its worker may still be running."
        ),
    }


def format_run_result(result: dict[str, Any]) -> str:
    if not result.get("success") and not result.get("status"):
        return f"Dev task run failed: {result.get('error') or 'unknown error'}"
    lines = [
        f"Dev task {result.get('task_id')}: {result.get('status')}",
        f"  Worker:   {result.get('worker')}",
        f"  Branch:   {result.get('branch')}",
        f"  Worktree: {result.get('worktree_path')}",
        f"  Changes:  {result.get('change_summary')}",
    ]
    if result.get("log_path"):
        lines.append(f"  Log:      {result.get('log_path')}")
    if result.get("error"):
        lines.append(f"  Error:    {result.get('error')}")
    if "pr_success" in result:
        if result.get("pr_success"):
            lines.append(f"  PR:       {result.get('pr_url') or '(created)'}")
        else:
            lines.append(f"  PR:       failed — {result.get('pr_error') or 'unknown error'}")
    return "\n".join(lines)


def _run_external(command: list[str], *, timeout: int = _GH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def _require_gh(repo: RepositoryInfo) -> str:
    """Return an error message when gh-based operations cannot run."""
    if shutil.which("gh") is None:
        return "gh CLI not found (install GitHub CLI and run: gh auth login)"
    if not repo.github:
        return f"no GitHub remote configured for repository: {repo.repo_id}"
    return ""


def list_github_issues(
    config: dict[str, Any] | None,
    repo_id: str,
    *,
    limit: int = 10,
    runner: Opener | None = None,
) -> dict[str, Any]:
    repo = get_repository(config, repo_id)
    if repo is None:
        return {"success": False, "error": f"repository not found: {repo_id}"}
    blocked = _require_gh(repo)
    if blocked:
        return {"success": False, "error": blocked}
    run = runner or _run_external
    try:
        proc = run(["gh", "-R", repo.github, "issue", "list", "--limit", str(limit), "--json", "number,title,state"])
    except Exception as exc:
        return {"success": False, "error": f"gh issue list failed: {exc}"}
    if proc.returncode != 0:
        return {"success": False, "error": (proc.stderr or proc.stdout or "gh issue list failed").strip()}
    try:
        issues = json.loads(proc.stdout or "[]")
    except Exception:
        return {"success": False, "error": "could not parse gh issue list output"}
    return {"success": True, "issues": issues if isinstance(issues, list) else []}


def view_github_issue(
    config: dict[str, Any] | None,
    repo_id: str,
    number: int,
    *,
    runner: Opener | None = None,
) -> dict[str, Any]:
    repo = get_repository(config, repo_id)
    if repo is None:
        return {"success": False, "error": f"repository not found: {repo_id}"}
    blocked = _require_gh(repo)
    if blocked:
        return {"success": False, "error": blocked}
    run = runner or _run_external
    try:
        proc = run(["gh", "-R", repo.github, "issue", "view", str(int(number)), "--json", "number,title,body,state,url"])
    except Exception as exc:
        return {"success": False, "error": f"gh issue view failed: {exc}"}
    if proc.returncode != 0:
        return {"success": False, "error": (proc.stderr or proc.stdout or "gh issue view failed").strip()}
    try:
        issue = json.loads(proc.stdout or "{}")
    except Exception:
        return {"success": False, "error": "could not parse gh issue view output"}
    if not isinstance(issue, dict) or "number" not in issue:
        return {"success": False, "error": f"issue not found: #{number}"}
    return {"success": True, "issue": issue}


def format_issues(issues: list[dict[str, Any]], repo_id: str) -> str:
    if not issues:
        return f"GitHub issues ({repo_id})\n  (none open)"
    lines = [f"GitHub issues ({repo_id})"]
    for issue in issues:
        lines.append(f"  - #{issue.get('number')} [{issue.get('state', '').lower()}] {issue.get('title', '')}")
    return "\n".join(lines)


def format_issue(issue: dict[str, Any]) -> str:
    body = str(issue.get("body") or "").strip()
    if len(body) > _ISSUE_BODY_MAX_CHARS:
        body = body[:_ISSUE_BODY_MAX_CHARS] + "\n... (truncated)"
    return "\n".join(
        [
            f"Issue #{issue.get('number')}: {issue.get('title', '')}",
            f"  State: {str(issue.get('state', '')).lower()}",
            f"  URL:   {issue.get('url', '')}",
            "",
            body or "(no description)",
        ]
    )


def assign_dev_task_from_issue(
    config: dict[str, Any] | None,
    repo_id: str,
    number: int,
    *,
    worker: str = "",
    requested_by: str = "cli",
    runner: Opener | None = None,
) -> dict[str, Any]:
    fetched = view_github_issue(config, repo_id, number, runner=runner)
    if not fetched.get("success"):
        return fetched
    issue = fetched["issue"]
    body = str(issue.get("body") or "").strip()
    if len(body) > _ISSUE_BODY_MAX_CHARS:
        body = body[:_ISSUE_BODY_MAX_CHARS]
    text = f"GitHub Issue #{issue['number']}: {issue.get('title', '')}"
    if body:
        text += f"\n\n{body}"
    return assign_dev_task(
        config,
        repo_id,
        text,
        worker=worker,
        requested_by=requested_by,
        extra_meta={"issue": int(issue["number"]), "issue_url": str(issue.get("url") or "")},
    )


def create_dev_pr(
    config: dict[str, Any] | None,
    task_id: str,
    *,
    confirm: bool = False,
    runner: Opener | None = None,
) -> dict[str, Any]:
    """Push a finished dev task's branch and create a GitHub PR.

    Without ``confirm`` this only previews the outward-facing actions
    (push, gh pr create) — both stay approval-gated per the plan.
    """
    from hermes_cli import kanban_db as kb

    clean_id = str(task_id or "").strip()
    if not clean_id:
        return {"success": False, "error": "usage: /dev pr <task_id> [--confirm]"}
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, clean_id)
    if task is None:
        return {"success": False, "error": f"task not found: {clean_id}"}
    meta = parse_dev_task_metadata(task.body)
    if meta.get("kind") != "dev_task":
        return {"success": False, "error": f"not a dev task: {clean_id}"}
    if task.status not in {"done", "review"}:
        return {"success": False, "error": f"task is not finished (status: {task.status}); run it first with /dev run"}
    if meta.get("pr"):
        return {"success": False, "error": f"PR already exists: {meta['pr']}"}
    branch = str(meta.get("branch") or "")
    worktree_path = str(meta.get("worktree_path") or "")
    if not branch or not worktree_path or not Path(worktree_path).is_dir():
        return {"success": False, "error": "task has no usable worktree; run it first with /dev run"}
    repo = get_repository(config, str(meta.get("repo_id") or ""))
    if repo is None:
        return {"success": False, "error": f"repository not found: {meta.get('repo_id')}"}
    blocked = _require_gh(repo)
    if blocked:
        return {"success": False, "error": blocked}
    base = repo.default_branch or "main"
    dev = _dev_config(config)
    commit_gated = bool(dev.get("require_approval_for_commit", False))

    def _counts() -> tuple[int, int]:
        status = _run_git(["-C", worktree_path, "status", "--porcelain"])
        uncommitted = len([line for line in (status.stdout or "").splitlines() if line.strip()])
        ahead_proc = _run_git(["-C", worktree_path, "rev-list", "--count", f"{base}..HEAD"])
        ahead = int((ahead_proc.stdout or "0").strip() or 0) if ahead_proc.returncode == 0 else 0
        return uncommitted, ahead

    uncommitted, ahead = _counts()
    if uncommitted and commit_gated:
        return {
            "success": False,
            "error": (
                f"worktree has {uncommitted} uncommitted file(s) and auto-commit is disabled "
                "(dev_orchestrator.require_approval_for_commit); commit manually, then retry"
            ),
        }
    if not uncommitted and ahead == 0:
        return {"success": False, "error": "no changes to publish (no commits ahead of base, no uncommitted files)"}

    planned = []
    if uncommitted:
        planned.append(f"commit {uncommitted} uncommitted file(s) on {branch}")
    planned.append(f"push {branch} to origin ({repo.github})")
    planned.append(f"create PR {branch} -> {base}: {task.title}")
    if not confirm:
        return {
            "success": True,
            "status": "preview",
            "task_id": clean_id,
            "planned": planned,
            "output": "\n".join(
                [
                    f"PR preview for {clean_id} ({ahead} commit(s) ahead of {base}):",
                    *[f"  - {step}" for step in planned],
                    "",
                    f"Run `/dev pr {clean_id} --confirm` to push and create the PR.",
                ]
            ),
        }

    run = runner or _run_external
    if uncommitted:
        added = _run_git(["-C", worktree_path, "add", "-A"])
        committed = _run_git(["-C", worktree_path, "commit", "-m", task.title])
        if added.returncode != 0 or committed.returncode != 0:
            detail = (committed.stderr or committed.stdout or added.stderr or "git commit failed").strip()
            return {"success": False, "error": f"failed to commit worker changes: {detail}"}
        _, ahead = _counts()
    try:
        pushed = run(["git", "-C", worktree_path, "push", "-u", "origin", branch])
    except Exception as exc:
        return {"success": False, "error": f"git push failed: {exc}"}
    if pushed.returncode != 0:
        return {"success": False, "error": (pushed.stderr or pushed.stdout or "git push failed").strip()}

    body_lines = [f"Dev task `{clean_id}` (worker={meta.get('worker') or '-'}), created by the Hermes dev orchestrator."]
    if meta.get("change_summary"):
        body_lines.append(f"\nChange summary: {meta['change_summary']}")
    if meta.get("issue"):
        body_lines.append(f"\nCloses #{meta['issue']}")
    try:
        created = run(
            [
                "gh", "-R", repo.github, "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", task.title,
                "--body", "\n".join(body_lines),
            ]
        )
    except Exception as exc:
        return {"success": False, "error": f"gh pr create failed: {exc}"}
    if created.returncode != 0:
        return {"success": False, "error": (created.stderr or created.stdout or "gh pr create failed").strip()}
    url = ""
    for line in reversed((created.stdout or "").strip().splitlines()):
        if line.strip().startswith("https://"):
            url = line.strip()
            break
    meta["pr"] = url or "(created)"
    try:
        _update_task_meta(clean_id, meta)
    except Exception:
        pass
    return {
        "success": True,
        "status": "created",
        "task_id": clean_id,
        "branch": branch,
        "base": base,
        "ahead": ahead,
        "pr_url": url,
        "output": f"PR created for {clean_id}: {url or '(no URL reported)'}",
    }


def remove_repository(
    config: dict[str, Any] | None,
    repo_id: str,
    *,
    deleter: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Unregister a repository from the config.

    Only removes the registry entry — the checkout, existing worktrees,
    and Kanban tasks are left untouched.
    """
    clean_id = str(repo_id or "").strip()
    if clean_id not in _repo_config(config):
        return {"success": False, "error": f"repository not found: {clean_id or '(empty)'}"}
    remove = deleter or delete_config_value
    ok = remove(f"repositories.{clean_id}")
    return {"success": ok, "repo_id": clean_id, "error": "" if ok else "failed to update config"}


def _vscode_command(config: dict[str, Any] | None, path: str) -> list[str]:
    dev = _dev_config(config)
    vscode = dev.get("vscode")
    vscode = vscode if isinstance(vscode, dict) else {}
    configured = str(vscode.get("command") or "").strip()
    command = configured or "code"
    if shutil.which(command):
        return [command, path]
    if platform.system() == "Darwin":
        app = str(vscode.get("fallback_macos_app") or "Visual Studio Code").strip()
        return ["open", "-a", app, path]
    return [command, path]


def open_repository(
    config: dict[str, Any] | None,
    repo_id: str,
    *,
    opener: Opener | None = None,
) -> dict[str, Any]:
    repo = get_repository(config, repo_id)
    if repo is None:
        return {"success": False, "error": f"repository not found: {repo_id}"}
    if not repo.exists:
        return {"success": False, "error": f"repository path does not exist: {repo.local_path}"}
    command = _vscode_command(config, repo.local_path)
    run = opener or _default_open
    try:
        proc = run(command)
    except Exception as exc:
        return {"success": False, "error": str(exc), "repo_id": repo.repo_id, "path": repo.local_path, "command": command}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {
            "success": False,
            "error": detail or f"open command failed with exit code {proc.returncode}",
            "repo_id": repo.repo_id,
            "path": repo.local_path,
            "command": command,
        }
    return {"success": True, "repo_id": repo.repo_id, "path": repo.local_path, "command": command}


def _default_open(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=15, check=False)


def format_open_result(result: dict[str, Any]) -> str:
    if result.get("success"):
        return f"Opened {result.get('repo_id')} in VS Code: {result.get('path')}"
    return f"Failed to open repository: {result.get('error') or 'unknown error'}"


def handle_dev_command(
    arg: str,
    *,
    config: dict[str, Any] | None,
    saver: ConfigSaver | None = None,
    opener: Opener | None = None,
    deleter: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    try:
        parts = shlex.split(str(arg or "").strip())
    except ValueError as exc:
        return {"success": False, "error": f"failed to parse /dev command: {exc}"}
    if not parts:
        parts = ["status"]
    sub = parts[0].lower()
    if sub in {"status"}:
        repos = load_repositories(config)
        ok_count = sum(1 for repo in repos if repo.exists and repo.is_git_repo)
        tasks = list_dev_tasks(config)
        return {
            "success": True,
            "output": "\n".join(
                [
                    "Development Orchestrator Status",
                    f"  Repositories: {ok_count}/{len(repos)} ready",
                    f"  Dev tasks:     {summarize_dev_tasks(tasks)}",
                    "  Task runner:   not implemented yet",
                    "  Voice notify:  not implemented yet",
                ]
            ),
        }
    if sub == "assign":
        if len(parts) < 3:
            return {
                "success": False,
                "error": "usage: /dev assign <repo_id> <task description | --issue <number>> [--worker codex|claude|hermes]",
            }
        worker = ""
        issue = ""
        words: list[str] = []
        rest = parts[2:]
        idx = 0
        while idx < len(rest):
            if rest[idx] == "--worker" and idx + 1 < len(rest):
                worker = rest[idx + 1]
                idx += 2
            elif rest[idx] == "--issue" and idx + 1 < len(rest):
                issue = rest[idx + 1]
                idx += 2
            else:
                words.append(rest[idx])
                idx += 1
        if issue:
            if not issue.isdigit():
                return {"success": False, "error": f"--issue expects a number, got: {issue}"}
            result = assign_dev_task_from_issue(config, parts[1], int(issue), worker=worker)
        else:
            result = assign_dev_task(config, parts[1], " ".join(words), worker=worker)
        if result.get("success"):
            return {
                "success": True,
                "output": (
                    f"Dev task created: {result['task_id']}\n"
                    f"  Repo:   {result['repo_id']}\n"
                    f"  Worker: {result['worker']}\n"
                    f"  Title:  {result['title']}"
                ),
            }
        return {"success": False, "error": result.get("error") or "failed to create dev task"}
    if sub == "tasks":
        repo_filter = parts[1] if len(parts) > 1 else ""
        if repo_filter and get_repository(config, repo_filter) is None:
            return {"success": False, "error": f"repository not found: {repo_filter}"}
        items = list_dev_tasks(config, repo_id=repo_filter)
        return {"success": True, "output": format_dev_tasks(items, repo_filter)}
    if sub == "run":
        if len(parts) < 2:
            return {"success": False, "error": "usage: /dev run <task_id> [--wait]"}
        if "--wait" in parts[2:]:
            result = run_dev_task(config, parts[1])
            return {
                "success": bool(result.get("success")),
                "output": format_run_result(result),
                "error": result.get("error"),
            }
        result = start_dev_task(config, parts[1])
        if not result.get("success"):
            return {"success": False, "error": result.get("error")}
        return {
            "success": True,
            "output": "\n".join(
                [
                    f"Dev task started: {result['task_id']}",
                    f"  Worker:   {result['worker']}",
                    f"  Branch:   {result['branch']}",
                    f"  Worktree: {result['worktree_path']}",
                    "  Progress: voice notifications + /dev tasks (done/blocked when finished)",
                ]
            ),
        }
    if sub == "stop":
        if len(parts) < 2:
            return {"success": False, "error": "usage: /dev stop <task_id>"}
        result = stop_dev_task(config, parts[1])
        return {
            "success": bool(result.get("success")),
            "output": str(result.get("output") or ""),
            "error": result.get("error"),
        }
    if sub == "issue":
        if len(parts) < 2:
            return {"success": False, "error": "usage: /dev issue <repo_id> [list|<number>]"}
        selector = parts[2] if len(parts) > 2 else "list"
        if selector == "list":
            result = list_github_issues(config, parts[1])
            if result.get("success"):
                return {"success": True, "output": format_issues(result["issues"], parts[1])}
            return {"success": False, "error": result.get("error")}
        if selector.lstrip("#").isdigit():
            result = view_github_issue(config, parts[1], int(selector.lstrip("#")))
            if result.get("success"):
                return {"success": True, "output": format_issue(result["issue"])}
            return {"success": False, "error": result.get("error")}
        return {"success": False, "error": "usage: /dev issue <repo_id> [list|<number>]"}
    if sub == "pr":
        if len(parts) < 2:
            return {"success": False, "error": "usage: /dev pr <task_id> [--confirm]"}
        confirm = "--confirm" in parts[2:] or "--yes" in parts[2:]
        result = create_dev_pr(config, parts[1], confirm=confirm)
        if result.get("success"):
            return {"success": True, "output": str(result.get("output") or "")}
        return {"success": False, "error": result.get("error")}
    if sub in {"repos", "repositories"}:
        return {"success": True, "output": format_repositories(load_repositories(config))}
    if sub == "repo":
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action in {"list", "repos"}:
            return {"success": True, "output": format_repositories(load_repositories(config))}
        if action == "show" and len(parts) >= 3:
            return {"success": True, "output": format_repository(get_repository(config, parts[2]), parts[2])}
        if action in {"remove", "rm", "delete"} and len(parts) >= 3:
            result = remove_repository(config, parts[2], deleter=deleter)
            if result.get("success"):
                return {
                    "success": True,
                    "output": (
                        f"Repository removed from registry: {result['repo_id']}\n"
                        "  (checkout, worktrees, and tasks are left untouched)"
                    ),
                }
            return {"success": False, "error": result.get("error") or "failed to remove repository"}
        if action == "add" and len(parts) >= 4:
            github = ""
            default_branch = ""
            worker = ""
            rest = parts[4:]
            idx = 0
            while idx < len(rest):
                key = rest[idx]
                value = rest[idx + 1] if idx + 1 < len(rest) else ""
                if key == "--github":
                    github = value
                    idx += 2
                elif key == "--default-branch":
                    default_branch = value
                    idx += 2
                elif key == "--worker":
                    worker = value
                    idx += 2
                else:
                    idx += 1
            result = add_repository(parts[2], parts[3], github=github, default_branch=default_branch, worker=worker, saver=saver)
            if result.get("success"):
                return {"success": True, "output": f"Repository added: {result['repo_id']}"}
            return {"success": False, "error": result.get("error") or "failed to add repository"}
        return {
            "success": False,
            "error": (
                "usage: /dev repo [list|show <repo_id>|add <repo_id> <local_path> "
                "[--github owner/repo] [--worker codex|claude|hermes]|remove <repo_id>]"
            ),
        }
    if sub == "open":
        if len(parts) < 2:
            return {"success": False, "error": "usage: /dev open <repo_id>"}
        result = open_repository(config, parts[1], opener=opener)
        return {"success": bool(result.get("success")), "output": format_open_result(result), "error": result.get("error")}
    return {"success": False, "error": "usage: /dev [status|repos|repo show|repo add|repo remove|assign|tasks|run|stop|issue|pr|open]"}
