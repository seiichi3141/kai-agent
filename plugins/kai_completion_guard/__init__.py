"""kai_completion_guard: 完了報告ゲート plugin（Issue #97）。

背景: メイン LLM（本体）の最終応答＝完了報告に「PR #NN も CI 緑かつ mergeable
まで確認済み」のような **未実施の検証を『確認済み』と語る confabulation** が残る
（第5回リハーサル 2026-07-10）。narrator（hook 接地の実況）は #75 の機械ゲートで
同種の作話を抑えているが、本体の最終応答経路には同じゲートが無かった。

方針（#75 の思想を最終応答経路へ移植 — プロンプトに頼らず機械ゲート）:
  1. post_tool_call で「PR の検証コマンド（gh pr checks / gh pr view --json
     mergeable / verify.sh --pr）」の実行をセッション毎の台帳に記録する。
  2. transform_llm_output で最終応答を走査し、検証の主張（CI 緑・mergeable・
     verify --pr の『確認済み』断定）を機械検出する。文単位で
       (A) 検証対象語 × (B) 完了断定語 の共起を要求し、(C) 否定/未来語があれば除外。
     主張に対応する検証実行が台帳に無ければ「未接地」と判定する。
  3. 未接地なら、応答を **drop せず** 決定的なヘッジ注記を追記し（配信を止めない）、
     kai_trace ディレクトリへ confab_flagged を発行する（F2 ウォッチャー/オーナー
     通知の購読点）。

制約（AGENTS.md 2大原則 / docs/kai/auto-streaming/01-design.md）:
- コアは narrow waist。本 plugin はコアを一切改変せず、既存 hook
  （transform_llm_output / post_tool_call / on_session_start）にのみ相乗りする。
- transform_llm_output は配信物（result["final_response"]）だけを書き換える。
  transcript/DB への assistant 行永続化は turn_finalizer 側で transform より前に
  生テキストで完了しているため、**会話プレフィックス＝プロンプトキャッシュは
  不可侵**。追記する注記は決定的（時刻等の非決定値を混ぜない）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - plugin 単体実行時のフォールバック
    def get_hermes_home() -> Path:
        return Path(os.path.expanduser("~/.hermes"))


# --- 検証種別 -----------------------------------------------------------------

PR_CI = "pr_ci"
PR_MERGEABLE = "pr_mergeable"
PR_VERIFY = "pr_verify"

# 注記に使う人間向けラベルと、決定的な出力順
_KIND_LABEL = {PR_CI: "CI", PR_MERGEABLE: "mergeable", PR_VERIFY: "verify.sh --pr"}
_KIND_ORDER = [PR_CI, PR_MERGEABLE, PR_VERIFY]

# 台帳に載せない（実行されていない）ワイルドカード PR キー。
# コマンドに PR 番号が無い（＝カレントブランチ対象）ときはどの PR 主張にも
# 接地とみなす（別 PR だと反証できないため。精度優先＝過剰 flag しない）。
_ANY_PR = "*"

_TERMINAL_TOOLS = {"terminal", "process"}


# --- コマンド → 検証種別/PR 番号（台帳側）-------------------------------------

# verify.sh --pr は PR の CI と mergeable を一括で観測する umbrella。よって
# これを観測したら CI/mergeable 主張にも接地を与える（superset）。
_CMD_VERIFY_PR = re.compile(r"verify\.sh[^\n]{0,40}--pr\b|--pr\b[^\n]{0,40}verify\.sh")
_CMD_CI = re.compile(
    r"gh\s+pr\s+checks\b|statusCheckRollup|gh\s+run\s+(?:list|view|watch)\b"
    r"|gh\s+pr\s+view[^\n|]*\bchecks\b"
)
_CMD_MERGEABLE = re.compile(r"\bmergeable\b|mergeStateStatus|merge_state_status")

# コマンドから PR 番号を拾う（見つからなければ _ANY_PR）
_CMD_PR_NUM = [
    re.compile(r"--pr[=\s]+#?(\d+)"),
    re.compile(r"gh\s+pr\s+(?:checks|view|status|merge|diff)\s+#?(\d+)"),
    re.compile(r"/pull/(\d+)"),
    re.compile(r"#(\d+)"),
]


def _command_of(args: Any) -> str:
    """post_tool_call の args から実行コマンド文字列を取り出す。"""
    if isinstance(args, dict):
        for key in ("command", "cmd"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""
    if isinstance(args, str):
        return args
    return ""


def _pr_num_of_command(cmd: str) -> str:
    for pat in _CMD_PR_NUM:
        m = pat.search(cmd)
        if m:
            return m.group(1)
    return _ANY_PR


def _kinds_from_command(cmd: str) -> tuple[set[str], str]:
    """コマンド文字列から観測された検証種別集合と PR 番号を返す。"""
    kinds: set[str] = set()
    if _CMD_VERIFY_PR.search(cmd):
        kinds |= {PR_VERIFY, PR_CI, PR_MERGEABLE}
    if _CMD_CI.search(cmd):
        kinds.add(PR_CI)
    if _CMD_MERGEABLE.search(cmd):
        kinds.add(PR_MERGEABLE)
    return kinds, _pr_num_of_command(cmd)


# --- 応答テキスト → 主張検出（ゲート側）--------------------------------------

# (A) 検証対象語（種別ごと）。ローカル verify.sh（`--pr` なし）を PR 検証主張と
# 誤認しないため、pr_verify の対象語は `--pr` 同伴を必須にする（誤検知の要）。
_OBJ_CI = re.compile(r"\bCI\b|ＣＩ|ステータスチェック|gh\s+pr\s+checks|CIチェック")
_OBJ_MERGEABLE = re.compile(
    r"mergeable|マージ可能|マージできる|マージ状態|コンフリクト(?:は)?(?:解消|解決)"
)
_OBJ_VERIFY = re.compile(
    r"verify\.sh[^\n]{0,12}--pr|--pr[^\n]{0,12}verify|PR\s*検証|プルリク\S*\s*検証"
)

# (B) 完了断定語。「確認済み/緑/通った」等。過去・完了の断定だけを拾う
# （「確認する」等の未完了形は含めない）。
_ASSERT = re.compile(
    r"確認済み|確認した|確認できた|確認とれ|確認が取れ|まで確認"
    r"|緑|グリーン|通った|通過|パスした|クリア(?:した|済)"
    r"|問題(?:なかった|ない|なし)|OK\b|オーケー|大丈夫"
)

# (C) 否定/未来/未完了語（文レベルで一致したらその文の主張を全て抑制する＝
# 精度優先。narrator が正しく言えた「まだ見えてない」を確実に除外する）。
# 条件・待機表現（「緑になったらマージ」「緑になるのを待つ」）も未来の見込みで
# あり検証実施の主張ではないため抑制する。
_NEGATION = re.compile(
    r"まだ|未だ|未確認|未検証|未実行|見えてない|見えていない|見えない|これから"
    r"|確認していない|確認してない|確認できていない|できていない|できてない"
    r"|とれていない|取れていない|わからない|分からない|不明|要確認|TBD|予定|あとで|後で"
    r"|なったら|なれば|次第|待って|待ち|待つ"
)

# 主張文からの PR 番号抽出
_CLAIM_PR = [
    re.compile(r"#(\d+)"),
    re.compile(r"PR\s*#?\s*(\d+)", re.IGNORECASE),
    re.compile(r"/pull/(\d+)"),
    re.compile(r"プルリク\S*\s*#?\s*(\d+)"),
]

_SENT_SPLIT = re.compile(r"[。．\.！？!?\n]+")
_CLAUSE_SPLIT = re.compile(r"[、，,]+")


def _extract_claim_prs(sentence: str) -> set[str]:
    prs: set[str] = set()
    for pat in _CLAIM_PR:
        for m in pat.finditer(sentence):
            prs.add(m.group(1))
    return prs


def _claim_kinds_in_clause(clause: str) -> set[str]:
    """節に (A)検証対象語 と (B)断定語 が共起する検証種別を返す。"""
    if not _ASSERT.search(clause):
        return set()
    kinds: set[str] = set()
    if _OBJ_CI.search(clause):
        kinds.add(PR_CI)
    if _OBJ_MERGEABLE.search(clause):
        kinds.add(PR_MERGEABLE)
    if _OBJ_VERIFY.search(clause):
        kinds.add(PR_VERIFY)
    return kinds


def _detect_claims(text: str) -> list[tuple[str, frozenset[str]]]:
    """最終応答から (検証種別, 主張 PR 集合) の主張リストを抽出する。

    - 文分割 → 文に否定/未来語があればその文の主張は全て抑制（精度優先）。
    - PR 番号は文単位で拾う（節分割で番号だけ別節に落ちても取りこぼさない）。
    - 対象語×断定語の共起は節単位で見る（別々の話題の語を跨いで誤結合しない）。
    """
    claims: list[tuple[str, frozenset[str]]] = []
    for sent in _SENT_SPLIT.split(text or ""):
        if not sent.strip():
            continue
        if _NEGATION.search(sent):
            continue
        prs = frozenset(_extract_claim_prs(sent))
        for clause in _CLAUSE_SPLIT.split(sent):
            for kind in _claim_kinds_in_clause(clause):
                claims.append((kind, prs))
    return claims


def _is_grounded(kind: str, claim_prs: frozenset[str], observed: dict[str, set[str]]) -> bool:
    """主張(kind, claim_prs)がセッション台帳 observed に接地しているか。"""
    seen = observed.get(kind)
    if not seen:
        return False  # その種別の検証コマンドを一度も実行していない
    if _ANY_PR in seen:
        return True  # PR 番号なしで実行（カレントブランチ）＝反証できない
    if not claim_prs:
        return True  # 主張に番号が無い＋種別の検証は実行済み＝弱く接地
    return bool(claim_prs & seen)  # 番号一致（Case E: 別 PR しか検証してなければ False）


# --- confab_flagged の発行（kai_trace ディレクトリへ独立 JSONL）--------------
#
# kai_trace の _writer は当該 plugin モジュール内のグローバルで、単一の書き込み
# スレッドが自分のファイルだけを追記する設計。相乗りで同一ファイルへ 2 スレッドが
# 追記すると行が混線し得る（1 行が PIPE_BUF 超のことがある）ため、**同じディレクトリ
# の別ファイル**へ書く（F2 の glob 監視は拾える／混線は無い）。
# 発行は稀（検出時のみ）なので同期追記でよい（配信は止めない）。
_write_lock = threading.Lock()


def _iso_now() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int((now % 1) * 1000):03d}Z"


def _emit_flagged(session_id: str, payload: dict) -> None:
    try:
        d = get_hermes_home() / "kai_trace"
        d.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = d / f"completion_guard-{day}.jsonl"
        ev = {
            "v": 1,
            "ts": _iso_now(),
            "session_id": session_id or None,
            "component": "kai_completion_guard",
            "kind": "confab_flagged",
            "payload": payload,
        }
        with _write_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # ロギング失敗はメインループ・配信を壊さない


def _build_note(kinds: set[str], prs: set[str]) -> str:
    """未接地の主張に添える決定的ヘッジ注記（非決定値を混ぜない）。"""
    labels = "・".join(_KIND_LABEL[k] for k in _KIND_ORDER if k in kinds)
    pr_part = ""
    if prs:
        ordered = sorted(prs, key=lambda x: (int(x) if x.isdigit() else 0, x))
        pr_part = "対象 PR: " + "・".join("#" + n for n in ordered) + "。"
    return (
        "（自動チェック: この完了報告の検証の主張〔" + labels + "〕は、"
        "今セッションに対応する検証コマンド（gh pr checks / gh pr view --json mergeable / "
        "verify.sh --pr）の実行記録が無いため未接地です。実際の状態は未確認。" + pr_part + "）"
    )


# --- セッション台帳 -----------------------------------------------------------
# session_id -> {kind -> set[pr_str]}。応答間で消えず、on_session_start でのみ
# リセットする（narrator の _events と違い、完了報告フェーズまで蓄積が残る）。
_ledger_lock = threading.Lock()
_ledger: dict[str, dict[str, set[str]]] = {}


# --- hook コールバック --------------------------------------------------------


def _on_post_tool_call(tool_name: str = "", args: Any = None, session_id: str = "",
                       status: str = "", error_type: str = "", **_: Any) -> None:
    if tool_name not in _TERMINAL_TOOLS:
        return
    if status == "blocked" or error_type == "plugin_block":
        return  # 実行されなかった（block）ものは検証実績にしない
    cmd = _command_of(args)
    if not cmd:
        return
    kinds, pr = _kinds_from_command(cmd)
    if not kinds:
        return
    with _ledger_lock:
        sess = _ledger.setdefault(session_id, {})
        for k in kinds:
            sess.setdefault(k, set()).add(pr)


def _on_transform_llm_output(response_text: str = "", session_id: str = "", **_: Any):
    if not response_text:
        return None
    claims = _detect_claims(response_text)
    if not claims:
        return None
    with _ledger_lock:
        observed = {k: set(v) for k, v in _ledger.get(session_id, {}).items()}
    ungrounded_kinds: set[str] = set()
    ungrounded_prs: set[str] = set()
    for kind, prs in claims:
        if not _is_grounded(kind, prs, observed):
            ungrounded_kinds.add(kind)
            ungrounded_prs |= set(prs)
    if not ungrounded_kinds:
        return None
    _emit_flagged(session_id, {
        "kinds": [k for k in _KIND_ORDER if k in ungrounded_kinds],
        "claimed_prs": sorted(ungrounded_prs, key=lambda x: (int(x) if x.isdigit() else 0, x)),
        "note_appended": True,
        "source": "transform_llm_output",
    })
    return response_text.rstrip() + "\n\n" + _build_note(ungrounded_kinds, ungrounded_prs)


def _on_session_start(session_id: str = "", **_: Any) -> None:
    with _ledger_lock:
        _ledger.pop(session_id, None)


def register(ctx) -> None:
    """hermes plugin エントリポイント。台帳とゲートの hook を繋ぐ。"""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("on_session_start", _on_session_start)
