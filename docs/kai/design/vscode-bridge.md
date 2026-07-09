# kai VSCode ブリッジ + 自作ツール 基本設計書

- **ステータス:** ドラフト
- **作成日 / 更新日:** 2026-07-06
- **満たす要件:** Issue #49（本設計の親）。関連: #46（指示プロンプトを隠す）/ #47（冒頭説明を Desktop ブラウザで）/ #48（コマンドを見える端末で実行・実況と同期）
- **マイルストーン:** M4（配信演出の完成）
- **種別:** plugin（ツール override）+ VSCode 拡張（ブリッジ）+ 運転台本
- **配置:** `plugins/kai_ide/`（新）、`kai-services/streaming/vm/vscode/kai-typewriter/`（拡張、ブリッジに拡張）、`docs/kai/m4-runbook.md`

## 0. 背景 — なぜ自作ツールか

第 3 回リハーサル（`docs/kai/stream-review/test-live-03.md`）のオーナーレビューで、
配信を「人が Linux Desktop を操作しながらライブコーディングしている画」にしたい、
という方向性が示された。現状は:

- kai の編集・コマンド実行は hermes が**内部で高速・無音実行**し、配信画面には
  演出（kai-typewriter のタイプ再生・コマンドログ）を後付けしている。
- 「本物の操作」ではなく、replay（再描画）。git 操作などが見えない。

選択肢:

| 方式                                       | 速度     | 確実性 | 見た目   | gpt-5.5(Codex) |
| ------------------------------------------ | -------- | ------ | -------- | -------------- |
| ① 内部ツール + 演出（現行）                | 速い     | 高い   | replay   | 実績あり       |
| ② computer_use（GUI 自動化）               | **遅い** | 低め   | 本物     | 要検証         |
| ③ **自作ツールで VSCode 直操作（本設計）** | 速い     | 高い   | **本物** | 実績あり       |

オーナー方針（2026-07-06）: **computer_use は汎用だが速度を犠牲にしすぎ。Codex が
返すツールごとに、VSCode を高速に操作する専用ツールを自作する。** ③ を採る。

前提（役割整理）: **判断 = Codex(gpt-5.5) / 実行 = hermes**。hermes の plugin は
`ctx.register_tool(override=True)` で built-in ツールを自作実装に差し替えられる
（`plugins.entries.<id>.allow_tool_override: true` で opt-in）。ゆえに Codex が
`write_file` を呼べば kai の VSCode 操作版が走る。

## 1. 目的と責務

kai（hermes）のファイル編集・コマンド実行を、配信画面の VSCode を**実際に操作**
する形で行い、かつ VSCode の状態を読んで UI 判断（不要タブを閉じる・見せたい
ファイルを開く）ができるようにする。computer_use を使わず、拡張 API 経由の
高速・確実な操作で「本物のライブコーディング」に見せる。

**やらないこと（非責務）:**

- 実況の生成・発話（kai_narrator / speechd の責務）
- タイピング演出そのもの（既存 kai-typewriter を拡張・流用する）
- ブラウザで見せる操作（stream-browser.py #11 の責務。ブリッジからは呼ぶだけ）
- hermes コアの改変（plugin のツール override と拡張で完結させる）

## 2. 配置と Footprint Ladder

- **選んだ段:** ④ plugin（ツール override）+ 既存 VSCode 拡張の拡張
- **理由:** hermes は plugin にツール override 機構（`register_tool(override=True)`）を
  提供しており、コア改変なしで built-in を差し替えられる。VSCode 操作は既存の
  kai-typewriter 拡張（127.0.0.1:8920）を「ブリッジ」に拡張して担う。
- **コア改変:** なし（override は plugin API の正規機能。`allow_tool_override` で opt-in）

## 3. インターフェース

### 3.1 提供するインターフェース

#### (A) VSCode ブリッジ（拡張の HTTP。127.0.0.1:8920）

```text
GET  /state
  → {"active": {"path": "...", "line": 42, "column": 0, "dirty": false},
     "tabs": [{"path": "...", "active": true, "dirty": false, "group": 0}, ...],
     "visibleEditors": ["path", ...]}

POST /open      {"path": "/abs/path", "line": 42}   → ファイルを開き該当行へ
POST /close     {"path": "/abs/path"} | {"all": true} → タブを閉じる
POST /edit      {"edits": [{"path, action}]}         → 既存（タイプ再生）
POST /terminal  {"command": "...", "id": "..."}      → 統合ターミナルで実行（§5.2）
POST /scroll    {"direction": "down|up"}             → アクティブエディタをスクロール
```

すべて 127.0.0.1 のみ bind（配信 VM のローカル）。秘匿情報は扱わない（コマンド・
パスはツール側でマスク済みを渡す）。

#### (B) kai のツール（plugin `kai_ide` が登録）

| ツール             | override           | 動作                                                 |
| ------------------ | ------------------ | ---------------------------------------------------- |
| `write_file`       | ○（built-in 差替） | ディスク書込 + `/edit` でタイプ表示                  |
| `patch`            | ○                  | 同上（差分をタイプ）                                 |
| `terminal`         | ○                  | `/terminal` で統合ターミナルで実行・出力捕捉（§5.2） |
| `vscode_state`     | 新規               | `/state` を返す。kai が UI 判断に使う                |
| `vscode_open`      | 新規               | `/open`。見せたいファイルを開く                      |
| `vscode_close_tab` | 新規               | `/close`。不要タブを閉じる                           |

`read_file` は override しない（高頻度・出力が大きい）。kai は見せたい時だけ
`vscode_open` を呼ぶ（`vscode_state` で判断）。

### 3.2 依存するインターフェース

| 依存先                 | 形式                     | 用途                     | 不達時の挙動                                                 |
| ---------------------- | ------------------------ | ------------------------ | ------------------------------------------------------------ |
| kai-typewriter 拡張    | HTTP (127.0.0.1:8920)    | VSCode 操作・状態        | 拡張不在（配信外）→ 各ツールは内部処理にフォールバック（§6） |
| tmux（統合ターミナル） | send-keys / capture-pane | terminal の可視実行      | 取得失敗 → subprocess 実行にフォールバック                   |
| built-in ツール実装    | Python 関数              | フォールバック時の実処理 | —                                                            |

## 4. データモデル

- ブリッジは状態を持たない（VSCode の API を都度読む）。
- `terminal` の実行同期用に一時マーカーファイル（`/tmp/kai-term-<id>.done`）を使う（§5.2）。

## 5. 処理フロー

### 5.1 write_file / patch（override）

1. **まずディスクへ実書き込み**（built-in の実装を呼ぶ。verify.sh 等が正しい実体を
   見られるようにする。これが最優先）。
2. 成功したら拡張の `/edit` を叩く（既存 kai-typewriter がタイプ再生。add/update 判定は
   既存ロジック）。拡張不在なら 2 をスキップ（内部書込のみ = 現行と同じ）。
3. built-in と同じ戻り値を Codex に返す（契約互換）。

**原則:** 演出（/edit）が失敗しても実書き込みと戻り値は保証する。

### 5.2 terminal（override）— 統合ターミナルで実行（#48 の核）

配信画面の統合ターミナル（tmux の作業ペイン）で**実際に実行**し、出力を捕捉する。
replay ではなく本物の 1 回実行。

1. コマンドを秘匿マスクし、実行同期用に完了マーカー付きで組み立てる:
   `<command>; printf 'KAI_EXIT:%s\n' "$?" > /tmp/kai-term-<id>.done`
2. `tmux send-keys -t <作業ペイン> -l '<上記>' && tmux send-keys Enter`
   （※ ここで初めて実行される。hermes の内部 subprocess は使わない）
3. `/tmp/kai-term-<id>.done` の出現をポーリング（タイムアウトあり）。
4. `tmux capture-pane` で出力を、マーカーの exit code で結果を取る。
5. Codex には built-in terminal と同じ形式（output / exit_code）で返す。

**リスクと対策:** 対話コマンド・巨大出力・ANSI・タイムアウトは §6。取得に失敗
したら built-in の subprocess 実行にフォールバック（確実性を優先）。

### 5.3 vscode_state / vscode_open / vscode_close_tab

- `vscode_state`: `/state` を返すだけ。Codex が「タブが N 個開いている、古いのを
  閉じよう」等を判断する材料。
- `vscode_open` / `vscode_close_tab`: `/open` `/close` を叩く。結果を短く返す。

### 5.4 hermes を隠す（#46）+ 冒頭を Desktop ブラウザに（#47）

- 運転台本 v0.4: hermes（`hermes -z '#N の対応を行う'`）は**配信に映らない隠し
  tmux セッション**で動かす。配信画面には VSCode（ブリッジ操作の結果）だけが映る。
- 冒頭「本日の予定」は OBS browser-source（kai-slide）を廃し、`stream-browser.py` で
  Desktop 上にスライド or GitHub を開いて見せる。

## 6. エラー処理・縮退

| 失敗モード                                                    | 検知             | 挙動                                                   | 復旧               |
| ------------------------------------------------------------- | ---------------- | ------------------------------------------------------ | ------------------ |
| 拡張/ブリッジ不在（配信外）                                   | HTTP 接続失敗    | 各ツールは内部処理（built-in）で実行。演出のみスキップ | 配信開始で自動復帰 |
| terminal の可視実行が取れない（マーカー未出現・タイムアウト） | ポーリング上限   | built-in subprocess 実行へフォールバックし結果は返す   | 次コマンドで再試行 |
| /state 取得失敗                                               | HTTP エラー      | `vscode_state` は「取得不可」を返す（作業は止めない）  | —                  |
| 巨大/対話出力                                                 | サイズ・時間上限 | 出力を末尾省略、対話はタイムアウトで打ち切り           | —                  |

**原則:** どのツールも「配信演出が壊れても、Codex への戻り値（実処理の結果）は
必ず保証する」。演出は best-effort、実行は確実。

## 7. 設定

| キー                                           | 置き場所    | デフォルト              | 説明                                                                       |
| ---------------------------------------------- | ----------- | ----------------------- | -------------------------------------------------------------------------- |
| `plugins.enabled: [..., kai_ide]`              | config.yaml | —                       | plugin 有効化                                                              |
| `plugins.entries.kai_ide.allow_tool_override`  | config.yaml | —                       | **true 必須**（built-in 差替の opt-in）                                    |
| `plugins.entries.kai_ide.bridge_url`           | config.yaml | `http://127.0.0.1:8920` | ブリッジ URL                                                               |
| `plugins.entries.kai_ide.terminal_pane`        | config.yaml | `kai-stream.work`       | 統合ターミナルの tmux ペイン                                               |
| `plugins.entries.kai_ide.enabled`              | config.yaml | true                    | 演出の有効化（false で内部処理のみ）                                       |
| `plugins.entries.kai_ide.typewriter_command_s` | config.yaml | `0.04`                  | terminal override の1文字タイプ演出の間隔（秒）。0 で演出オフ（Issue #96） |

## 8. セキュリティ

- **ツール override の信頼ゲート:** `allow_tool_override: true` を明示した時だけ
  built-in を差し替える（誤って `write_file` 等を奪う事故を防ぐ hermes の設計）。
- **秘匿情報:** コマンド・出力・パスは kai_trace / narrator と同じマスクを通してから
  ブリッジ・ターミナルへ渡す（トークン等は «redacted»）。
- **ネットワーク:** ブリッジは 127.0.0.1 のみ。公開ポートなし。
- **実体の正しさ:** write/patch はディスクへ実書き込みするので、verify.sh 等が
  演出の影響を受けない（演出は表示のみ）。

## 9. テスト・検証

- **ユニット（plugin）:** 各ツール handler を HTTP スタブ + tmux スタブで検証。
  - write_file override: 実書込が呼ばれる / 拡張不在でも戻り値が正 / マスク
  - terminal override: マーカー方式で出力・exit code を組み立てる / タイムアウトで
    フォールバック / マスク
  - vscode_state/open/close: ブリッジへ正しい JSON を送る / 不達で縮退
- **拡張:** `/state` `/open` `/close` を実 VSCode で叩き、タブ状態が反映されること
  （手動 + スクショ）。
- **runtime acceptance（必須）:** kai-vm 実機で、Codex に `terminal`/`write_file` を
  使わせ、統合ターミナルで実行・VSCode にタイプ表示されることを配信/録画で確認。
- `scripts/kai/verify.sh` 6/6 緑。

## 10. 実装手順（PR 分割）

1. **PR-1 ブリッジ土台 + 状態系ツール:** 拡張に `GET /state` `POST /open` `POST /close`。
   plugin `kai_ide` に `vscode_state` / `vscode_open` / `vscode_close_tab`（override なし）。
   → 最小で「kai が VSCode 状態を読む・開く・閉じる」を成立させ、override 機構を検証。
2. **PR-2 terminal override:** `/terminal` + `terminal` の override（統合ターミナル実行・
   出力捕捉・フォールバック）。#48 の核。
3. **PR-3 write_file/patch override:** ディスク書込 + `/edit` 統合。既存 kai_director の
   編集通知を plugin 側へ寄せる（重複を整理）。
4. **PR-4 運転台本 v0.4:** hermes 隠しセッション（#46）+ 冒頭 Desktop ブラウザ（#47）+
   タブ整理を kai の判断に委ねる運用。

**変更してよいファイル:** `plugins/kai_ide/*`、`kai-services/streaming/vm/vscode/kai-typewriter/*`、`docs/kai/**`、`tests/plugins/*`。hermes コア（`tools/`・`model_tools.py`・`hermes_cli/`）は変更しない（override と拡張で完結）。

## 11. 完了条件（DoD チェックリスト）

- [ ] Codex が `terminal` を呼ぶと統合ターミナルで実行され、出力が配信画面に見える
- [ ] Codex が `write_file`/`patch` を呼ぶと VSCode にタイプ表示され、ディスクも正しい
- [ ] kai が `vscode_state` を読んでタブを開く/閉じる判断ができる
- [ ] 拡張不在（配信外）でも全ツールが内部処理で正しく動く（フォールバック）
- [ ] 秘匿情報がターミナル・ブリッジ・配信に漏れない
- [ ] runtime acceptance の証跡（配信/録画・スクショ）を PR に記載
- [ ] `scripts/kai/verify.sh` 6/6 緑

## 12. 制約・禁止事項

- hermes コア改変禁止（override と拡張で完結）
- ツール override は `allow_tool_override` opt-in 必須
- 演出が壊れても実処理の戻り値を保証（Codex を誤動作させない）
- terminal の可視実行は「二重実行しない」（hermes の内部 subprocess は使わず、統合
  ターミナルでの実行を唯一の実行にする。取れない時だけ subprocess フォールバック）
- 秘匿マスクを通さずにコマンド・出力を外へ出さない

## 13. 未決事項

| #   | 事項                                                                          | 決め方 / 期限                                                                         |
| --- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 1   | terminal 可視実行の完了検知・出力捕捉の確実性（対話・巨大出力・pager）        | PR-2 で PoC。取れない範囲は subprocess フォールバックで割り切る                       |
| 2   | 既存 kai_director（コマンドログ・端末ミラー・編集通知）との整理               | PR-2/3 で kai_ide に寄せ、重複を廃止（ミラーは terminal override で不要になる見込み） |
| 3   | write の実書込は built-in をどう呼ぶか（override 内から元実装を参照できるか） | PR-3 実装時に registry の元 handler 取得可否を確認。不可なら file I/O を自前実装      |
| 4   | 実況（音声）と操作の同期                                                      | まずツール実行 = 可視操作で自然に同期。厳密な待ち合わせは効果を見て判断               |
