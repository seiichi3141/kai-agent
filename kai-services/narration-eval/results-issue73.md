# Issue #73 実測記録 — 人格・few-shot プロンプトの eval 比較

実施: 2026-07-09（Mac 開発環境から実測）。loop contract P1 に従い、プロンプト変更を
`generate.py`（実プラグインのプロンプト・後処理を直接 import）→ `eval.py` で
数値比較してから採用した。

## 計測条件

- 生成器: `generate.py`（本ディレクトリ）。`plugins/kai_narrator/__init__.py` の
  `_NARRATION_SYSTEM_PROMPT` / `_build_narration_user_prompt` / `_generate_narration`
  をそのまま使用（プロンプトの再実装なし）
- バックエンド: sei-win llama.cpp `http://100.98.225.44:8080/v1`
  （Qwen3.6-35B-A3B-UD-IQ3_XXS、`chat_template_kwargs.enable_thinking=false`）
  ＝ 本番実況 LLM と同一系統の小型ローカル LLM
- temperature=0（再現可能な比較。同条件で再実行し完全一致を確認済み）
- fixture: `docs/kai/narration/fixtures/issue65-confabulation.jsonl` /
  `issue55-baseline-good.jsonl`（narrator 発話スロット 7 / 9 件）

## 結果（composite と主要 FR）

| 指標                | issue65 現行 | issue65 新 | issue55 現行 | issue55 新      |
| ------------------- | ------------ | ---------- | ------------ | --------------- |
| **composite**       | 31           | **51**     | 18           | **54**          |
| FR1 一人称          | 0 PASS       | 0 PASS     | 0 PASS       | 0 PASS          |
| FR5 ID/生データ漏れ | 2 (raw_ref)  | **0 PASS** | 2 (raw_ref)  | **1 (raw_ref)** |
| FR6 反復率          | 0.0          | 0.286      | 0.111        | **0.0**         |
| FR7 操作説明のみ率  | 0.857        | **0.0**    | 0.889        | **0.111**       |
| FR9 range外         | 1            | **0**      | 0            | 0               |
| few-shot 例文漏れ   | —            | **0 件**   | —            | **0 件**        |

- **FR7（単調・操作スナップショットのみ）が支配的に改善** — #73 の狙いどおり、
  理由 or 結果反応が発話に入るようになった。
- FR5 は「Issue や PR は『65番の課題』のように言う」の陽性指示で 4→1 件に減少。
- issue65 の FR6 悪化（0→0.286）は「次は検証器を走らせようかな」の反復。残課題。
- confabulation フラグは両者とも残存（下記の注記参照）。機械ゲートは #75 の範囲。

## 採用しなかった案（負例プライミングの教訓）

v2: 間投詞の抑制指示＋禁止例を「Issue 65」表記に変更した版は **悪化**
（issue65: 51→27、raw_ref 0→3）。禁止例として書いた「Issue 65」の文字列を
小型モデルがそのまま真似た。**負例は書かず、陽性の言い換え（「65番の課題」）だけを
見本に置く**のが小型ローカル LLM には有効。

## 注記（verifier は緩めていない）

- issue55 の confabulation フラグ `課題×3` は、採用した陽性指示「N番の課題」が
  接地テキストに無い語「課題」を導入したことによる**構造的な検出**。
  GENERIC_ALLOW への追加（=検証器の緩和）は本変更と同時には行わない（P2）。
  別判断として扱う。
- issue65 の `パッチ×2` は接地の英語トークン `patch` のカタカナ転写で、
  transliteration の既知の弱点（false positive 寄り）。

## 再現手順

```bash
cd kai-services/narration-eval
python3 generate.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl \
  --base-url http://<llm-host>:8080/v1 --out /tmp/gen65.json
python3 eval.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl \
  --candidates /tmp/gen65.json
```
