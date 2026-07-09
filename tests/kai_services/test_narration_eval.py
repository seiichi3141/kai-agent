"""Tests for the offline narration evaluator (kai-services/narration-eval/eval.py).

Issue #71: the FR5 leak check must flag plaintext secrets (credential
assignments / PEM headers) that are neither env values nor known token formats,
without false-positives on normal narration.
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_eval():
    path = REPO_ROOT / "kai-services" / "narration-eval" / "eval.py"
    spec = importlib.util.spec_from_file_location("narration_eval", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fr5_flags_plaintext_secret():
    ev = _load_eval()
    hits = ev.check_fr5("接続情報は db_password: hunter2 だよ")
    assert any(name == "plaintext_secret" for name, _ in hits)
    hits2 = ev.check_fr5("-----BEGIN OPENSSH PRIVATE KEY----- が見えた")
    assert any(name == "plaintext_secret" for name, _ in hits2)
    hits3 = ev.check_fr5("api_key=abc123def を設定した")
    assert any(name == "plaintext_secret" for name, _ in hits3)


def test_fr5_no_false_positive_on_normal_narration():
    ev = _load_eval()
    assert ev.check_fr5("いまテストを回してるよ、通るといいな") == []
    assert ev.check_fr5("ドキュメントに後片付けの手順を足したよ") == []


def test_fr8_kickoff_quality():
    # FR8 (Issue #72): 操作スナップショットだけの kickoff は「説明」と数えない
    ev = _load_eval()
    op_only = [{"phase": "kickoff", "text": "verify.sh の中身を見て、テストの入口をつかむよ。",
                "chars": 22}]
    real = [{"phase": "kickoff",
             "text": "今日は配信あとの後片付け手順をドキュメントに書き足すよ。"
                     "口伝だと漏れちゃうから、ちゃんと残しておきたいんだ。",
             "chars": 55}]
    none = [{"phase": "work", "text": "テストを回してるよ", "chars": 9}]
    r1 = ev._fr8_kickoff(op_only)
    assert r1["present"] and not r1["explains_why"]
    r2 = ev._fr8_kickoff(real)
    assert r2["present"] and r2["explains_why"]
    r3 = ev._fr8_kickoff(none)
    assert not r3["present"] and not r3["explains_why"]


def test_fr5_baseline_fixtures_unchanged():
    # 既存 fixture の録音発話に新パターンの誤検出が無い（ベースライン非退行）
    ev = _load_eval()
    fixtures = REPO_ROOT / "docs" / "kai" / "narration" / "fixtures"
    for fx in sorted(fixtures.glob("*.jsonl")):
        with open(fx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for sp in row.get("expected_or_recorded") or []:
                    hits = ev.check_fr5(sp.get("text", ""))
                    leaked = [h for h in hits if h[0] == "plaintext_secret"]
                    assert not leaked, (fx.name, sp.get("text"), leaked)
