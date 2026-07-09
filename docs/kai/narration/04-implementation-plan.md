# 実況再設計 — 実装計画（レビュー反映版）

2026-07-09 の懐疑レビュー（Opus × Codex、独立で強く一致）とハーネスのベースラインを反映した、
実装の正典。`03-design.md` の一部主張はこの文書で**上書き**する（下記「設計の訂正」）。

## 設計の訂正（レビューで判明した load-bearing な誤り）

1. **接地に core 改変は不要。** 接地材料は既に hook でプラグインに届いており、narrator が捨てているだけ。
   → 「no core diff」を**ハードゲート**にする（`03-design.md §2.1` / `02-requirements NFR3` を上書き）。
   - `post_tool_call` は既に `result` を渡す（`model_tools.py:884-889`）。`kai_trace`/`langfuse` は受けて記録済み
     （`plugins/kai_trace/__init__.py:157,164`）。narrator の `_on_post_tool_call` が `**_` で捨てている。
   - full `args` も既に来る（`model_tools.py:887`）。`_ARG_KEYS`（`plugins/kai_narrator/__init__.py:174`）が
     `content/new_string/old_string/todos` を落としているだけ。
   - **本体の"本物の意図テキスト"**は `post_api_request` hook で観測可能
     （`assistant_message.content` ＋ `tool_calls`。`agent/conversation_loop.py:4100-4134`,
     payload は `run_agent.py:2404-2415`）。narrator は未購読（登録は `plugins/kai_narrator/__init__.py:612-616`）。
2. **観測者に"意図・感情"を創作させない（confabulation を移動させない）。** 観測者は
   **接地事実（本物の意図テキスト＋ツール＋結果）の短い一人称翻訳＋SKIP** に限定。作話余地を断つ。
3. **「kickoff＝本体の声」は自律ループで不成立。** 本体はターン終端で 1 回しか喋らない
   （`agent/turn_finalizer.py:365-380`）。冒頭の Issue 説明は**観測者**が担う（`gh issue view` の結果は
   既に `post_tool_call.result` にある）。本体の声は**末尾サマリ**に限定。
4. **`pre_llm_call` は観測専用ではない（地雷）。** 返り値 `{"context": …}` は user message に注入され
   会話・キャッシュに触れる（`agent/conversation_loop.py:431-449`）。narrator の `_on_pre_llm_call` は
   現状 `None` を返す（`:574-577`）。**全 hook が None を返すことをテストで固定**する。
5. **秘密漏洩サーフェスが増える（新規）。** ツール結果を材料にすると `.env` の read、token を echo した
   terminal の結果が実況 LLM/字幕へ流入しうる。`_mask`（`:83-91`）はファイル本文中の秘密を捕まえない。
   → 結果は**短いダイジェストに切る**＋機微 read の denylist ＋秘密を仕込んだ**漏洩回帰 fixture**。
6. **eval は非決定を排す。** 生成は `temperature=0`。「録音発話のスコア＝参考値」と
   「同一 fixture を新生成ロジックに通す＝本命ベースライン」を分離する。
7. **penalty は `call_llm` の引数に無い。** `frequency/presence_penalty` は `extra_body` 経由
   （`agent/auxiliary_client.py:5922-5939`）。

## ベースライン（現状・ハーネス実測）

| fixture       | 総合   | confabulation                                             |
| ------------- | ------ | --------------------------------------------------------- |
| #65（失敗例） | 33/100 | ⚑ 検出（ズレ×3。Issue 本文・patch に「ズレ/表示」は不在） |
| #55（良好例） | 72/100 | クリア                                                    |

改善は「#65 の総合を上げ、ID 漏れ 0、confabulation フラグを消す」を数値目標にする。

---

## Phase 1 — プラグインだけの接地修正（core 非改変・ハーネス不要）

すべて `plugins/kai_narrator/__init__.py` 内。core（`run_agent.py` 等）は触らない。

1. **本体の意図を捕捉**: `post_api_request` を購読し、各イテレーションの `assistant_message.content`
   （kai の本物の思考/宣言）を AgentActivity バッファに積み、直後の tool イベントに束ねる。
2. **結果を受ける**: `_on_post_tool_call` に `result` 引数を追加し、**tool 別の構造化ダイジェスト**を作る
   （1 フィールド抽出をやめる）:
   - `todo` → `todos[].content` の要点、`write_file`/`patch` → `content`/`new_string` の要点、
     `terminal` → `command` ＋ 結果ダイジェスト、`read_file`/`search_files` → 対象 ＋ 結果ダイジェスト。
   - 結果ダイジェストは**境界長で切る**＋秘密マスク＋機微 read の denylist。
3. **一人称ログへ整形**: 「ボクが今考えてること／今やったこと／今つかんだこと（結果）」の形に束ねて
   narration LLM に渡す（元 kai `formatActivityBuffer` 相当）。
4. **sanitize 強化**: 内部 ID（hash・`feature/…` slug・`issue55-verify` 型 todo ID・生 JSON）→ 人間語 or 除去、
   path→basename、秘密マスク。発話直前に必ず通す。
5. **プロンプト刷新**: 「接地事実の短い一人称翻訳。材料が無ければ SKIP。結果は観測後のみ断定。
   payload に無い原因を推測しない」に絞る（現行の「目的か結果を必ず 1 つ」強制＝`:228` を廃す）。
   `<log>`/`<body>` XML タグでデータと指示を分離。
6. **hooks は None 固定**: 全 hook が `None` を返すことを保証（特に `_on_pre_llm_call`）。
7. **penalty**: `extra_body={"frequency_penalty":0.6,"presence_penalty":0.3}` を narration 呼び出しに付与。

### Phase 1 の検証（loop contract P1）

- `kai-services/narration-eval/eval.py --candidates` に Phase 1 narrator の生成（temp=0）を通し、
  #65 の総合が基準線 33 を明確に上回る／confabulation フラグが消える／ID 漏れ 0 を確認。
- **秘密漏洩回帰**: ツール結果に秘密を仕込んだ fixture で、発話に秘密が出ないことをテスト。
- narrator 全 hook が None を返すテスト。
- 実機ドライラン（kai-vm・OBS）で trace＋スクショ＋発話を残し、人＋懐疑レビュー。
- `scripts/kai/verify.sh` 緑、`scripts/run_tests.sh` 該当テスト緑。

## Phase 2 — 薄い回帰ハーネス（骨組みは実装済み）

`kai-services/narration-eval/`（eval.py・fixtures・baseline）は構築済み。追加で:

- 秘密漏洩 fixture、生成 temp=0 の候補ランナー、録音＝参考値／再生成＝本命の分離を明文化。
- 較正済み LLM ジャッジは**任意**（機械チェックで足りない FR2/FR7 品質を補うときだけ）。

### confabulation 防御の位置づけ（Issue #75 で確定）

接地材料（Phase 1）は confabulation 防止の**必要条件だが十分条件ではない**。防御は三層:

1. **runtime の機械ゲート**（`plugins/kai_narrator/` — 生成前: 薄い材料で LLM を呼ばない
   `_material_is_thin` / 生成後: 接地外の具体的主張を落とす `_is_grounded`・近似反復を落とす
   `_too_similar`）。軽量ヒューリスティックであり完全ではない（precision 優先＝過剰抑制しない側に倒す）。
2. **narration-eval ハーネスが本命バックストップ**。プロンプト・ゲートの変更は必ず
   `generate.py`（temp=0・実プラグインのプロンプト）→ `eval.py` の数値比較を通してから採用する。
3. penalty（`frequency/presence_penalty`）は**バックエンド依存**: ローカル OpenAI 互換
   （llama.cpp）では実効を A/B 実測済み。**openai-codex（本番 narration の実解決先）では
   auxiliary_client の `_CodexCompletionsAdapter` が extra_body から reasoning しか変換せず
   黙って捨てる**（コード実測 2026-07-09）。よって反復抑制は penalty に依存せず、
   `_too_similar`（bigram Jaccard ≥ 0.5）が機械層として常に効く。実測記録 =
   `kai-services/narration-eval/results-issue75.md`。

## Defer（ドライランで残課題が見えてから）

recorder（`turn_intent` 充填）・本格 LLM ジャッジ・phrase-bank・翻訳 allowlist 一式・3 フェーズ・
アバター emotion/motion。big-bang 移植はしない（改善が不可分になり原因分析ができなくなるため）。
