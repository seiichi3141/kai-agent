"""Agent tools for the dev orchestrator.

Lets the chat agent (including voice chat) manage development tasks:
check repository/task status, assign new tasks, and start workers.
Outward-facing GitHub actions (push, PR create) stay slash-command
only because they need the explicit /dev pr --confirm approval step.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def check_dev_requirements() -> bool:
    try:
        config = _load_config()
        dev = config.get("dev_orchestrator")
        dev = dev if isinstance(dev, dict) else {}
        if not dev.get("enabled", True):
            return False
        repos = config.get("repositories")
        return isinstance(repos, dict) and bool(repos)
    except Exception:
        return False


def dev_status_tool(repo_id: str = "") -> str:
    try:
        from hermes_cli.dev_orchestrator import list_dev_tasks, load_repositories

        config = _load_config()
        repos = [
            {
                "repo_id": r.repo_id,
                "local_path": r.local_path,
                "github": r.github,
                "worker": r.worker,
                "ready": r.exists and r.is_git_repo,
            }
            for r in load_repositories(config)
            if not repo_id or r.repo_id == repo_id
        ]
        tasks = list_dev_tasks(config, repo_id=repo_id, limit=20)
        return json.dumps({"success": True, "repositories": repos, "tasks": tasks}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def dev_assign_tool(repo_id: str, task: str, *, worker: str = "", issue: int | None = None) -> str:
    try:
        from hermes_cli.dev_orchestrator import assign_dev_task, assign_dev_task_from_issue

        config = _load_config()
        if issue:
            result = assign_dev_task_from_issue(
                config, repo_id, int(issue), worker=worker, requested_by="agent"
            )
        else:
            result = assign_dev_task(config, repo_id, task, worker=worker, requested_by="agent")
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def dev_run_tool(task_id: str) -> str:
    try:
        from hermes_cli.dev_orchestrator import start_dev_task

        result = start_dev_task(_load_config(), task_id)
        result.pop("thread", None)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


registry.register(
    name="dev_status",
    toolset="dev",
    schema={
        "name": "dev_status",
        "description": (
            "Show development orchestrator status: registered repositories and "
            "their dev tasks (id, status, worker, branch, PR). Use when the user "
            "asks about development tasks, 開発タスク, repo status, or what a "
            "worker is doing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Optional repository id to filter by (e.g. 'aozora').",
                },
            },
            "required": [],
        },
    },
    handler=lambda args, **kw: dev_status_tool(repo_id=args.get("repo_id", "")),
    check_fn=check_dev_requirements,
)

registry.register(
    name="dev_assign",
    toolset="dev",
    schema={
        "name": "dev_assign",
        "description": (
            "Create a development task for a registered repository. The task is "
            "tracked on the Kanban board and later executed by a coding CLI "
            "worker (codex or claude) in an isolated git worktree. Use when the "
            "user asks to fix/implement something in one of their repositories. "
            "After assigning, call dev_run to actually start the work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository id from dev_status (e.g. 'aozora').",
                },
                "task": {
                    "type": "string",
                    "description": "Concrete task description for the worker (what to change, where, constraints).",
                },
                "worker": {
                    "type": "string",
                    "description": "Optional worker override: codex | claude.",
                },
                "issue": {
                    "type": "integer",
                    "description": "Optional GitHub issue number to create the task from (task text is then ignored).",
                },
            },
            "required": ["repo_id", "task"],
        },
    },
    handler=lambda args, **kw: dev_assign_tool(
        args.get("repo_id", ""),
        args.get("task", ""),
        worker=args.get("worker", ""),
        issue=args.get("issue"),
    ),
    check_fn=check_dev_requirements,
)

def dev_stop_tool(task_id: str) -> str:
    try:
        from hermes_cli.dev_orchestrator import stop_dev_task

        return json.dumps(stop_dev_task(_load_config(), task_id), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


registry.register(
    name="dev_stop",
    toolset="dev",
    schema={
        "name": "dev_stop",
        "description": (
            "Stop a running dev task: terminates its coding worker and marks "
            "the task blocked (stopped by user). Use when the user asks to "
            "stop, cancel, or abort a development task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Dev task id of the running task.",
                },
            },
            "required": ["task_id"],
        },
    },
    handler=lambda args, **kw: dev_stop_tool(args.get("task_id", "")),
    check_fn=check_dev_requirements,
)


registry.register(
    name="dev_run",
    toolset="dev",
    schema={
        "name": "dev_run",
        "description": (
            "Start the coding worker for a ready dev task (from dev_assign or "
            "dev_status). Runs in the background in a dedicated git worktree and "
            "returns immediately; completion or blockage is announced by voice "
            "notifications and visible via dev_status. Never pushes or creates PRs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Dev task id (e.g. 't_35a48d8f').",
                },
            },
            "required": ["task_id"],
        },
    },
    handler=lambda args, **kw: dev_run_tool(args.get("task_id", "")),
    check_fn=check_dev_requirements,
)
