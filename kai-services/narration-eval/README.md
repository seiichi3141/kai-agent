# narration-eval — 実況オフライン評価ハーネス

kai の実況（narrator）改善のための**オフライン評価環境**。配信せずに
「本番相当の操作ログ（fixture）→ 実況 → 評価スコア」を回す。設計は
`docs/kai/narration/03-design.md`（§0 背骨・§2.2 fixture・§2.5 評価器）、
受け入れ基準は `docs/kai/narration/02-requirements.md`（FR1〜9）。

> **なぜ先にこれを作るか（loop contract P1）**: 「実況が良くなった」を検証器に
> 接地するため。ハーネスが無いと改善を数値で確認できない。まず現 narrator の
> 記録済み発話でベースラインを取り、以降の変更はこのスコアで回帰確認する。

```text
 ①本番相当の操作ログ        ②実況生成            ③評価（ここ）
 fixture (op列+接地)  ──▶  narrator(candidate) ──▶  eval.py ──▶ スコア
   *.jsonl                 ※今回は未接続         FR機械チェック
```

## 使い方

標準ライブラリのみ。追加インストール不要（`python3` だけ）。

```bash
cd kai-services/narration-eval

# 記録済み発話（当時の実況）でベースライン採点
python3 eval.py --fixture ../../docs/kai/narration/fixtures/issue65-confabulation.jsonl

# 機械可読 JSON も出す
python3 eval.py --fixture <fixture.jsonl> --json out.json

# 生成器（将来）の出力を採点する ＝ 候補生成の口（下記）
python3 eval.py --fixture <fixture.jsonl> --candidates gen.json

# agent_response も含める / narrator だけ（既定）
python3 eval.py --fixture <fixture.jsonl> --source all
```

現状のベースラインは [`baseline.md`](./baseline.md)、生 JSON は
`baseline-issue65.json` / `baseline-issue55.json`（回帰の基準線）。

## fixture 形式（`docs/kai/narration/fixtures/*.jsonl`）

JSONL・1 行 1 操作（時系列）。スキーマは 03-design §2.2 準拠。

```jsonc
{
  "phase": "kickoff|work|summary",
  "ts": "2026-07-08T13:13:51.835Z",
  "issue": { "number": 65, "title": "…", "body": "…" }, // 先頭行のみ
  "turn_intent": null, // ※トレースは assistant の思考文を記録しない → null
  "tool": "patch",
  "args": { "path": "…", "patch": "…" }, // 編集の中身込み（接地材料）
  "result": { "status": "ok", "digest": "…" }, // トレースの結果から要約
  "expected_or_recorded": [
    // 当時の記録済み発話（採点対象・baseline）
    { "source": "narrator", "text": "…" },
  ],
}
```

- **接地の正直さ（verify-first）**: `turn_intent` はトレースに assistant の
  reasoning テキストが無いため `null`。`result.digest` はトレースに実在する
  tool result / error からのみ要約。「接地が無い」ことも記録として残す。
- fixture は実リハーサル trace（VM `~/.hermes/kai_trace/`）＋当時の Issue 本文から
  再構成（`docs/kai/narration/fixtures/` の 2 本）。増やしていく。
- **記録済み発話は fixture とは別軸**: `expected_or_recorded` として各操作に紐づく。
  eval は既定でこの `narrator` 発話列を採点し、`--candidates` でこれを差し替える。

## スコアの意味（FR 機械チェック）

各 FR は `02-requirements.md` の受け入れ基準に対応。`eval.py` は正規表現＋
ヒューリスティックの**機械チェック**のみ（LLM ジャッジは 03-design §2.5 の将来分）。

| 項目                 | 何を測るか                                                                                      | 良い方向     |
| -------------------- | ----------------------------------------------------------------------------------------------- | ------------ |
| **FR1** 一人称       | 三人称語（「エージェント」「AI が」）＋禁止一人称（僕/私/わたし）の出現数。※「ボク」は許可      | 0 で PASS    |
| **FR5** ID/生データ  | commit hash・`feature/…` slug・todo ID（`issue55-verify` 等）・生 Issue/PR 番号・生 JSON の漏れ | 0 で PASS    |
| **FR6** 反復         | 直近 3 発話との文字 bi-gram Jaccard・文末パターン重複率                                         | 低いほど良い |
| **FR7** 操作説明のみ | 理由/結果反応/感情/間投詞のいずれも含まない発話の割合（間投詞辞書＝01-target §1.2）             | 低いほど良い |
| **FR9** 長さ         | 平均/最長/最短文字数（20〜80 字目安、読み上げ占有率の代理）                                     | range 内     |
| **confabulation**    | 発話語彙が接地（args/Issue 本文/結果）に無い語で、**複数発話に反復** → 作り話の疑い             | flag=なし    |
| **composite**        | 上記を重み付けした 0〜100 の便宜値。**真の判定は各 FR の pass/率**（P1）                        | 高いほど良い |

出力: fixture ごとに FR 別スコア＋総合＋「悪かった発話 Top5（理由付き）」を
人間可読（stdout）と JSON（`--json`）で。

### confabulation ヒューリスティックの要点

発話から漢字/カタカナの内容語（2 字以上）を抜き、接地テキスト（全 args＋Issue
本文＋result.digest＋turn_intent）に部分一致しない語を「接地外」とする。generic な
語（状態/確認/準備…）と、2 字語幹が接地にある複合語（変更点←変更）は除外。
**接地外語が 2 発話以上に反復**したら session フラグを立てる。#65 の「ズレ」
（Issue #65 本文に無い）が 3 回出るのを検出する回帰ケース。

## 候補生成を繋ぐ場所（★生成器はここに差す）

**`generate.py` が実装済みの生成器**（Issue #73 で接続）: 実プラグイン
（`plugins/kai_narrator/__init__.py`）のプロンプト・後処理をそのまま import し、
OpenAI 互換エンドポイント（llama.cpp 等）に temp=0 で投げて candidates JSON を出す。
プロンプト変更は必ずこれで現行と比較してから採用する（実測記録 =
[`results-issue73.md`](./results-issue73.md)）。

```bash
python3 generate.py --fixture <fixture.jsonl> --base-url http://<llm-host>:8080/v1 --out gen.json
python3 eval.py --fixture <fixture.jsonl> --candidates gen.json
```

なお候補の `"SKIP"`（plugin の沈黙センチネル）は「発話されなかった」扱いで採点から
除外され、件数が `skipped_candidates` として報告される。

生成器を繋ぐ口は 2 箇所:

1. **`eval.py --candidates FILE.json`** — 生成器の出力をそのまま採点する。
   `FILE.json` は**記録済み narrator 発話と同じ順序・同数**の JSON 配列。各要素は
   文字列または `{"text": "…"}`。fixture の操作・接地対応はそのまま流用される。
   実装は `apply_candidates()`（eval.py）。

   ```jsonc
   // gen.json — op ごとに生成器が返した実況（順序は fixture の narrator 発話と一致）
   ["65番の課題を画面に出すね。…", "書けたよ。後片付けを3行足したんだ", …]
   ```

2. **fixture の `expected_or_recorded`** — レコーダー（03-design §2.1・今回未実装）が
   ドライラン中の接地パイプラインから録る本番忠実な入力。生成器を実機に載せる前段。

いずれも eval 側は「発話列（＋接地）」を受け取るだけで、生成ロジックには依存しない。

## 制約 / 範囲

- **標準ライブラリのみ**（pip 追加なし）。`python3 -m py_compile eval.py` が通ること。
- core（`run_agent.py` 等）は不変。すべて `kai-services/narration-eval/` と
  `docs/kai/narration/` 配下（NFR3 Footprint Ladder）。
- 機械チェックのみ。LLM ジャッジ（confabulation の精査・理想カタログ近接度）は
  03-design §2.5 の次段。FR3/FR4/FR8 の一部は接地レコーダー実装後に追加余地。
