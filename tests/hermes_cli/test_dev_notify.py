import subprocess

import pytest

from hermes_cli.dev_notify import (
    DevVoiceNotifier,
    VoiceNotifyConfig,
    collect_dev_notifications,
    compose_notification,
    load_voice_notify_config,
    start_dev_voice_watcher,
)
from hermes_cli.dev_orchestrator import assign_dev_task, run_dev_task


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def notify_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    repo_path = tmp_path / "proj"
    repo_path.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(repo_path))
    _git(repo_path, "config", "user.email", "t@e.com")
    _git(repo_path, "config", "user.name", "T")
    (repo_path / "README.md").write_text("hi\n")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-q", "-m", "init")
    return {
        "dev_orchestrator": {
            "default_worker": "claude",
            "worktree_root": str(tmp_path / "worktrees"),
        },
        "repositories": {"proj": {"local_path": str(repo_path)}},
    }


def _run_task(config, text="Do work", returncode=0):
    created = assign_dev_task(config, "proj", text)

    def fake_worker(command):
        return subprocess.CompletedProcess(command, returncode, stdout="out", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        run_dev_task(config, created["task_id"], runner=fake_worker)
    return created["task_id"]


def test_collect_reports_started_and_completed_then_advances_cursor(notify_env):
    task_id = _run_task(notify_env)

    first = collect_dev_notifications(notify_env)
    second = collect_dev_notifications(notify_env)

    kinds = [n["kind"] for n in first if n["task_id"] == task_id]
    assert kinds == ["claimed", "completed"]
    assert all("proj" in n["text"] for n in first)
    assert second == []


def test_collect_reports_blocked_with_reason(notify_env):
    task_id = _run_task(notify_env, returncode=3)

    notifications = collect_dev_notifications(notify_env)

    blocked = [n for n in notifications if n["kind"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["task_id"] == task_id
    assert "blocked" in blocked[0]["text"]
    assert "exit code 3" in blocked[0]["text"]


def test_collect_respects_report_flags_but_advances_cursor(notify_env):
    notify_env["dev_orchestrator"]["voice_notifications"] = {"report_started": False}
    _run_task(notify_env)

    first = collect_dev_notifications(notify_env)
    second = collect_dev_notifications(notify_env)

    assert [n["kind"] for n in first] == ["completed"]
    assert second == []


def test_compose_notification_caps_length():
    cfg = VoiceNotifyConfig(max_chars=30)

    text = compose_notification("blocked", "proj", {"reason": "x" * 200}, cfg)

    assert len(text) <= 30
    assert text.endswith("…")


def test_deliver_applies_cooldown_and_batch_dedupe():
    spoken = []
    shown = []
    now = [100.0]
    notifier = DevVoiceNotifier(
        {"dev_orchestrator": {"voice_notifications": {"cooldown_seconds": 10}}},
        speaker=spoken.append,
        overlay=shown.append,
        clock=lambda: now[0],
    )
    batch = [
        {"task_id": "t1", "repo_id": "proj", "kind": "claimed", "event_id": 1, "text": "開始"},
        {"task_id": "t1", "repo_id": "proj", "kind": "completed", "event_id": 2, "text": "完了"},
    ]

    first = notifier.deliver(batch)
    now[0] += 5.0
    second = notifier.deliver([{"task_id": "t1", "repo_id": "proj", "kind": "blocked", "event_id": 3, "text": "再度"}])
    now[0] += 10.0
    third = notifier.deliver([{"task_id": "t1", "repo_id": "proj", "kind": "blocked", "event_id": 4, "text": "三回目"}])

    assert [n["text"] for n in first] == ["完了"]  # batch dedupe keeps the last
    assert second == []  # within cooldown
    assert [n["text"] for n in third] == ["三回目"]
    assert spoken == ["完了", "三回目"]
    assert shown == ["完了", "三回目"]


def test_deliver_disabled_returns_nothing():
    notifier = DevVoiceNotifier(
        {"dev_orchestrator": {"voice_notifications": {"enabled": False}}},
        speaker=lambda _t: pytest.fail("must not speak"),
        overlay=lambda _t: pytest.fail("must not display"),
    )

    assert notifier.deliver([{"task_id": "t1", "repo_id": "p", "kind": "completed", "event_id": 1, "text": "x"}]) == []


def test_start_dev_voice_watcher_disabled_returns_none():
    assert start_dev_voice_watcher({"dev_orchestrator": {"voice_notifications": {"enabled": False}}}) is None
    assert start_dev_voice_watcher({"dev_orchestrator": {"enabled": False}}) is None


def test_load_voice_notify_config_is_shape_safe():
    cfg = load_voice_notify_config(
        {
            "dev_orchestrator": {
                "voice_notifications": {
                    "enabled": "true",
                    "max_chars": "80",
                    "cooldown_seconds": -5,
                    "report_blocked": 0,
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.max_chars == 80
    assert cfg.cooldown_seconds == 10
    assert cfg.report_blocked is False
