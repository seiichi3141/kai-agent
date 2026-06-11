import subprocess

from hermes_cli.dev_orchestrator import (
    add_repository,
    format_repositories,
    get_repository,
    handle_dev_command,
    load_repositories,
    open_repository,
)


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
