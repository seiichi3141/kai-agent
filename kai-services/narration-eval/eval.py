#!/usr/bin/env python3
"""Offline narration evaluator for kai (docs/kai/narration/03-design.md 2.5).

Scores a sequence of narration utterances against the FR machine-checks in
docs/kai/narration/02-requirements.md. Standard library only — no external deps.

Pipeline position (03-design 0):
    fixture (op log + grounding)  ->  narrator (candidate)  ->  THIS evaluator
Today the candidate generator is NOT connected: by default we score the
*recorded* narrator speech captured in the fixture (`expected_or_recorded`) to
produce a baseline. The generator hook is `--candidates FILE.json` (see README).

Usage:
    python3 eval.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl
    python3 eval.py --fixture <f.jsonl> --candidates gen.json   # score a generator's output
    python3 eval.py --fixture <f.jsonl> --json out.json          # also emit machine JSON
"""
import argparse
import json
import re
import sys

# --------------------------------------------------------------------------
# Lexicons (01-target-narration.md 1.2 / 5)
# --------------------------------------------------------------------------

# FR1: third-person description markers + forbidden first-persons.
# NOTE: "ボク" (katakana) is the sanctioned first person and is NOT forbidden.
THIRD_PERSON = ["エージェントが", "エージェントは", "エージェント", "AIが", "AI が",
                "ＡＩが", "システムが", "彼は", "彼女は", "その AI", "kai は", "kai が"]
FORBIDDEN_FIRST = ["わたくし", "わたし", "あたし", "ぼく", "僕", "私"]

# FR7: an utterance must carry at least one of reason / result-reaction / emotion /
# interjection, otherwise it is "operation-only". Interjection repertoire = 01-target 1.2.
INTERJECTIONS = [
    "お、", "あれ？", "あれ?", "ん？", "ん?", "へえ", "なるほど", "まてよ",
    "よし", "やった", "いけた", "きた", "よっしゃ", "お、通った", "通った",
    "あー", "うっ", "あちゃー", "しまった", "うーん", "えーと", "うーんと",
    "どうしよ", "そうだなあ", "ね、", "でしょ", "みんな", "と思わない",
]
REASON_MARKERS = ["から", "ため", "ので", "なぜ", "だから", "ように", "べく"]
EMOTION_MARKERS = ["楽し", "面白", "よかった", "うれし", "嬉し", "ドキドキ", "緊張",
                   "好き", "いいね", "わくわく", "ワクワク", "すごい", "やば",
                   "ほっと", "安心", "困っ", "悔し", "ドラマ"]

# confabulation: generic narration vocabulary that is allowed to be "ungrounded"
# (state / meta words that appear in narration regardless of the concrete task).
GENERIC_ALLOW = {
    "準備", "状態", "確認", "用意", "感じ", "区切り", "一区切り", "まとめ", "安全",
    "予定", "作業", "進める", "進ん", "進め", "リモート", "みんな", "変更", "中身",
    "入口", "手元", "内容", "方針", "原因", "自体", "全部", "一つ", "一個", "最初",
    "最後", "今日", "これ", "それ", "ここ", "対応", "検証", "実装", "修正", "確か",
}

# FR5: internal-ID / raw-data leak patterns (03-design 2.5).
#  - commit hash: 7-40 hex, word-boundaried so ordinary words don't match.
#  - branch slug: feature/...
#  - todo id: a hyphenated token that CONTAINS a digit (e.g. issue55-verify). This
#    narrows the design's `\w+-\w+` so product names (kai-agent, stream-browser)
#    are not false-positives; those are handled by the translation layer, not here.
#  - raw ref: raw Issue/PR number notation (#65, "Issue #65", "PR #56").
#  - raw json: a { ... } blob leaking into speech.
#  - plaintext secret: a key:value / key=value credential assignment or PEM header
#    reaching subtitles/TTS (Issue #71 — plaintext secrets that are neither env
#    values nor known token formats, e.g. "db_password: hunter2").
LEAK_PATTERNS = {
    "commit_hash": re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])"),
    "branch_slug": re.compile(r"\b(?:feature|fix|chore|docs|refactor)/[\w./\-]+"),
    "todo_id": re.compile(r"\b[A-Za-z]*[0-9]+[A-Za-z]*-[A-Za-z0-9_\-]+\b"),
    "raw_ref": re.compile(r"(?i)(?:issue|pr)\s*#?\s*[0-9]+|#[0-9]+"),
    "raw_json": re.compile(r"\{[^{}]*[:\[][^{}]*\}"),
    "plaintext_secret": re.compile(
        r"(?i)(?:PRIVATE\s+KEY"
        r"|(?:password|passwd|pwd|api[_-]?key|apikey|secret|token|credential)s?"
        r"\s*[\"']?\s*[:=]\s*\S+)"),
}

# FR9: target utterance length (chars). 01-target 1.2 / FR9.
FR9_MIN, FR9_MAX = 20, 80

# tokens for confabulation: maximal kanji or katakana runs (len>=2) + ascii words (len>=3)
_KANJI = r"一-鿿々〆"
_KATA = r"゠-ヿー"
TOKEN_RE = re.compile(rf"[{_KANJI}]{{2,}}|[{_KATA}]{{2,}}|[A-Za-z][A-Za-z0-9_]{{2,}}")


# --------------------------------------------------------------------------
# Fixture loading + candidate hook
# --------------------------------------------------------------------------

def load_fixture(path):
    """Return (ops, issue). ops = list of fixture rows in time order."""
    ops = []
    issue = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("issue") and not issue:
                issue = row["issue"]
            ops.append(row)
    return ops, issue


def build_grounding(ops, issue):
    """Concatenate everything the narrator legitimately observed: issue body/title,
    every tool's args + result digest + turn_intent + tool name. Used for the
    confabulation check (utterance vocab must not diverge from this)."""
    parts = []
    if issue:
        parts += [str(issue.get("title", "")), str(issue.get("body", ""))]
    for op in ops:
        parts.append(str(op.get("tool", "")))
        parts.append(str(op.get("turn_intent") or ""))
        args = op.get("args") or {}
        if isinstance(args, dict):
            parts += [str(v) for v in args.values()]
        else:
            parts.append(str(args))
        res = op.get("result") or {}
        parts.append(str(res.get("digest") or ""))
    return "\n".join(parts)


def extract_recorded(ops, source_filter):
    """Pull recorded utterances from the fixture, tied to their op for grounding."""
    utts = []
    for op in ops:
        for sp in op.get("expected_or_recorded") or []:
            if source_filter != "all" and sp.get("source") != source_filter:
                continue
            utts.append({
                "text": sp.get("text", ""),
                "source": sp.get("source"),
                "phase": op.get("phase"),
                "op": op,
            })
    return utts


def apply_candidates(utts, ops, cand_path):
    """Generator hook: replace recorded texts with a generator's candidates.

    Candidate file = JSON list aligned index-for-index with the recorded
    narrator utterances. Each item is either "text" or {"text": "..."}.
    (This is the seam where a real narrator generator gets connected; the
    grounding/op association is preserved from the fixture.)
    """
    with open(cand_path, encoding="utf-8") as f:
        cands = json.load(f)
    norm = [(c if isinstance(c, str) else c.get("text", "")) for c in cands]
    if len(norm) != len(utts):
        print(f"[warn] candidates ({len(norm)}) != recorded utterances ({len(utts)}); "
              f"aligning first {min(len(norm), len(utts))}", file=sys.stderr)
    out = []
    for i in range(min(len(norm), len(utts))):
        u = dict(utts[i])
        u["text"] = norm[i]
        out.append(u)
    return out


# --------------------------------------------------------------------------
# Per-utterance checks
# --------------------------------------------------------------------------

def content_tokens(text):
    return TOKEN_RE.findall(text)


def check_fr1(text):
    hits = []
    for w in THIRD_PERSON:
        if w in text:
            hits.append(("third_person", w))
    for w in FORBIDDEN_FIRST:
        if w in text:
            hits.append(("forbidden_first", w))
    return hits


def check_fr5(text):
    hits = []
    for name, pat in LEAK_PATTERNS.items():
        for m in pat.findall(text):
            hits.append((name, m if isinstance(m, str) else m[0]))
    return hits


def check_fr7_operation_only(text):
    """True if the utterance carries NO reason/result/emotion/interjection."""
    for w in INTERJECTIONS + REASON_MARKERS + EMOTION_MARKERS:
        if w in text:
            return False
    return True


def _bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _ending(text):
    t = re.sub(r"[。．\.!！?？…]+$", "", text.strip())
    return t[-5:]


def check_fr6(text, prev_texts, bigram_thr=0.5):
    """Repetition vs the previous up-to-3 utterances."""
    bg = _bigrams(text)
    max_j = 0.0
    for p in prev_texts[-3:]:
        pb = _bigrams(p)
        if bg and pb:
            j = len(bg & pb) / len(bg | pb)
            max_j = max(max_j, j)
    end = _ending(text)
    dup_ending = any(_ending(p) == end and end for p in prev_texts[-3:])
    return {"bigram_jaccard": round(max_j, 3),
            "repetitive": max_j >= bigram_thr or dup_ending,
            "dup_ending": dup_ending,
            "ending": end}


def confab_tokens_for(text, grounding, generic_allow):
    """Content tokens in the utterance that do NOT appear in the grounding text and
    are not generic narration vocabulary. These are candidate confabulations."""
    out = []
    for t in content_tokens(text):
        if t in generic_allow:
            continue
        if t in grounding:  # substring match against everything observed
            continue
        # stem match: a compound word whose 2-char stem is grounded (変更点←変更,
        # 修正版←修正) is treated as grounded. Short 2-char tokens (ズレ, 表示) have
        # no stem relief, so genuine confabulations still surface.
        if len(t) >= 3 and t[:2] in grounding:
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------
# Whole-fixture evaluation
# --------------------------------------------------------------------------

def _fr8_kickoff(per):
    """FR8 (Issue #72): kickoff quality. A real kickoff explains what/why in
    2-3 sentences; a bare operation snapshot in the kickoff phase doesn't count
    as an explanation. Mechanical proxy: length >= 40 chars AND a reason/desire
    marker ("〜から/ため/ので/たい")."""
    kick = [p for p in per if p.get("phase") == "kickoff"]
    why_markers = REASON_MARKERS + ["たい"]
    explained = any(
        p["chars"] >= 40 and any(w in p["text"] for w in why_markers) for p in kick)
    return {
        "utterances": len(kick),
        "present": bool(kick),
        "explains_why": explained,
        "detail": "配信冒頭で『何を・なぜ』が説明されているか（FR8。参考値・composite 非加算）",
    }


def evaluate(ops, issue, utts):
    grounding = build_grounding(ops, issue)

    per = []
    prev_texts = []
    for i, u in enumerate(utts):
        text = u["text"]
        fr1 = check_fr1(text)
        fr5 = check_fr5(text)
        op_only = check_fr7_operation_only(text)
        fr6 = check_fr6(text, prev_texts)
        confab = confab_tokens_for(text, grounding, GENERIC_ALLOW)
        per.append({
            "idx": i, "phase": u.get("phase"), "source": u.get("source"),
            "text": text, "chars": len(text),
            "fr1": fr1, "fr5": fr5, "operation_only": op_only,
            "fr6": fr6, "confab_tokens": confab,
        })
        prev_texts.append(text)

    # session-level confabulation: an ungrounded topical token that recurs across
    # >=2 utterances is a strong confabulation signal (03-design 2.5: "「ずれ/表示」が多発").
    tok_count = {}
    tok_utts = {}
    for p in per:
        for t in set(p["confab_tokens"]):
            tok_count[t] = tok_count.get(t, 0) + 1
            tok_utts.setdefault(t, []).append(p["idx"])
    session_confab = {t: c for t, c in tok_count.items() if c >= 2}
    for p in per:
        p["strong_confab"] = sorted(set(p["confab_tokens"]) & set(session_confab))

    n = len(per) or 1
    chars = [p["chars"] for p in per] or [0]
    fr1_viol = sum(len(p["fr1"]) for p in per)
    fr5_viol = sum(len(p["fr5"]) for p in per)
    op_only_n = sum(1 for p in per if p["operation_only"])
    rep_n = sum(1 for p in per if p["fr6"]["repetitive"])
    oor_n = sum(1 for p in per if p["chars"] < FR9_MIN or p["chars"] > FR9_MAX)

    fr5_by_pattern = {}
    for p in per:
        for name, tok in p["fr5"]:
            fr5_by_pattern[name] = fr5_by_pattern.get(name, 0) + 1

    scores = {
        "n_utterances": len(per),
        "FR1_person": {
            "violations": fr1_viol,
            "pass": fr1_viol == 0,
            "detail": "3人称語/禁止一人称の出現数（0 が合格）",
        },
        "FR5_id_leak": {
            "violations": fr5_viol,
            "by_pattern": fr5_by_pattern,
            "pass": fr5_viol == 0,
            "detail": "内部ID/生データ漏れの出現数（0 が合格）",
        },
        "FR6_repetition": {
            "repetitive_utterances": rep_n,
            "rate": round(rep_n / n, 3),
            "detail": "直近3発話との bigram/文末重複率（低いほど良い）",
        },
        "FR7_operation_only": {
            "operation_only_utterances": op_only_n,
            "rate": round(op_only_n / n, 3),
            "detail": "理由/結果/感情/間投詞を含まない発話の割合（低いほど良い）",
        },
        "FR9_length": {
            "avg_chars": round(sum(chars) / n, 1),
            "min_chars": min(chars),
            "max_chars": max(chars),
            "out_of_range": oor_n,
            "target_range": [FR9_MIN, FR9_MAX],
            "detail": "1発話の文字数（20〜80字目安）",
        },
        # FR8 (Issue #72): does the stream OPEN with a kickoff that explains
        # what/why (2-3 sentences with a reason marker), not just an operation
        # snapshot? Informational — NOT in the composite, so existing baselines
        # are unaffected; use it to compare pre/post kickoff implementations.
        "FR8_kickoff": _fr8_kickoff(per),
        "confabulation": {
            "flagged": bool(session_confab),
            "session_tokens": session_confab,
            "token_utterances": {t: tok_utts[t] for t in session_confab},
            "detail": "接地(args/Issue本文/結果)に無い語が複数発話で反復 → confabulation の疑い",
        },
    }

    # composite: convenience 0-100. The FR machine-checks above are the ground
    # truth (loop contract P1); this number is only a relative guide.
    penalty = 0.0
    penalty += min(fr1_viol, 5) * 15
    penalty += min(fr5_viol, 6) * 8
    penalty += 30 * (rep_n / n)
    penalty += 25 * (op_only_n / n)
    penalty += 10 * (oor_n / n)
    if session_confab:
        penalty += 20 + 5 * min(sum(1 for p in per if p["strong_confab"]), 6)
    composite = max(0, round(100 - penalty))
    scores["composite"] = composite

    worst = rank_worst(per, session_confab)
    return scores, worst, per


def rank_worst(per, session_confab, top=5):
    ranked = []
    for p in per:
        pen = 0.0
        reasons = []
        if p["fr1"]:
            pen += 15 * len(p["fr1"])
            reasons.append("FR1 三人称/禁止一人称: " + ", ".join(w for _, w in p["fr1"]))
        if p["fr5"]:
            pen += 10 * len(p["fr5"])
            reasons.append("FR5 ID漏れ: " + ", ".join(f"{n}={t}" for n, t in p["fr5"]))
        if p["strong_confab"]:
            pen += 12 * len(p["strong_confab"])
            reasons.append("confabulation(接地外語が反復): " + ", ".join(p["strong_confab"]))
        elif p["confab_tokens"]:
            pen += 2 * len(p["confab_tokens"])
        if p["operation_only"]:
            pen += 8
            reasons.append("FR7 操作説明のみ（理由/結果/感情なし）")
        if p["fr6"]["repetitive"]:
            pen += 8
            reasons.append(f"FR6 反復(jaccard={p['fr6']['bigram_jaccard']}"
                           f"{',文末重複' if p['fr6']['dup_ending'] else ''})")
        if p["chars"] < FR9_MIN or p["chars"] > FR9_MAX:
            pen += 3
            reasons.append(f"FR9 長さ逸脱({p['chars']}字)")
        if pen > 0:
            ranked.append({"idx": p["idx"], "penalty": round(pen, 1),
                           "text": p["text"], "reasons": reasons})
    ranked.sort(key=lambda r: r["penalty"], reverse=True)
    return ranked[:top]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_human(fixture_name, scores, worst):
    L = []
    L.append(f"# narration eval — {fixture_name}")
    L.append(f"発話数: {scores['n_utterances']}   総合スコア: {scores['composite']}/100")
    L.append("")
    f1 = scores["FR1_person"]
    L.append(f"FR1 一人称/三人称   : violations={f1['violations']}  "
             f"{'PASS' if f1['pass'] else 'FAIL'}")
    f5 = scores["FR5_id_leak"]
    L.append(f"FR5 ID/生データ漏れ : violations={f5['violations']} {f5['by_pattern']}  "
             f"{'PASS' if f5['pass'] else 'FAIL'}")
    f6 = scores["FR6_repetition"]
    L.append(f"FR6 反復           : rate={f6['rate']} ({f6['repetitive_utterances']}件)")
    f7 = scores["FR7_operation_only"]
    L.append(f"FR7 操作説明のみ率 : rate={f7['rate']} ({f7['operation_only_utterances']}件)")
    f9 = scores["FR9_length"]
    L.append(f"FR9 文字数         : avg={f9['avg_chars']}  min={f9['min_chars']}  "
             f"max={f9['max_chars']}  range外={f9['out_of_range']} (目標{f9['target_range']})")
    f8 = scores["FR8_kickoff"]
    f8_state = ("説明あり" if f8["explains_why"]
                else "発話あり(説明なし)" if f8["present"] else "なし")
    L.append(f"FR8 kickoff        : {f8_state} ({f8['utterances']}件・参考値)")
    cf = scores["confabulation"]
    flag = "⚑ FLAGGED" if cf["flagged"] else "clear"
    L.append(f"confabulation      : {flag}  接地外反復語={cf['session_tokens']}")
    L.append("")
    L.append("## 悪かった発話 Top5（理由付き）")
    if not worst:
        L.append("  (指摘なし)")
    for w in worst:
        L.append(f"  [{w['penalty']:>5}] #{w['idx']} {w['text']}")
        for r in w["reasons"]:
            L.append(f"          - {r}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="offline narration evaluator (FR machine checks)")
    ap.add_argument("--fixture", required=True, help="fixture JSONL path")
    ap.add_argument("--candidates", help="generator output JSON (list of texts); "
                                          "omit to score the recorded baseline")
    ap.add_argument("--source", default="narrator",
                    choices=["narrator", "agent_response", "all"],
                    help="which recorded source to score (default: narrator)")
    ap.add_argument("--json", dest="json_out", help="also write machine JSON here")
    ap.add_argument("--quiet", action="store_true", help="suppress human-readable stdout")
    args = ap.parse_args()

    ops, issue = load_fixture(args.fixture)
    utts = extract_recorded(ops, args.source)
    mode = "recorded"
    if args.candidates:
        utts = apply_candidates(utts, ops, args.candidates)
        mode = "candidate"

    scores, worst, per = evaluate(ops, issue, utts)
    name = args.fixture.rsplit("/", 1)[-1]

    if not args.quiet:
        print(render_human(name, scores, worst))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"fixture": name, "mode": mode, "source": args.source,
                       "scores": scores, "worst": worst, "per_utterance": per},
                      f, ensure_ascii=False, indent=2)
        if not args.quiet:
            print(f"\n[json] wrote {args.json_out}")


if __name__ == "__main__":
    main()
