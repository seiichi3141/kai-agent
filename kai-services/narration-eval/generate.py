#!/usr/bin/env python3
"""Candidate generator for the narration eval harness (Issue #73).

Feeds fixture ops through the REAL narrator plugin prompts
(plugins/kai_narrator/__init__.py: _NARRATION_SYSTEM_PROMPT +
_build_narration_user_prompt + _generate_narration post-processing) against an
OpenAI-compatible LLM endpoint, and writes a candidates JSON for
``eval.py --candidates``. Standard library only.

The point (loop contract P1): prompt changes to the narrator must be measured
on the same fixture with the same backend at temperature=0 BEFORE adoption —
"it should sound better" is not evidence.

Usage:
    python3 generate.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl \
        --base-url http://<llm-host>:8080/v1 --out gen-issue65.json

Batching mirrors the plugin worker (_maybe_narrate / _handle_response):
  * tool ops accumulate as events; each recorded narrator utterance slot
    consumes the pending batch (last 8) and generates one candidate
  * a recorded agent_response updates the context and clears pending events
  * generated (non-SKIP) candidates feed the <recent> anti-repetition list
"""
import argparse
import importlib.util
import json
import re
import sys
import types
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Load the real narrator plugin (prompts + post-processing are the artifact
# under test — do NOT reimplement them here)
# --------------------------------------------------------------------------

def load_narrator_plugin():
    plugin_dir = REPO_ROOT / "plugins" / "kai_narrator"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kai_narrator", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kai_narrator"
    sys.modules["hermes_plugins.kai_narrator"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Fake agent.auxiliary_client so plugin._generate_narration talks to our
# OpenAI-compatible endpoint (llama.cpp etc.) with temperature=0
# --------------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def install_fake_auxiliary(base_url: str, model: str, timeout: float,
                           temperature_override: float | None):
    calls = {"n": 0}

    def call_llm(task="", messages=None, max_tokens=120, temperature=0.7,
                 extra_body=None, **_kw):
        payload = {
            "model": model,
            "messages": messages or [],
            "max_tokens": max_tokens,
            "temperature": (temperature_override
                            if temperature_override is not None else temperature),
            # Qwen 系は思考モードが max_tokens を食い潰す（M3 実機知見）
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if isinstance(extra_body, dict):
            payload.update(extra_body)
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        calls["n"] += 1
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # llama.cpp が enable_thinking を無視した場合の保険（<think> ブロック除去）
        content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL)
        return _Resp(content.strip())

    fake = types.ModuleType("agent.auxiliary_client")
    fake.call_llm = call_llm
    fake.extract_content_or_reasoning = lambda resp: resp.choices[0].message.content
    fake_agent = types.ModuleType("agent")
    sys.modules["agent"] = fake_agent
    sys.modules["agent.auxiliary_client"] = fake
    return calls


# --------------------------------------------------------------------------
# Fixture → plugin-shaped events → candidates
# --------------------------------------------------------------------------

def op_to_event(op) -> dict:
    res = op.get("result") or {}
    return {
        "tool": op.get("tool"),
        "args": op.get("args"),
        "intent": str(op.get("turn_intent") or ""),
        "result_digest": str(res.get("digest") or "")[:100],
        "status": res.get("status") or "",
        "error_message": res.get("error") or "",
        "duration_ms": op.get("duration_ms"),
        "session_id": "eval",
    }


def generate(plugin, ops) -> list[dict]:
    """One candidate per recorded narrator utterance, in fixture order."""
    out = []
    pending: list[dict] = []
    context = ""
    recent: list[str] = []
    for op in ops:
        if op.get("tool"):
            pending.append(op_to_event(op))
        for sp in op.get("expected_or_recorded") or []:
            src = sp.get("source")
            if src == "narrator":
                events = pending[-8:] or [op_to_event(op)]
                try:
                    text = plugin._generate_narration(
                        events, context=context, recent=recent[-3:])
                except Exception as e:
                    print(f"[error] generation failed: {e}", file=sys.stderr)
                    text = ""
                text = text if text else "SKIP"
                out.append({"text": text, "recorded": sp.get("text", ""),
                            "phase": op.get("phase")})
                pending = []
                if not plugin._is_skip(text):
                    recent.append(text)
            elif src == "agent_response":
                # 最終応答で未実況イベントは stale（plugin._handle_response と同じ）
                context = str(sp.get("text") or "")[:120]
                pending = []
    return out


def main():
    ap = argparse.ArgumentParser(description="generate narration candidates via the real plugin prompts")
    ap.add_argument("--fixture", required=True, help="fixture JSONL path")
    ap.add_argument("--base-url", required=True,
                    help="OpenAI-compatible endpoint base URL (e.g. http://host:8080/v1)")
    ap.add_argument("--model", default="default", help="model name to send (backend may ignore)")
    ap.add_argument("--out", required=True, help="candidates JSON output path (for eval.py --candidates)")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="sampling temperature (default 0.0 = reproducible comparison)")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    calls = install_fake_auxiliary(args.base_url, args.model, args.timeout, args.temperature)
    plugin = load_narrator_plugin()

    # eval.load_fixture と同じ読み方（同ディレクトリの eval.py を直接 import）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import eval as eval_mod
    ops, _issue = eval_mod.load_fixture(args.fixture)

    cands = generate(plugin, ops)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cands, f, ensure_ascii=False, indent=2)

    n_skip = sum(1 for c in cands if eval_mod._is_skip_candidate(c["text"]))
    print(f"[gen] {len(cands)} candidates ({n_skip} SKIP) via {calls['n']} LLM calls "
          f"-> {args.out}")
    for i, c in enumerate(cands):
        print(f"  #{i} [{c['phase']}] {c['text']}")


if __name__ == "__main__":
    main()
