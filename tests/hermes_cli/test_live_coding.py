import subprocess
from pathlib import Path
from unittest.mock import patch

from hermes_cli.live_coding import (
    build_delegate_command,
    check_delegate_available,
    load_live_coding_config,
    run_delegate,
    sanitize_for_stream,
)


def test_load_live_coding_config_is_shape_safe():
    cfg = load_live_coding_config(
        {
            "stream_assistant": {
                "coding": {
                    "delegate_to": "CODEX",
                    "codex_path": "/opt/bin/codex",
                    "timeout_seconds": "120",
                    "max_output_chars": "2000",
                    "allow_file_edits": False,
                }
            }
        }
    )

    assert cfg.delegate_to == "codex"
    assert cfg.codex_path == "/opt/bin/codex"
    assert cfg.claude_path == "claude"
    assert cfg.claude_permission_mode == "acceptEdits"
    assert cfg.timeout_seconds == 120
    assert cfg.max_output_chars == 2000
    assert cfg.allow_file_edits is False


def test_load_live_coding_config_normalizes_claude_aliases():
    cfg = load_live_coding_config(
        {
            "stream_assistant": {
                "coding": {
                    "delegate_to": "Claude_Code",
                    "claude_path": "/opt/bin/claude",
                    "claude_permission_mode": "",
                }
            }
        }
    )

    assert cfg.delegate_to == "claude"
    assert cfg.claude_path == "/opt/bin/claude"
    assert cfg.claude_permission_mode == ""


def test_sanitize_for_stream_redacts_secrets_and_shortens_paths():
    text = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz\n"
        "/Users/seiichiro/apps/project/.env"
    )

    sanitized = sanitize_for_stream(text)

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "OPENAI_API_KEY=<redacted>" in sanitized
    assert "/Users/seiichiro" not in sanitized
    assert "<path>/project/.env" in sanitized


def test_build_delegate_command_adds_live_stream_safety_rules():
    cfg = load_live_coding_config({})

    command = build_delegate_command("Fix the login button", cfg)

    assert command[:3] == ["codex", "exec", "--"]
    prompt = command[3]
    assert not prompt.startswith("-")
    assert "Fix the login button" in prompt
    assert "Do not reveal secrets" in prompt
    assert "Do not run git commit or git push" in prompt


def test_build_delegate_command_for_claude_uses_headless_print_mode():
    cfg = load_live_coding_config(
        {"stream_assistant": {"coding": {"delegate_to": "claude"}}}
    )

    command = build_delegate_command("Fix the login button", cfg)

    assert command[:5] == ["claude", "-p", "--permission-mode", "acceptEdits", "--"]
    prompt = command[5]
    assert not prompt.startswith("-")
    assert "Fix the login button" in prompt
    assert "Do not reveal secrets" in prompt
    assert "Do not run git commit or git push" in prompt


def test_build_delegate_command_for_claude_omits_permission_mode_when_read_only():
    cfg = load_live_coding_config(
        {
            "stream_assistant": {
                "coding": {"delegate_to": "claude", "allow_file_edits": False}
            }
        }
    )

    command = build_delegate_command("Inspect the login button", cfg)

    assert command[:3] == ["claude", "-p", "--"]
    assert "--permission-mode" not in command
    assert "Do not modify files" in command[3]


def test_check_delegate_available_uses_configured_claude_path():
    config = {"stream_assistant": {"coding": {"delegate_to": "claude", "claude_path": "/opt/bin/claude"}}}

    with patch("hermes_cli.live_coding.shutil.which", return_value="/opt/bin/claude") as which:
        assert check_delegate_available(config) is True

    which.assert_called_once_with("/opt/bin/claude")


def test_run_delegate_updates_overlay_and_returns_output(tmp_path: Path):
    calls = []

    def fake_publish(_config, **fields):
        calls.append(fields)

    completed = subprocess.CompletedProcess(
        args=["codex", "exec", "task"],
        returncode=0,
        stdout="done\nAPI_TOKEN=secret-value\n",
        stderr="",
    )

    with patch("hermes_cli.live_coding.shutil.which", return_value="/usr/bin/codex"), patch(
        "hermes_cli.live_coding.subprocess.run", return_value=completed
    ), patch("hermes_cli.live_coding.publish_live_coding_state", side_effect=fake_publish):
        result = run_delegate("Fix bug", config={}, workdir=tmp_path)

    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["delegate"] == "codex"
    assert "API_TOKEN=secret-value" not in result["output"]
    assert calls[0]["codex_status"] == "running"
    assert calls[-1]["codex_status"] == "done"


def test_run_delegate_runs_claude_and_reports_delegate(tmp_path: Path):
    config = {"stream_assistant": {"coding": {"delegate_to": "claude"}}}
    completed = subprocess.CompletedProcess(
        args=["claude", "-p", "task"],
        returncode=0,
        stdout="done\n",
        stderr="",
    )

    with patch("hermes_cli.live_coding.shutil.which", return_value="/usr/bin/claude"), patch(
        "hermes_cli.live_coding.subprocess.run", return_value=completed
    ) as run, patch("hermes_cli.live_coding.publish_live_coding_state"):
        result = run_delegate("Fix bug", config=config, workdir=tmp_path)

    assert result["success"] is True
    assert result["delegate"] == "claude"
    assert run.call_args[0][0][:2] == ["claude", "-p"]


def test_run_delegate_reports_missing_codex(tmp_path: Path):
    with patch("hermes_cli.live_coding.shutil.which", return_value=None):
        result = run_delegate("Fix bug", config={}, workdir=tmp_path)

    assert result["success"] is False
    assert "Codex CLI not found" in result["error"]


def test_run_delegate_reports_missing_claude(tmp_path: Path):
    config = {"stream_assistant": {"coding": {"delegate_to": "claude"}}}

    with patch("hermes_cli.live_coding.shutil.which", return_value=None):
        result = run_delegate("Fix bug", config=config, workdir=tmp_path)

    assert result["success"] is False
    assert "Claude Code CLI not found" in result["error"]
