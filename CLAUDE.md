# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

このリポジトリは **Hermes Agent**（Nous Research 製の自己改善型 AI エージェント）の fork で、この上に AITuber「kai」を実装します（要件は `docs/kai/requirements.md`）。

## ブランチ戦略（2026-07-05 改訂）

- **`main`** — kai の稼働・アップデート用メインブランチ（GitHub デフォルト）。kai 固有の修正はすべてここに積む。GCP / kai-vm 上の kai はこのブランチを pull / self-update する。PR のマージ先。
- **`upstream`** — upstream（NousResearch/hermes-agent、remote 名も `upstream`）追従ミラー。**kai のコミットを載せない**。`upstream/main`（remote-tracking）から fast-forward のみ。
- **upstream 追従** — `upstream/main`（remote）→ `upstream`（ブランチ、ff）→ `main` へ merge、の一方向。
- **機能開発** — `feature/*` ブランチ → PR → `main`。
- **注意（命名の重なり）** — remote 名 `upstream` とブランチ名 `upstream` が同名。`git switch upstream` はブランチ、`upstream/main` は remote-tracking を指す。曖昧な操作では `origin upstream` / `upstream main` のように remote を明示する。
- 旧 `kai/main` は廃止済み（今後は使わない）。

## 開発の鉄則（loop contract）★最優先

Loop Engineering の原則（詳細と要件は `docs/kai/loop-engineering.md`）。**kai は自己改善エージェントであり、これらは他のどの規約より優先する。**

- **P1. 検証器に接地する。** 「完了した」の根拠は常に検証器の pass/fail（`scripts/run_tests.sh` / `node --test` / `scripts/kai-docs-lint.sh` / `ruff` / `ty` / `shellcheck` / CI / `gh pr` の実状態）。**エージェントの「できました」という自己申告は完了の根拠にしない。**
- **P1b. やっていないことを「やった」と書かない。** ツールを実行していないなら実行していないと言う。ツール出力を要約・再構成する際に、実行していない検証の成功を作文しない（2026-07-05 の捏造インシデントの再発防止。[[fabrication-incident-verify-first]]）。不確かなら「不確か」と明示し、`ls`/`cat`/`git log` の生出力で裏を取る。
- **P2. 検証器を緩めない・隔離する。** 実装を直すべき場面でテストを甘くしない。テスト（検証器）は実装ループと別に扱い、報酬ハックを断つ。
- **P3. `main` は保護する。** 原則 PR → CI 緑 → merge。CI が最終ゲート。高リスク変更のマージ承認は人間（またはオーナー承認の隔離レビュー）。
- **P4. 「テスト緑＝完了」ではない。** 重要変更は隔離コンテキストの懐疑的レビュー（`deep-reasoner` 等）を通してから PR。
- **P5. 証跡を残す。** 全 run のログ・トランスクリプト（`kai_trace`）・（配信/UI 変更時は）スクリーンショットを永続化する。

## 正典は AGENTS.md

開発ガイドの正典はルートの **`AGENTS.md`（71KB）** です。貢献ルーブリック、Footprint Ladder、アーキテクチャ、各サブシステムの詳細はすべてそちらにあります。**作業前に `AGENTS.md` を読んでください。**

以下は最低限のポインタのみ:

- **2 つの絶対原則** — (1) 会話単位のプロンプトキャッシュは不可侵、(2) コアは narrow waist で機能はエッジ（plugin/skill/CLI）に置く。詳細は `AGENTS.md` 冒頭と "The Footprint Ladder"。
- **テスト** — 必ず `scripts/run_tests.sh` 経由（`pytest` 直叩き禁止）。単一テストは `scripts/run_tests.sh tests/agent/test_foo.py::test_x`。
- **Lint / 型** — `ruff check .` と `ty check`（設定は `pyproject.toml`）。
- **kai ドキュメントの lint / format** — `scripts/kai-docs-lint.sh [--fix]`（prettier + markdownlint。対象は kai 所有ファイルのみ = `docs/kai/`・`CLAUDE.md`・`.claude/agents/`。**upstream のファイルは整形しない** — merge コンフリクト防止のため `.prettierignore` は allowlist 方式）。
- **TypeScript**（`ui-tui` / `apps/desktop` / `web`）— `ui-tui` で `npm run dev|build|typecheck|lint|test`。詳細は `AGENTS.md` の TUI / Desktop セクション。
- **設定** — 非機密は `config.yaml`（`hermes_cli/config.py` の `DEFAULT_CONFIG`）、機密のみ `.env`。新規 `HERMES_*` env var の追加は禁止。
- **プロファイル対応** — パスは `get_hermes_home()` / `display_hermes_home()` を使い、`~/.hermes` をハードコードしない。
- **主要ファイル** — `run_agent.py`（AIAgent コアループ）、`cli.py`、`model_tools.py`、`toolsets.py`、`hermes_state.py`、`hermes_cli/commands.py`（スラッシュコマンドの正典レジストリ）。
- **コミット** — Conventional Commits（例: `fix(cli): ...`）。

## Orchestration workflow

あなた（Fable）はオーケストレーターです。計画、分解、統合を行います。

- 推論の重いフェーズ（アーキテクチャ、複雑なデバッグ、アルゴリズム設計）→ `deep-reasoner` サブエージェント（Opus）
- 機械的な作業（boilerplate、テスト、フォーマット、単純な編集）→ `fast-worker` サブエージェント（Sonnet）
- Codex（`/codex:rescue --background`）は deep-reasoner に匹敵する優秀なエンジニアで、異なる視点を持つ。レビュアーではなく**ピア**として扱う。
- 高リスクの決定: 同じ問題を Opus と Codex に**並行して**タスクし、互いの回答を見せずに、両者の最良の部分を統合する。
- 自分（オーケストレーター）のコンテキストは軽く保つ。ファイルの大量読み込みや探索はサブエージェントに委譲する。
