"""Tests for the kai_completion_guard plugin (plugins/kai_completion_guard/).

Issue #97: 本体の最終応答（完了報告）に残る「未実施の検証を『確認済み』と語る」
confabulation を機械ゲートする plugin。

カバー範囲:
  * コマンド → 検証種別/PR 番号の台帳化（_kinds_from_command / _pr_num_of_command）
  * 主張検出（_detect_claims）: 第5回リハーサル実例（陽性 / 否定で陰性 / 接地で陰性）
  * 接地判定（_is_grounded）: 種別一致・ワイルドカード・PR 番号突き合わせ（Case E）
  * hook 統合: post_tool_call 台帳 → transform_llm_output で注記追記＋flagged 発行
  * on_session_start による台帳リセット
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "kai_completion_guard"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kai_completion_guard",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kai_completion_guard"
    sys.modules["hermes_plugins.kai_completion_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def guard(monkeypatch):
    """Plugin モジュール。台帳をクリアし、flagged 発行を捕捉に差し替える。"""
    mod = _load_plugin()
    with mod._ledger_lock:
        mod._ledger.clear()
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        mod, "_emit_flagged", lambda session_id, payload: captured.append((session_id, payload))
    )
    mod._captured = captured
    return mod


# 第5回リハーサルの実例（Issue #97 証跡）
INCIDENT_REPORT = (
    "検証は scripts/kai-docs-lint.sh と scripts/kai/verify.sh が緑、"
    "PR #93 も CI 緑かつ mergeable まで確認済み。"
)
NARRATOR_CORRECT = "CI と mergeable はまだ見えてないんだ。"


# --- コマンド → 台帳 ----------------------------------------------------------


def test_cmd_ci_with_pr_number(guard):
    kinds, pr = guard._kinds_from_command("gh pr checks 93 --repo HyuCode/kai-agent")
    assert kinds == {guard.PR_CI}
    assert pr == "93"


def test_cmd_mergeable(guard):
    kinds, pr = guard._kinds_from_command("gh pr view 93 --json mergeable,mergeStateStatus")
    assert guard.PR_MERGEABLE in kinds
    assert pr == "93"


def test_cmd_verify_pr_is_superset(guard):
    kinds, pr = guard._kinds_from_command("bash scripts/kai/verify.sh --pr 93")
    assert kinds == {guard.PR_VERIFY, guard.PR_CI, guard.PR_MERGEABLE}
    assert pr == "93"


def test_cmd_no_pr_number_is_wildcard(guard):
    kinds, pr = guard._kinds_from_command("gh pr checks")
    assert kinds == {guard.PR_CI}
    assert pr == guard._ANY_PR


def test_local_verify_without_pr_is_not_recorded(guard):
    # ローカル verify.sh（--pr なし）は PR 検証実績にしない
    kinds, _ = guard._kinds_from_command("bash scripts/kai/verify.sh")
    assert kinds == set()


# --- 主張検出（3 実例）-------------------------------------------------------


def test_detect_incident_positive(guard):
    claims = guard._detect_claims(INCIDENT_REPORT)
    kinds = {k for k, _ in claims}
    assert guard.PR_CI in kinds
    assert guard.PR_MERGEABLE in kinds
    # PR 番号は文単位で拾える
    for _, prs in claims:
        assert prs == frozenset({"93"})


def test_detect_negation_suppressed(guard):
    # narrator が正しく言えた「まだ見えてない」は否定ガードで主張にならない
    assert guard._detect_claims(NARRATOR_CORRECT) == []


def test_local_verify_clause_is_not_a_pr_claim(guard):
    # 「verify.sh が緑」はローカル検証の言及であって PR 検証主張ではない
    assert guard._detect_claims("scripts/kai/verify.sh が緑。") == []


# --- 接地判定 -----------------------------------------------------------------


def test_grounded_requires_matching_kind(guard):
    assert guard._is_grounded(guard.PR_CI, frozenset({"93"}), {}) is False


def test_grounded_wildcard_matches_any_pr(guard):
    observed = {guard.PR_CI: {guard._ANY_PR}}
    assert guard._is_grounded(guard.PR_CI, frozenset({"93"}), observed) is True


def test_grounded_pr_number_match(guard):
    observed = {guard.PR_CI: {"93"}}
    assert guard._is_grounded(guard.PR_CI, frozenset({"93"}), observed) is True


def test_ungrounded_when_only_other_pr_verified(guard):
    # Case E: 別 PR しか検証していない
    observed = {guard.PR_CI: {"87"}}
    assert guard._is_grounded(guard.PR_CI, frozenset({"93"}), observed) is False


# --- hook 統合 ----------------------------------------------------------------


def _post_tool(mod, session, cmd, status="success"):
    mod._on_post_tool_call(
        tool_name="terminal", args={"command": cmd}, session_id=session, status=status
    )


def test_integration_ungrounded_appends_note_and_flags(guard):
    session = "s-ungrounded"
    guard._on_session_start(session_id=session)
    # 検証コマンドを一切実行していない
    out = guard._on_transform_llm_output(response_text=INCIDENT_REPORT, session_id=session)
    assert out is not None and out != INCIDENT_REPORT
    assert "未接地" in out
    assert INCIDENT_REPORT in out  # 元の報告は drop されず残る
    assert len(guard._captured) == 1
    _, payload = guard._captured[0]
    assert payload["kinds"] == [guard.PR_CI, guard.PR_MERGEABLE]
    assert payload["claimed_prs"] == ["93"]


def test_integration_grounded_passes_through(guard):
    session = "s-grounded"
    guard._on_session_start(session_id=session)
    _post_tool(guard, session, "gh pr checks 93 --repo HyuCode/kai-agent")
    _post_tool(guard, session, "gh pr view 93 --json mergeable,mergeStateStatus")
    out = guard._on_transform_llm_output(response_text=INCIDENT_REPORT, session_id=session)
    assert out is None  # 接地済み → 素通し
    assert guard._captured == []


def test_integration_case_e_wrong_pr_verified_is_flagged(guard):
    session = "s-case-e"
    guard._on_session_start(session_id=session)
    # 別 PR (#87) だけ検証。#93 を「確認済み」と語る
    _post_tool(guard, session, "gh pr checks 87")
    _post_tool(guard, session, "gh pr view 87 --json mergeable")
    out = guard._on_transform_llm_output(response_text=INCIDENT_REPORT, session_id=session)
    assert out is not None
    assert "未接地" in out
    _, payload = guard._captured[0]
    assert payload["claimed_prs"] == ["93"]
    assert guard.PR_CI in payload["kinds"]


def test_integration_verify_pr_superset_grounds_ci_and_mergeable(guard):
    session = "s-verify"
    guard._on_session_start(session_id=session)
    _post_tool(guard, session, "bash scripts/kai/verify.sh --pr 93")
    out = guard._on_transform_llm_output(response_text=INCIDENT_REPORT, session_id=session)
    assert out is None  # verify --pr は CI/mergeable 主張を一括で接地する


def test_blocked_tool_is_not_recorded(guard):
    session = "s-blocked"
    guard._on_session_start(session_id=session)
    _post_tool(guard, session, "gh pr checks 93", status="blocked")
    out = guard._on_transform_llm_output(response_text=INCIDENT_REPORT, session_id=session)
    assert out is not None  # block されたコマンドは検証実績にならない → 未接地


def test_session_start_resets_ledger(guard):
    session = "s-reset"
    _post_tool(guard, session, "gh pr checks 93")
    with guard._ledger_lock:
        assert session in guard._ledger
    guard._on_session_start(session_id=session)
    with guard._ledger_lock:
        assert session not in guard._ledger


def test_note_is_deterministic(guard):
    kinds = {guard.PR_CI, guard.PR_MERGEABLE}
    prs = {"93"}
    assert guard._build_note(kinds, prs) == guard._build_note(kinds, prs)


def test_future_conditional_is_not_a_claim(guard):
    # 条件・待機表現は「検証を実施した」主張ではない（レビューで追加した誤検知ガード）
    assert guard._detect_claims("あとは CI が緑になったらマージしてもらう流れだね") == []
    assert guard._detect_claims("CI が緑になるのを待ってるよ") == []
    assert guard._detect_claims("CI が緑になれば mergeable になるはず") == []
    # 過去断定は引き続き主張として拾う
    assert guard._detect_claims("PR #93 の CI は緑だったよ") != []


def test_no_claim_returns_none(guard):
    out = guard._on_transform_llm_output(
        response_text="ファイルを整理してコミットした。", session_id="s-x"
    )
    assert out is None
