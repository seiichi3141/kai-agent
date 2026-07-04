#!/usr/bin/env bash
# kai の独立検証器（Loop Engineering の verifier。設計: docs/kai/loop-engineering.md FR-L2）。
#
# kai 所有コードの全検証器を実際に実行し、pass/fail を集計する。
# **これが「完了」の唯一の根拠。** エージェント（Claude / kai）は「できました」を
# 完了の根拠にせず、このスクリプトが緑（exit 0）を返したことだけを完了と呼ぶ。
#
# 原則（loop contract P1/P1b）:
#   - 全 kai 検証器を回す（パス絞り込みの誤判定で「検証したつもり」を作らない）。
#   - **検証ツールが無い場合は失敗**として扱う（exit 非ゼロ）。「検証できなかった」を
#     「検証済み（緑）」に見せないため。CI（ubuntu runner）には全ツールが揃う。
#   - どれか1つでも赤なら全体が非ゼロ終了。最後にサマリを出す。
#
# 使い方:
#   scripts/kai/verify.sh            # kai 所有コードの全検証器を実行
#   scripts/kai/verify.sh --list     # 実行する検証器を列挙するだけ（何もしない）
#
# CI（.github/workflows/kai-ci.yml）もローカルもこのスクリプトを唯一の入口にし、
# 「ローカル緑・CI 赤」の乖離を無くす（NFR-2）。
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 2
REPO_ROOT="$(pwd)"

# Python の検証ツール（ruff / ty / pytest）は .venv に入る。venv を PATH 先頭に
# 足して、run_tests.sh と同じ環境で検証する（NFR-2: ローカルと CI の一致）。
if [[ -d "${REPO_ROOT}/.venv/bin" ]]; then
  PATH="${REPO_ROOT}/.venv/bin:${PATH}"
  export PATH
fi

# ── 集計 ────────────────────────────────────────────────────────────────
FAILURES=()
PASSES=()

# run_check <表示名> <コマンド...>: コマンドを実行し、結果を集計する。
run_check() {
  local name="$1"
  shift
  echo ""
  echo "──────────────────────────────────────────────────────────"
  echo "▶ ${name}"
  echo "──────────────────────────────────────────────────────────"
  if "$@"; then
    echo "✅ PASS: ${name}"
    PASSES+=("${name}")
  else
    echo "❌ FAIL: ${name} (exit $?)"
    FAILURES+=("${name}")
  fi
}

# require_tool <コマンド名> <インストール手順>: 検証ツールの存在を強制する。
# 無ければ「検証不能＝失敗」として扱い、非ゼロで即分かるようにする。
require_tool() {
  local tool="$1" hint="$2"
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "❌ 検証ツール '${tool}' が見つかりません。${hint}"
    echo "   （'検証できない' を '検証済み' に見せないため、これは失敗として扱います）"
    return 1
  fi
  return 0
}

# ── 検証器の定義 ─────────────────────────────────────────────────────────

check_python_tests() {
  # kai 所有の Python テスト（plugins/kai_*）。正典ランナー経由（pytest 直叩き禁止）。
  local files=(tests/plugins/test_kai_*.py)
  if [[ ! -e "${files[0]}" ]]; then
    echo "（kai plugin テストが見つかりません: tests/plugins/test_kai_*.py）"
    return 1
  fi
  bash scripts/run_tests.sh "${files[@]}"
}

check_python_lint() {
  require_tool ruff "pip install ruff / uv pip install -e '.[dev]'" || return 1
  ruff check plugins/kai_narrator plugins/kai_trace tests/plugins/test_kai_*.py
}

check_python_types() {
  require_tool ty "uv pip install -e '.[dev]'" || return 1
  ty check plugins/kai_narrator plugins/kai_trace
}

check_node_tests() {
  require_tool node "https://nodejs.org / brew install node" || return 1
  local rc=0 dir
  # kai-services 配下で *.test.mjs を持つディレクトリを検証（node_modules は除外）。
  while IFS= read -r dir; do
    echo "→ node --test in ${dir}"
    ( cd "${dir}" && node --test ) || rc=1
  done < <(find kai-services -type f -name '*.test.mjs' -not -path '*/node_modules/*' \
             -exec dirname {} \; | sort -u)
  return "${rc}"
}

check_docs_lint() {
  bash scripts/kai-docs-lint.sh
}

check_shellcheck() {
  require_tool shellcheck "brew install shellcheck / apt-get install shellcheck" || return 1
  local scripts=()
  while IFS= read -r f; do scripts+=("${f}"); done < <(kai_shell_scripts)
  if [[ ${#scripts[@]} -eq 0 ]]; then
    echo "（kai 所有のシェルスクリプトが見つかりません）"
    return 1
  fi
  printf '  %s\n' "${scripts[@]}"
  # warning 以上をゲート対象にする（info/style の助言は落とさない。SC2329 の
  # 間接呼び出し誤検知などを避けつつ、実バグ＝warning/error は必ず捕まえる）。
  shellcheck --severity=warning "${scripts[@]}"
}

# kai_shell_scripts: kai 所有のシェルスクリプトを列挙する（node_modules / vendor 除外）。
kai_shell_scripts() {
  find scripts/kai kai-services -type f -name '*.sh' \
    -not -path '*/node_modules/*' -not -path '*/vendor/*' 2>/dev/null | sort
  # scripts 直下の kai 所有スクリプトも対象（kai-docs-lint.sh）。
  find scripts -maxdepth 1 -type f -name 'kai-*.sh' 2>/dev/null | sort
}

# ── PR モード（FR-L2 後半 / L3）─────────────────────────────────────────────
# 「完了」を PR の機械状態（CI 緑かつ mergeable）まで引き上げる。エージェントの
# 自己申告ではなく gh で PR の実状態を確認する（nokonora の run.sh 相当）。
PR_TIMEOUT_MIN="${PR_TIMEOUT_MIN:-15}"  # CI 完了待ちの上限（分）
PR_POLL_SEC="${PR_POLL_SEC:-20}"        # ポーリング間隔（秒）

# kai_repo: origin の GitHub リポジトリ（owner/repo）を導出する。gh は remote 曖昧性で
# upstream（NousResearch/hermes-agent）を誤選択するため、常に --repo で明示する。
kai_repo() {
  local url
  url="$(git config --get remote.origin.url 2>/dev/null)"
  # git@github.com:owner/repo.git / https://github.com/owner/repo(.git) の両形式に対応
  printf '%s' "${url}" | sed -E 's#^.*github\.com[:/]##; s#\.git$##'
}

run_pr_mode() {
  require_tool gh "https://cli.github.com/ （認証: gh auth login）" || return 1
  local repo pr="$1"
  repo="$(kai_repo)"
  if [[ -z "${repo}" ]]; then
    echo "❌ origin の GitHub リポジトリを特定できません"
    return 1
  fi
  if [[ -z "${pr}" ]]; then
    pr="$(gh pr view --repo "${repo}" --json number -q .number 2>/dev/null || true)"
  fi
  if [[ -z "${pr}" ]]; then
    echo "❌ PR を特定できません。現在のブランチに PR が無いなら番号を渡してください: verify.sh --pr <N>"
    return 1
  fi
  echo "▶ PR #${pr}（${repo}）の実状態を検証します（自己申告に頼らず gh で確認）"

  # 1) CI が完了するまで待つ（PENDING の間はポーリング。偽陰性=「まだ緑じゃない」防止）。
  local deadline=$((SECONDS + PR_TIMEOUT_MIN * 60)) total pending
  while :; do
    total="$(gh pr checks "${pr}" --repo "${repo}" --json bucket -q 'length' 2>/dev/null || echo -1)"
    if [[ "${total}" -le 0 ]]; then
      echo "  （チェック未登録。kai CI の起動待ち...）"
    else
      pending="$(gh pr checks "${pr}" --repo "${repo}" --json bucket \
        -q '[.[]|select(.bucket=="pending")]|length' 2>/dev/null || echo 0)"
      echo "  checks=${total} pending=${pending}"
      [[ "${pending}" -eq 0 ]] && break
    fi
    if [[ "${SECONDS}" -ge "${deadline}" ]]; then
      echo "❌ CI が ${PR_TIMEOUT_MIN} 分以内に完了しませんでした（未確認＝失敗として扱う）"
      return 1
    fi
    sleep "${PR_POLL_SEC}"
  done

  # 2) 失敗した checks が無いこと（fail / cancel を赤とみなす。pass / skipping は許容）。
  if gh pr checks "${pr}" --repo "${repo}" --json name,bucket \
      -q '.[]|select(.bucket=="fail" or .bucket=="cancel")|"  ❌ \(.name): \(.bucket)"' 2>/dev/null \
      | grep -q .; then
    echo "❌ 失敗した CI チェックがあります:"
    gh pr checks "${pr}" --repo "${repo}" --json name,bucket \
      -q '.[]|select(.bucket=="fail" or .bucket=="cancel")|"  ❌ \(.name): \(.bucket)"' 2>/dev/null
    return 1
  fi
  echo "  ✅ CI 緑（fail/cancel なし）"

  # 3) mergeable であること（コンフリクト / BEHIND を排除）。
  local mergeable
  mergeable="$(gh pr view "${pr}" --repo "${repo}" --json mergeable -q .mergeable 2>/dev/null)"
  echo "  mergeable=${mergeable}"
  if [[ "${mergeable}" != "MERGEABLE" ]]; then
    echo "❌ PR が MERGEABLE ではありません（コンフリクト等）。main 追従・解消が必要です。"
    return 1
  fi

  echo ""
  echo "✅ PR #${pr} は CI 緑かつ MERGEABLE。merge-ready です（マージは人間 or 承認済み手順で）。"
  return 0
}

# ── --pr モード ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "--pr" ]]; then
  run_pr_mode "${2:-}"
  exit $?
fi

# ── --list モード ────────────────────────────────────────────────────────
if [[ "${1:-}" == "--list" ]]; then
  echo "kai verify.sh が実行する検証器:"
  echo "  1. python-tests   : scripts/run_tests.sh tests/plugins/test_kai_*.py"
  echo "  2. python-lint    : ruff check (plugins/kai_*, tests/plugins/test_kai_*.py)"
  echo "  3. python-types   : ty check (plugins/kai_*)"
  echo "  4. node-tests     : node --test (kai-services の *.test.mjs 保有ディレクトリ)"
  echo "  5. docs-lint      : scripts/kai-docs-lint.sh"
  echo "  6. shellcheck     : kai 所有シェルスクリプト"
  echo ""
  echo "対象シェルスクリプト:"
  kai_shell_scripts | sed 's/^/  /'
  exit 0
fi

# ── 実行 ────────────────────────────────────────────────────────────────
echo "kai verify.sh — 検証器を実行します（REPO_ROOT=${REPO_ROOT}）"

run_check "python-tests" check_python_tests
run_check "python-lint"  check_python_lint
run_check "python-types" check_python_types
run_check "node-tests"   check_node_tests
run_check "docs-lint"    check_docs_lint
run_check "shellcheck"   check_shellcheck

# ── サマリ ──────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════"
echo "kai verify.sh サマリ: ${#PASSES[@]} passed, ${#FAILURES[@]} failed"
echo "══════════════════════════════════════════════════════════"
if [[ ${#FAILURES[@]} -gt 0 ]]; then
  printf '  ❌ %s\n' "${FAILURES[@]}"
  echo ""
  echo "→ 検証は赤です。この状態を「完了」と呼んではいけません（loop contract P1）。"
  exit 1
fi
echo "  すべての検証器が緑です。"
exit 0
