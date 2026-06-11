import subprocess

import pytest

from hermes_cli.dev_orchestrator import (
    add_repository,
    assign_dev_task,
    assign_dev_task_from_issue,
    create_dev_pr,
    create_task_worktree,
    format_repositories,
    get_repository,
    handle_dev_command,
    list_dev_tasks,
    load_repositories,
    open_repository,
    parse_dev_task_metadata,
    run_dev_task,
)


def _git(repo_path, *args):
    subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(repo_path):
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path.parent, "init", "-q", "-b", "main", str(repo_path))
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    (repo_path / "README.md").write_text("hello\n")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-q", "-m", "init")


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME plus one registered git repo."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    repo_path = tmp_path / "kai"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    config = {
        "dev_orchestrator": {"default_worker": "codex"},
        "repositories": {"kai": {"local_path": str(repo_path)}},
    }
    return config


def test_load_repositories_reads_config_and_expands_paths(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()

    config = {
        "dev_orchestrator": {
            "default_worker": "codex",
            "worktree_root": str(tmp_path / "worktrees"),
        },
        "repositories": {
            "kai": {
                "local_path": str(repo_path),
                "github": "seiichi3141/kai",
                "default_branch": "main",
            }
        },
    }

    repos = load_repositories(config)

    assert len(repos) == 1
    assert repos[0].repo_id == "kai"
    assert repos[0].exists is True
    assert repos[0].is_git_repo is True
    assert repos[0].worker == "codex"
    assert repos[0].worktree_root.endswith("/worktrees/kai")
    assert "kai" in format_repositories(repos)


def test_add_repository_uses_config_saver(tmp_path):
    calls = []

    result = add_repository(
        "hermes-agent",
        str(tmp_path / "hermes-agent"),
        github="seiichi3141/hermes-agent",
        saver=lambda key, value: calls.append((key, value)) or True,
    )

    assert result["success"] is True
    assert calls == [
        (
            "repositories.hermes-agent",
            {
                "local_path": str(tmp_path / "hermes-agent"),
                "github": "seiichi3141/hermes-agent",
            },
        )
    ]


def test_add_repository_rejects_dot_in_repo_id(tmp_path):
    result = add_repository("bad.repo", str(tmp_path), saver=lambda _key, _value: True)

    assert result["success"] is False


def test_add_repository_normalizes_claude_code_worker(tmp_path):
    calls = []

    result = add_repository(
        "kai",
        str(tmp_path / "kai"),
        worker="Claude_Code",
        saver=lambda key, value: calls.append((key, value)) or True,
    )

    assert result["success"] is True
    assert calls[0][1]["worker"] == "claude"


def test_add_repository_rejects_unknown_worker(tmp_path):
    result = add_repository(
        "kai",
        str(tmp_path / "kai"),
        worker="copilot",
        saver=lambda _key, _value: True,
    )

    assert result["success"] is False
    assert "unknown worker" in result["error"]


def test_load_repositories_defaults_without_dev_section(tmp_path, monkeypatch):
    """The TUI gateway passes raw config.yaml (no DEFAULT_CONFIG merge);
    worktree_root and worker must still resolve (#regression: /dev run
    failed with 'no worktree_root configured')."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()

    repos = load_repositories({"repositories": {"repo": {"local_path": str(repo_path)}}})

    assert repos[0].worker == "codex"
    assert repos[0].worktree_root == str(home / "dev-worktrees" / "repo")


def test_load_repositories_normalizes_worker_aliases(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    config = {
        "dev_orchestrator": {"default_worker": "claude-code"},
        "repositories": {"repo": {"local_path": str(repo_path)}},
    }

    repos = load_repositories(config)

    assert repos[0].worker == "claude"


def test_open_repository_uses_configured_code_command(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    calls = []
    config = {
        "dev_orchestrator": {"vscode": {"command": "definitely-missing-code-command"}},
        "repositories": {"repo": {"local_path": str(repo_path)}},
    }

    def opener(command):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = open_repository(config, "repo", opener=opener)

    assert result["success"] is True
    assert calls
    assert calls[0][-1] == str(repo_path)


def test_handle_dev_command_repos_and_repo_add(tmp_path):
    calls = []
    config = {"repositories": {}}

    empty = handle_dev_command("repos", config=config)
    added = handle_dev_command(
        f"repo add kai {tmp_path / 'kai'} --github seiichi3141/kai",
        config=config,
        saver=lambda key, value: calls.append((key, value)) or True,
    )

    assert empty["success"] is True
    assert "(none configured)" in empty["output"]
    assert added["success"] is True
    assert calls[0][0] == "repositories.kai"
    assert calls[0][1]["github"] == "seiichi3141/kai"


def test_handle_dev_command_repo_add_accepts_quoted_path(tmp_path):
    calls = []
    repo_path = tmp_path / "repo with spaces"

    result = handle_dev_command(
        f'repo add spaced "{repo_path}" --github seiichi3141/spaced',
        config={"repositories": {}},
        saver=lambda key, value: calls.append((key, value)) or True,
    )

    assert result["success"] is True
    assert calls[0][0] == "repositories.spaced"
    assert calls[0][1]["local_path"] == str(repo_path)


def test_get_repository_returns_none_for_unknown():
    assert get_repository({"repositories": {}}, "missing") is None


def test_assign_dev_task_creates_kanban_task_with_metadata(dev_env):
    result = assign_dev_task(dev_env, "kai", "Fix the AquesTalk reading bug", worker="claude")

    assert result["success"] is True
    assert result["worker"] == "claude"

    items = list_dev_tasks(dev_env)
    assert len(items) == 1
    assert items[0]["task_id"] == result["task_id"]
    assert items[0]["repo_id"] == "kai"
    assert items[0]["worker"] == "claude"
    assert items[0]["status"] == "ready"

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task.tenant == "dev"
    assert task.assignee == "claude"
    meta = parse_dev_task_metadata(task.body)
    assert meta["kind"] == "dev_task"
    assert meta["repo_id"] == "kai"
    assert "Fix the AquesTalk reading bug" in task.body


def test_assign_dev_task_rejects_unknown_repo_and_missing_text(dev_env):
    missing_repo = assign_dev_task(dev_env, "nope", "Fix something")
    empty_task = assign_dev_task(dev_env, "kai", "   ")

    assert missing_repo["success"] is False
    assert "repository not found" in missing_repo["error"]
    assert empty_task["success"] is False
    assert "task description is required" in empty_task["error"]


def test_list_dev_tasks_filters_by_repo(dev_env, tmp_path):
    other_path = tmp_path / "other"
    other_path.mkdir()
    (other_path / ".git").mkdir()
    dev_env["repositories"]["other"] = {"local_path": str(other_path)}

    assign_dev_task(dev_env, "kai", "Task for kai")
    assign_dev_task(dev_env, "other", "Task for other")

    assert len(list_dev_tasks(dev_env)) == 2
    kai_items = list_dev_tasks(dev_env, repo_id="kai")
    assert len(kai_items) == 1
    assert kai_items[0]["repo_id"] == "kai"


def test_list_dev_tasks_hides_old_done_tasks(worker_env):
    import time

    from hermes_cli import kanban_db as kb

    old_done = assign_dev_task(worker_env, "proj", "Old finished work")["task_id"]
    old_blocked = assign_dev_task(worker_env, "proj", "Old blocked work")["task_id"]
    fresh = assign_dev_task(worker_env, "proj", "Fresh work")["task_id"]
    three_hours_ago = int(time.time()) - 3 * 3600
    with kb.connect_closing() as conn:
        kb.complete_task(conn, old_done, result="done")
        kb.complete_task(conn, fresh, result="done")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (three_hours_ago, old_done))
        assert kb.claim_task(conn, old_blocked, claimer="x") is not None
        kb.block_task(conn, old_blocked, reason="needs input")

    default_ids = {t["task_id"] for t in list_dev_tasks(worker_env)}
    all_ids = {t["task_id"] for t in list_dev_tasks(worker_env, include_all=True)}

    assert fresh in default_ids
    assert old_blocked in default_ids  # blocked needs attention, never hidden
    assert old_done not in default_ids
    assert old_done in all_ids

    listed = handle_dev_command("tasks", config=worker_env)
    listed_all = handle_dev_command("tasks --all", config=worker_env)
    assert "Old finished work" not in listed["output"]
    assert "are hidden" in listed["output"]
    assert "Old finished work" in listed_all["output"]


def test_handle_dev_command_assign_and_tasks(dev_env):
    created = handle_dev_command(
        "assign kai Fix the overlay flicker --worker claude_code",
        config=dev_env,
    )
    listed = handle_dev_command("tasks kai", config=dev_env)
    status = handle_dev_command("status", config=dev_env)
    unknown = handle_dev_command("tasks nope", config=dev_env)

    assert created["success"] is True
    assert "Worker: claude" in created["output"]
    assert listed["success"] is True
    assert "Fix the overlay flicker" in listed["output"]
    assert "1 total (1 ready)" in status["output"]
    assert unknown["success"] is False


def test_parse_dev_task_metadata_is_shape_safe():
    assert parse_dev_task_metadata(None) == {}
    assert parse_dev_task_metadata("no fence here") == {}
    assert parse_dev_task_metadata("```dev-task-meta\nnot json\n```") == {}


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME plus a real git repo registered as 'proj'."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    repo_path = tmp_path / "proj"
    _init_git_repo(repo_path)
    config = {
        "dev_orchestrator": {
            "default_worker": "claude",
            "worktree_root": str(tmp_path / "worktrees"),
        },
        "repositories": {"proj": {"local_path": str(repo_path)}},
    }
    return config


def test_create_task_worktree_creates_branch_and_dir(worker_env):
    repo = get_repository(worker_env, "proj")

    result = create_task_worktree(repo, "t_abc123")

    assert result["success"] is True
    assert result["branch"] == "dev/t_abc123"
    worktree = subprocess.run(
        ["git", "-C", result["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert worktree.stdout.strip() == "dev/t_abc123"

    again = create_task_worktree(repo, "t_abc123")
    assert again["success"] is True
    assert again["reused"] is True


def test_run_dev_task_completes_task_with_change_summary(worker_env):
    created = assign_dev_task(worker_env, "proj", "Add a feature file")
    task_id = created["task_id"]

    def fake_worker(command):
        assert command[0] == "claude"
        return subprocess.CompletedProcess(command, 0, stdout="did the work\n", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/claude")
        result = run_dev_task(worker_env, task_id, runner=fake_worker)

    assert result["success"] is True
    assert result["status"] == "done"
    assert result["branch"] == f"dev/{task_id}"

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task.status == "done"
    meta = parse_dev_task_metadata(task.body)
    assert meta["branch"] == f"dev/{task_id}"
    assert meta["worktree_path"] == result["worktree_path"]


def test_run_dev_task_blocks_on_worker_failure(worker_env):
    created = assign_dev_task(worker_env, "proj", "Break something")
    task_id = created["task_id"]

    def fake_worker(command):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="boom API_KEY=secret123456")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/claude")
        result = run_dev_task(worker_env, task_id, runner=fake_worker)

    assert result["success"] is False
    assert result["status"] == "blocked"
    assert "secret123456" not in result["output"]

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task.status == "blocked"


def test_run_dev_task_validates_task_and_status(worker_env):
    missing = run_dev_task(worker_env, "t_nope")
    assert missing["success"] is False
    assert "task not found" in missing["error"]

    created = assign_dev_task(worker_env, "proj", "Run twice")
    task_id = created["task_id"]

    def fake_worker(command):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/claude")
        first = run_dev_task(worker_env, task_id, runner=fake_worker)
        second = run_dev_task(worker_env, task_id, runner=fake_worker)

    assert first["success"] is True
    assert second["success"] is False
    assert "not ready" in second["error"]


def test_start_dev_task_returns_immediately_and_completes_in_background(worker_env):
    from hermes_cli.dev_orchestrator import start_dev_task

    created = assign_dev_task(worker_env, "proj", "Background work")
    task_id = created["task_id"]

    def fake_worker(command):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        result = start_dev_task(worker_env, task_id, runner=fake_worker)
        assert result["success"] is True
        assert result["status"] == "started"
        result["thread"].join(timeout=30)

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task.status == "done"


def test_handle_dev_command_run_is_async_by_default(worker_env):
    created = assign_dev_task(worker_env, "proj", "Async via command")

    def fake_worker(command):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        mp.setattr("hermes_cli.dev_orchestrator._default_worker_run", lambda *a, **k: fake_worker(a[0]))
        result = handle_dev_command(f"run {created['task_id']}", config=worker_env)

        import time

        from hermes_cli import kanban_db as kb

        deadline = time.time() + 30
        status = "running"
        while time.time() < deadline and status == "running":
            with kb.connect_closing() as conn:
                status = kb.get_task(conn, created["task_id"]).status
            time.sleep(0.05)

    assert result["success"] is True
    assert "Dev task started" in result["output"]
    assert status == "done"


def test_remove_repository_via_command(tmp_path):
    deleted = []
    config = {"repositories": {"kai": {"local_path": str(tmp_path)}}}
    deleter = lambda key: deleted.append(key) or True  # noqa: E731

    removed = handle_dev_command("repo remove kai", config=config, deleter=deleter)
    missing = handle_dev_command("repo remove nope", config=config, deleter=deleter)

    assert removed["success"] is True
    assert "Repository removed" in removed["output"]
    assert deleted == ["repositories.kai"]
    assert missing["success"] is False
    assert "repository not found" in missing["error"]


def test_atomic_roundtrip_yaml_delete(tmp_path):
    from utils import atomic_roundtrip_yaml_update

    path = tmp_path / "config.yaml"
    path.write_text("# keep this comment\nrepositories:\n  kai:\n    local_path: /x\n  other:\n    local_path: /y\n")

    atomic_roundtrip_yaml_update(path, "repositories.kai", None, delete=True)
    atomic_roundtrip_yaml_update(path, "repositories.missing", None, delete=True)
    atomic_roundtrip_yaml_update(path, "nope.deep.key", None, delete=True)

    text = path.read_text()
    assert "keep this comment" in text
    assert "kai" not in text
    assert "other" in text


def test_stop_dev_task_blocks_claimed_task_without_live_worker(worker_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli.dev_orchestrator import stop_dev_task

    created = assign_dev_task(worker_env, "proj", "Long running work")
    task_id = created["task_id"]
    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, task_id, claimer="other-process") is not None

    result = stop_dev_task(worker_env, task_id)

    assert result["success"] is True
    assert result["status"] == "blocked"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"

    not_running = stop_dev_task(worker_env, task_id)
    assert not_running["success"] is False
    assert "not running" in not_running["error"]


def test_stop_dev_task_terminates_live_worker(worker_env):
    import hermes_cli.dev_orchestrator as devmod

    class FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    fake = FakeProc()
    with devmod._workers_lock:
        devmod._RUNNING_WORKERS["t_live"] = fake
    try:
        result = devmod.stop_dev_task(worker_env, "t_live")
    finally:
        with devmod._workers_lock:
            devmod._RUNNING_WORKERS.pop("t_live", None)
            stopped = "t_live" in devmod._STOPPED_TASKS
            devmod._STOPPED_TASKS.discard("t_live")

    assert result["success"] is True
    assert result["status"] == "stopping"
    assert fake.terminated is True
    assert stopped is True


def test_run_dev_task_rejects_hermes_worker(worker_env):
    created = assign_dev_task(worker_env, "proj", "Use hermes lane", worker="hermes")

    result = run_dev_task(worker_env, created["task_id"])

    assert result["success"] is False
    assert "not implemented" in result["error"]


def _with_github(config):
    for repo in config["repositories"].values():
        repo["github"] = "seiichi3141/proj"
    return config


def test_assign_dev_task_from_issue_embeds_issue_metadata(worker_env):
    config = _with_github(worker_env)

    def fake_gh(command):
        assert command[:2] == ["gh", "-R"]
        assert "view" in command
        payload = '{"number": 42, "title": "Fix TTS crash", "body": "It crashes.", "state": "OPEN", "url": "https://github.com/seiichi3141/proj/issues/42"}'
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = assign_dev_task_from_issue(config, "proj", 42, runner=fake_gh)

    assert result["success"] is True
    items = list_dev_tasks(config)
    assert items[0]["issue"] == 42
    assert "Fix TTS crash" in items[0]["title"]


def _finished_task_with_commit(config, text="Publish me"):
    created = assign_dev_task(config, "proj", text)
    task_id = created["task_id"]

    def fake_worker(command):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        run_result = run_dev_task(config, task_id, runner=fake_worker)
    worktree = run_result["worktree_path"]
    from pathlib import Path

    Path(worktree, "feature.txt").write_text("new\n")
    _git(Path(worktree), "add", "feature.txt")
    _git(Path(worktree), "commit", "-q", "-m", "add feature")
    return task_id, worktree


def test_create_dev_pr_preview_requires_confirm(worker_env):
    config = _with_github(worker_env)
    task_id, _ = _finished_task_with_commit(config)

    calls = []

    def fake_external(command):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = create_dev_pr(config, task_id, confirm=False, runner=fake_external)

    assert result["success"] is True
    assert result["status"] == "preview"
    assert "--confirm" in result["output"]
    assert calls == []  # no outward-facing command without confirm


def test_create_dev_pr_confirm_pushes_and_saves_url(worker_env):
    config = _with_github(worker_env)
    task_id, _ = _finished_task_with_commit(config)

    calls = []

    def fake_external(command):
        calls.append(command)
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/seiichi3141/proj/pull/7\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = create_dev_pr(config, task_id, confirm=True, runner=fake_external)

    assert result["success"] is True
    assert result["pr_url"] == "https://github.com/seiichi3141/proj/pull/7"
    assert calls[0][:2] == ["git", "-C"]
    assert "push" in calls[0]
    assert calls[1][0] == "gh"

    items = list_dev_tasks(config)
    assert items[0]["pr"] == "https://github.com/seiichi3141/proj/pull/7"

    again = create_dev_pr(config, task_id, confirm=True, runner=fake_external)
    assert again["success"] is False
    assert "PR already exists" in again["error"]


def test_create_dev_pr_adopts_existing_pr(worker_env):
    config = _with_github(worker_env)
    task_id, _ = _finished_task_with_commit(config)

    def fake_external(command):
        if command[0] == "gh":
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr='a pull request for branch "dev/x" into branch "main" already exists:\nhttps://github.com/seiichi3141/proj/pull/42',
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = create_dev_pr(config, task_id, confirm=True, runner=fake_external)

    assert result["success"] is True
    assert result["status"] == "exists"
    assert result["pr_url"] == "https://github.com/seiichi3141/proj/pull/42"
    assert list_dev_tasks(config)[0]["pr"] == "https://github.com/seiichi3141/proj/pull/42"


def test_create_dev_pr_blocks_uncommitted_when_commit_gated(worker_env):
    config = _with_github(worker_env)
    config["dev_orchestrator"]["require_approval_for_commit"] = True
    task_id, worktree = _finished_task_with_commit(config)
    from pathlib import Path

    Path(worktree, "dirty.txt").write_text("dirty\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = create_dev_pr(config, task_id, confirm=True, runner=lambda c: pytest.fail("must not run external commands"))

    assert result["success"] is False
    assert "uncommitted" in result["error"]


def test_run_dev_task_auto_creates_pr_when_enabled(worker_env, tmp_path):
    config = _with_github(worker_env)
    config["dev_orchestrator"]["auto_create_pr"] = True
    created = assign_dev_task(config, "proj", "Ship it")
    task_id = created["task_id"]
    worktree = tmp_path / "worktrees" / "proj" / task_id
    calls = []

    def fake_runner(command):
        calls.append(command)
        if command[0] == "claude":
            from pathlib import Path

            Path(worktree, "feature.txt").write_text("new\n")
            _git(worktree, "add", "feature.txt")
            _git(worktree, "commit", "-q", "-m", "work")
            return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/seiichi3141/proj/pull/9\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        result = run_dev_task(config, task_id, runner=fake_runner)

    assert result["success"] is True
    assert result["pr_success"] is True
    assert result["pr_url"] == "https://github.com/seiichi3141/proj/pull/9"
    items = list_dev_tasks(config)
    assert items[0]["pr"] == "https://github.com/seiichi3141/proj/pull/9"


def test_run_dev_task_reports_auto_pr_failure_without_failing_task(worker_env):
    config = _with_github(worker_env)
    config["dev_orchestrator"]["auto_create_pr"] = True
    created = assign_dev_task(config, "proj", "No changes made")

    def fake_runner(command):
        return subprocess.CompletedProcess(command, 0, stdout="nothing to do", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        result = run_dev_task(config, created["task_id"], runner=fake_runner)

    assert result["success"] is True  # task itself is done
    assert result["pr_success"] is False
    assert "no changes to publish" in result["pr_error"]


def test_run_dev_task_skips_pr_by_default(worker_env):
    created = assign_dev_task(worker_env, "proj", "No auto pr")

    def fake_runner(command):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/x")
        result = run_dev_task(worker_env, created["task_id"], runner=fake_runner)

    assert result["success"] is True
    assert "pr_success" not in result


def test_create_dev_pr_rejects_unfinished_task(worker_env):
    config = _with_github(worker_env)
    created = assign_dev_task(config, "proj", "Not run yet")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hermes_cli.dev_orchestrator.shutil.which", lambda _name: "/usr/bin/gh")
        result = create_dev_pr(config, created["task_id"], confirm=True)

    assert result["success"] is False
    assert "not finished" in result["error"]
