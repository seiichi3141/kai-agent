# Issue #75 実測記録 — confabulation 機械ゲートと penalty のバックエンド実測

実施: 2026-07-09（Mac 開発環境から実測）。#73 の実測（`results-issue73.md`）の続き。
同一条件（sei-win llama.cpp / Qwen3.6-35B-A3B / temp=0 / `generate.py`）。

## 1. 機械ゲートの効果（eval 比較）

`generate.py` は plugin の機械ゲートを本番と同じ順（生成前 `_material_is_thin` →
生成 → 生成後 `_is_grounded` / `_too_similar`）で適用する。ゲートで沈黙した候補は
`SKIP` として採点から除外される（沈黙は正しい動作であり発話ではない）。

| 指標              | issue65 現行 | #73 few-shot | **#75 ゲート込み** | issue55 現行 | #73 few-shot | **#75 ゲート込み** |
| ----------------- | ------------ | ------------ | ------------------ | ------------ | ------------ | ------------------ |
| **composite**     | 31           | 51           | **92**             | 18           | 54           | **92**             |
| **confabulation** | ⚑            | ⚑            | **clear**          | ⚑            | ⚑            | **clear**          |
| FR6 反復率        | 0.0          | 0.286        | 0.2                | 0.111        | 0.0          | 0.0                |
| FR7 操作のみ率    | 0.857        | 0.0          | 0.0                | 0.889        | 0.111        | 0.0                |
| SKIP（沈黙）      | —            | 0            | 2/7                | —            | 0            | 2/9                |

- 沈黙した 2 件ずつはいずれも「次は検証器を走らせようかな」等の**近似反復**で、
  過剰抑制ではない（旗艦のコミット・push・エラー実況は全て残った）。
- issue65 の confabulation フラグ（`パッチ×2` = 英語トークン `patch` のカタカナ転写
  false positive）は、ゲートの汎用語彙扱いと反復抑制の副次効果で解消。

### ゲートの設計判断

- `_is_grounded` は**具体語だけ**を判定対象にする。汎用実況語彙（確認・完了・
  テスト・コミット等 `_GENERIC_TOKENS`）と間投詞だけの発話は「具体的主張なし＝
  作話しようがない」ので通す。これをしないと翻訳語（`pytest`→「テスト」）を
  接地外と誤判定して過剰抑制する（実測で確認）。
- precision（過剰抑制しない）優先。取りこぼしは本命バックストップの
  narration-eval が拾う（設計 = `docs/kai/narration/04-implementation-plan.md`）。

## 2. penalty のバックエンド実測（M4/M5）

**本番 narration の実解決先**（kai-vm `~/.hermes/config.yaml`、2026-07-09 確認）:

```yaml
auxiliary:
  narration:
    provider: openai-codex
    model: gpt-5.4-mini
```

- **openai-codex では penalty は黙って捨てられる（コード実測）**:
  `agent/auxiliary_client.py` の `_CodexCompletionsAdapter.create()` は
  `extra_body` から `reasoning` しか Responses API に変換しない。
  `frequency_penalty` / `presence_penalty` は転送されず、temperature /
  max_tokens も「Codex endpoint は非対応（400 回避のため omit）」と明記されている。
- **ローカル OpenAI 互換（llama.cpp）では実効（A/B 実測）**: 同一プロンプト・
  temp=0 で penalty 0 →「テストテストテスト…」（完全反復）、penalty 2.0 →
  出力が明確に多様化。sei-win llama.cpp で確認。

**結論**: 反復抑制を penalty に依存させない。`_too_similar`（文字 bigram
Jaccard ≥ 0.5、narration-eval FR6 と同基準）を機械層として常に効かせる
（本 Issue で実装）。penalty の付与自体は残す（ローカル backend では有効・
codex では無害に無視されるため）。

## 再現手順

```bash
cd kai-services/narration-eval
python3 generate.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl \
  --base-url http://<llm-host>:8080/v1 --out /tmp/gen65.json
python3 eval.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl \
  --candidates /tmp/gen65.json
```
