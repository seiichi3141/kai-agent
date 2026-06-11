# 複数リポジトリ開発オーケストレーター計画

## 目的

Hermes を、複数リポジトリにまたがる開発作業の受付、進行管理、実行委譲、
報告を行う開発オーケストレーターとして使えるようにする。

対象作業:

- Issue の調査、対応、修正ブランチ作成、PR 作成。
- バグ調査、再現確認、修正方針の提案。
- 新規 Issue 起票。
- 複数リポジトリでの並行作業。
- 作業開始、進捗、完了、blocked の報告。
- STT/TTS による音声問い合わせと音声報告。
- 作業中リポジトリや task worktree を VS Code で開く操作。

## 既存資産

- Hermes Kanban:
  - task 永続化、status、worker、dashboard、通知購読の土台。
  - 複数 task の並行実行に向く。
- `live_coding_delegate`:
  - Codex CLI へ one-shot task を委譲できる。
  - 現状は配信 overlay 向けの最小 coordinator。
- GitHub skills:
  - Issue 閲覧、Issue 起票、PR 作成、PR review の手順がある。
- Codex / Claude Code skills:
  - コーディング作業を外部 coding agent に委譲する手順がある。
- Voice / TUI:
  - Deepgram STT で音声入力を agent turn にできる。
  - TTS で agent 応答を音声化できる。
  - OBS overlay へ caption と状態を出せる。

## 基本方針

開発作業の永続状態は memory provider ではなく Kanban DB に置く。

- Kanban:
  - 現在の task、repo、worktree、status、run、summary、blocked reason。
- Memory:
  - repo ごとの慣習、ユーザーの好み、繰り返し使う運用ルール。
- Skills:
  - Issue 対応、PR 作成、Codex 委譲、配信時の説明方針などの手順。
- Docs:
  - 設計判断、未解決課題、運用方針。

## 用語

- `repo_id`: Hermes 内で対象リポジトリを呼ぶ短い名前。例: `hermes-agent`, `kai`。
- `repo registry`: `repo_id` から local path / GitHub repo / default branch などを引く設定。
- `dev task`: repo に紐づく開発作業。内部的には Kanban task。
- `worker`: dev task を実行する Hermes worker または Codex lane。
- `worktree`: task ごとに隔離された作業ディレクトリ。
- `voice notification`: task event を短く TTS で読み上げる通知。

## 設定案

```yaml
dev_orchestrator:
  enabled: true
  default_worker: codex  # codex | claude (Claude Code) | hermes
  worktree_root: ~/.hermes/dev-worktrees
  require_approval_for_issue_create: true
  require_approval_for_pr_create: true
  require_approval_for_push: true
  require_approval_for_commit: false
  voice_notifications:
    enabled: true
    max_chars: 120
    cooldown_seconds: 10
    report_started: true
    report_completed: true
    report_blocked: true
    report_failed: true
  vscode:
    command: code
    fallback_macos_app: Visual Studio Code

repositories:
  hermes-agent:
    local_path: /Users/seiichiro/apps/seiichi3141/hermes-agent
    github: seiichi3141/hermes-agent
    default_branch: main
    worktree_root: ~/.hermes/dev-worktrees/hermes-agent
    worker: codex
  kai:
    local_path: /Volumes/ExSSD/apps/seiichi3141/kai
    github: seiichi3141/kai
    default_branch: main
    worktree_root: ~/.hermes/dev-worktrees/kai
    worker: codex
```

`repositories` は後で別ファイルへ分離してもよい。初期実装では
`config.yaml` に置く。

## コマンド案

### Repository Registry

```text
/dev repos
/dev repo add <repo_id> <local_path> [--github owner/repo]
/dev repo remove <repo_id>
/dev repo show <repo_id>
```

### Task 作成

```text
/dev assign <repo_id> <task>
/dev issue <repo_id> <title/body>
/dev investigate <repo_id> <topic>
/dev fix <repo_id> <issue_number_or_description>
```

初期版では `/dev assign` に集約してよい。`issue`、`investigate`、`fix` は
後から alias / template として追加する。

### Status

```text
/dev status
/dev status <task_id>
/dev tasks
/dev tasks --repo <repo_id>
/dev current
```

音声問い合わせ例:

```text
kai の作業状況を教えて。
hermes-agent の PR 作成は終わった？
今動いているタスクを読んで。
blocked になっている作業はある？
```

### VS Code 操作

```text
/dev open <repo_id>
/dev open <task_id>
/dev open current
```

動作:

- `<repo_id>`: registry の `local_path` を VS Code で開く。
- `<task_id>`: task の `worktree_path` があればそれを開く。
- `current`: running task、なければ直近 task を開く。

実装:

```bash
code /path/to/repo
```

fallback:

```bash
open -a "Visual Studio Code" /path/to/repo
```

VS Code を開く操作はユーザーの画面に見えるため、音声または明示コマンドで
依頼された場合のみ実行する。

## 音声 UI

### 問い合わせ型

ユーザー発話を STT で受け、通常の agent turn として処理する。

例:

- 「kai の作業状況を教えて」
- 「今動いているタスク一覧を読んで」
- 「hermes-agent の作業フォルダを VS Code で開いて」

必要な実装:

- agent が `/dev status` 相当の情報を tool / command から取得できる。
- 音声応答は短くする。
- 詳細は TUI / overlay / kanban dashboard へ誘導する。

### 報告型

Kanban task event を voice notification queue に流し、TTS で短く報告する。

対象 event:

- task created
- task claimed / started
- worker heartbeat stale
- blocked
- failed
- completed
- PR created
- Issue created

例:

```text
kai の調査を開始しました。
hermes-agent の修正が完了しました。テストは通っています。
mobile-app の作業が blocked です。認証情報が必要です。
```

制御:

- ユーザー発話中は割り込まない。
- TTS 再生中は queue に積む。
- 同じ repo の通知は cooldown する。
- 長い詳細は TTS に乗せず、overlay / dashboard に出す。
- 配信中は秘密情報、private path、未公開仕様を読み上げない。

## Worker 実行方針

初期版では Codex を primary worker にする。

1. repo registry から local path を解決する。
2. default branch を fetch する。
3. task 専用 branch / worktree を作る。
4. Codex に one-shot prompt を渡す。
5. Hermes が diff とテスト結果を確認する。
6. task summary を Kanban に保存する。
7. 必要なら commit / PR 作成を行う。

安全ルール:

- shared dirty checkout では直接作業しない。
- `.env`、secret、private key、token は読まない。
- PR 作成、Issue 起票、push は初期設定では明示承認を必要にする。
- commit は設定で許可する。初期値は `require_approval_for_commit: false` でもよいが、
  push は必ず承認制にする。
- Codex の自己申告だけで task done にしない。Hermes が diff / tests を確認する。

## GitHub 操作

GitHub 操作はまず `gh` CLI を優先する。

- `gh issue view/list/create`
- `gh pr create/view/status`
- `gh repo view`

fallback は既存 GitHub skills の curl / git 手順に従う。

外部公開アクション:

- Issue 起票。
- PR 作成。
- PR コメント。
- push。

これらは、少なくとも初期実装では confirmation を挟む。

## データモデル案

Kanban task body または metadata に以下を入れる。

```json
{
  "kind": "dev_task",
  "repo_id": "kai",
  "github": "seiichi3141/kai",
  "local_path": "/Volumes/ExSSD/apps/seiichi3141/kai",
  "worktree_path": "~/.hermes/dev-worktrees/kai/t123",
  "branch": "dev/t123-fix-tts",
  "issue": 123,
  "pr": null,
  "worker": "codex",
  "requested_by": "voice",
  "notify_voice": true,
  "last_reported_event_id": null
}
```

初期実装では Kanban の既存 schema を壊さないため、metadata JSON へ入れる。

## Overlay / TUI 表示

dev overlay state:

- active repo
- active task
- worker status
- branch
- test status
- PR / Issue URL
- blocked reason
- next action

TTS は短い報告のみ。overlay は詳細を表示する。

## Memory の使い方

holographic memory provider を使う場合、以下のような fact を明示保存する。

- `kai は AquesTalk の読み変換にローカルLLMを使う。`
- `hermes-agent では細かいコミットはユーザー指示時のみ行う。`
- `PR 作成と push は明示承認を必要とする。`
- `ゲーム実況配信中は秘密情報を TTS / overlay に出さない。`

現在の task 状態は memory に入れない。Kanban を canonical source にする。

## 実装フェーズ

### Phase 1: Repo Registry

- [x] `dev_orchestrator` config を追加する。
- [x] `repositories` config を読み込む helper を追加する。
- [x] repo_id、local_path、github、default_branch を検証する。
- [x] `/dev repos`、`/dev repo show` を追加する。
- [x] tests を追加する。

検証:

- 複数 repo を一覧できる。
- 存在しない path / git repo で分かりやすい error を返す。
- GitHub owner/repo を remote から推定できる。

### Phase 2: Dev Command MVP

- [x] `/dev assign <repo_id> <task>` を追加する。
- [x] Kanban task を `kind=dev_task` metadata 付きで作る(body 内の
      ```dev-task-meta``` JSON ブロック、tenant=dev、assignee=worker)。
- [x] `/dev tasks`、`/dev status` を追加する。
- [ ] 音声問い合わせで status を短く返せるようにする。

検証:

- TUI から dev task を作れる。
- task が Kanban dashboard に出る。
- `/dev status` が running / blocked / done を返す。

### Phase 3: VS Code Open

- [ ] `/dev open <repo_id|task_id|current>` を追加する。
- [ ] `code` CLI を使う。
- [ ] macOS fallback として `open -a "Visual Studio Code"` を使う。
- [ ] path が repo registry または task metadata 由来であることを確認する。

検証:

- repo root を VS Code で開ける。
- task worktree を VS Code で開ける。
- 不明 repo_id / task_id では起動しない。

### Phase 4: Worker / Codex Lane

- [x] task 専用 worktree を作る(`/dev run <task_id>`、branch は `dev/<task_id>`)。
- [x] Codex / Claude one-shot worker を起動する(live_coding の delegate_argv を共用)。
- [x] worker log / exit status を Kanban run に保存する。
- [ ] Hermes が diff / tests を確認する(現状は change summary の記録まで。
      テスト実行による検証は未実装)。
- [x] task complete / block を保存する(成功 → done、失敗/timeout → blocked)。

検証:

- 2つの repo / 2つの task を並列実行できる。
- 片方が失敗しても他方に影響しない。
- dirty checkout を汚さない。

### Phase 5: GitHub Issue / PR

- [x] `gh` CLI の存在を確認し、auth エラーは gh の出力をそのまま表示する。
- [x] Issue view/list helper を追加する(`/dev issue <repo_id> [list|<number>]`、
      `/dev assign <repo_id> --issue <n>` で Issue から task 作成)。create は未実装。
- [x] PR create helper を追加する(`/dev pr <task_id>`)。
- [x] PR 作成 / push の confirmation を入れる(`--confirm` なしはプレビューのみ。
      uncommitted 変更の auto-commit は require_approval_for_commit が false の時のみ)。
- [x] URL を task metadata に保存する(`pr` / `issue` / `issue_url`)。

検証:

- Issue から task を作れる。
- task 完了後に PR を作れる。
- PR URL が `/dev status` と overlay に出る。

### Phase 6: Voice Notifications

- [x] Kanban task event watcher を追加する(hermes_cli/dev_notify.py。cursor は
      task metadata の last_reported_event_id。/voice on で起動、off で停止)。
- [x] voice notification queue を追加する(バッチ内は repo ごとに最新のみ読み上げ。
      cooldown 中の通知は queue ではなく drop)。
- [x] TTS 再生中は通知を待たせる(classic voice の _tts_playing を wait。
      streaming TTS worker との調停は未対応)。STT 録音中の保留も未対応。
- [x] cooldown と max chars を適用する。
- [x] overlay に通知詳細を出す(publish_caption speaker=assistant, ttl=8s)。

検証:

- task started / completed / blocked が TTS で報告される。
- ユーザー発話中に割り込まない。
- 同じ通知が繰り返し読まれない。

### Phase 7: Dashboard / Overlay Polish

- [ ] dev task 専用の overlay state を追加する。
- [ ] active repo / task / PR / test status を表示する。
- [ ] dashboard から repo / task を開ける導線を検討する。

検証:

- 配信中に現在の作業が一目で分かる。
- 詳細は TTS ではなく overlay / dashboard に逃がせる。

## 最小実装順

1. Repo registry。
2. `/dev repos`、`/dev status`。
3. `/dev open`。
4. `/dev assign` で Kanban task 作成。
5. task event の TTS 報告。
6. Codex worker 実行。
7. GitHub Issue / PR 連携。

先に `/dev open` まで作ると、音声で「kai を開いて」「今の作業を開いて」が
すぐに使える。並行実行と PR 作成はその後でよい。

## 未決事項

- `/dev` は CLI/TUI slash command にするか、Kanban の alias として実装するか。
- repo registry を `config.yaml` に置くか、`~/.hermes/repositories.yaml` に分けるか。
- worker は Hermes worker を主にするか、Codex lane を主にするか。
- commit を自動許可するか。push / PR は承認制でよい。
- voice notification を TUI gateway 内で実装するか、Kanban dispatcher 側に寄せるか。
- GitHub connector / `gh` CLI / REST fallback のどれを primary にするか。
