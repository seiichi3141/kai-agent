#!/usr/bin/env bash
# kai 配信制御 CLI（M4）。OBS の起動・配信開始/停止・クリーン終了を一括で行う。
# kai-vm 上でオーナーが手動実行する想定（MVP）。obs-websocket v5 経由（obsws.py）。
#
# 使い方:
#   broadcast.sh start         # OBS 起動 + websocket 疎通待ち（配信はまだ始めない）
#   broadcast.sh stream-start  # 配信開始（YouTube へ出る。明示コマンドに分離）
#   broadcast.sh stream-stop   # 配信停止（OBS は起動したまま）
#   broadcast.sh stop          # 配信停止（必要なら）→ OBS をクリーン終了
#   broadcast.sh status        # OBS プロセス・配信・録画の状態（人間可読サマリ）
#   broadcast.sh status --json # obs-websocket の生 JSON（調査用）
#   broadcast.sh screenshot [出力パス] # 画面の PNG スクリーンショットを保存
#   broadcast.sh record-start | record-stop   # 録画（配信に出さない検証用）
#   broadcast.sh scene <シーン名>      # OBS のシーン切替（kai-slide / シーン 等）
#   broadcast.sh agenda "項目1" ...    # 冒頭スライドの「本日の予定」を設定
#
# 運用上の注意:
#   - OBS は SIGTERM ではシーンを保存しない。終了は必ず本スクリプトの stop
#     （wmctrl のウィンドウクローズ）を使う。
#   - Tailscale SSH から使う場合も想定し DISPLAY / XDG_RUNTIME_DIR は自前で補う。
set -euo pipefail

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBSWS="${HERE}/obsws.py"
OBS_UNIT="kai-obs"
WS_WAIT_SEC="${WS_WAIT_SEC:-40}"   # OBS 起動から websocket 応答までの待ち上限
STOP_WAIT_SEC="${STOP_WAIT_SEC:-30}"

pick_obsws_python() {
  if [[ -n "${OBSWS_PYTHON:-}" ]]; then
    printf '%s\n' "${OBSWS_PYTHON}"
    return 0
  fi
  if python3 -c 'import websocket' >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi
  if [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c 'import websocket' >/dev/null 2>&1; then
    printf '%s\n' "/usr/bin/python3"
    return 0
  fi
  printf '%s\n' "python3"
}

OBSWS_PYTHON="$(pick_obsws_python)"

obs_running() { pgrep -x obs >/dev/null; }

# OBS メインウィンドウの ID（WM_CLASS が obs のもの）を返す。無ければ空。
obs_window() { wmctrl -lx 2>/dev/null | awk '$3 ~ /^obs\./ {print $1; exit}'; }

ws_ok() { "${OBSWS_PYTHON}" "${OBSWS}" GetVersion >/dev/null 2>&1; }

streaming_active() {
  "${OBSWS_PYTHON}" "${OBSWS}" GetStreamStatus 2>/dev/null \
    | python3 -c 'import json,sys; d=json.loads(sys.stdin.readline()); sys.exit(0 if d["responseData"]["outputActive"] else 1)'
}

cmd_start() {
  if obs_running; then
    echo "OBS は既に起動しています。"
  else
    systemctl --user reset-failed "${OBS_UNIT}" 2>/dev/null || true
    # --disable-shutdown-check: 前回が異常終了でもセーフモード確認ダイアログを
    # 出さない（配信画面にダイアログが映る/起動が止まるのを防ぐ）。
    systemd-run --user --unit="${OBS_UNIT}" --collect \
      --setenv=DISPLAY="${DISPLAY}" obs --disable-shutdown-check
    echo "OBS を起動しました（unit: ${OBS_UNIT}）。websocket 応答を待ちます..."
  fi
  for _ in $(seq 1 "${WS_WAIT_SEC}"); do
    if ws_ok; then
      echo "✅ obs-websocket 疎通 OK。配信を始めるには: broadcast.sh stream-start"
      cmd_status
      return 0
    fi
    sleep 1
  done
  echo "❌ ${WS_WAIT_SEC} 秒待っても obs-websocket に接続できません。" >&2
  return 1
}

cmd_stream_start() {
  "${OBSWS_PYTHON}" "${OBSWS}" StartStream
  echo "✅ 配信開始を要求しました。実際の出力状態:"
  sleep 2
  "${OBSWS_PYTHON}" "${OBSWS}" GetStreamStatus
}

cmd_stream_stop() {
  "${OBSWS_PYTHON}" "${OBSWS}" StopStream
  echo "✅ 配信停止を要求しました。"
}

cmd_stop() {
  if ! obs_running; then
    echo "OBS は起動していません。"
    return 0
  fi
  if streaming_active; then
    echo "配信中のため先に停止します..."
    "${OBSWS_PYTHON}" "${OBSWS}" StopStream
    sleep 3
  fi
  local win
  win="$(obs_window)"
  if [[ -z "${win}" ]]; then
    echo "❌ OBS のウィンドウが見つかりません（プロセスは存在）。手動確認が必要です。" >&2
    return 1
  fi
  # クリーン終了（シーン保存はウィンドウクローズ経由でのみ行われる）
  wmctrl -i -c "${win}"
  for _ in $(seq 1 "${STOP_WAIT_SEC}"); do
    if ! obs_running; then
      echo "✅ OBS をクリーン終了しました。"
      return 0
    fi
    sleep 1
  done
  echo "❌ ${STOP_WAIT_SEC} 秒待っても OBS が終了しません（終了確認ダイアログが出ている可能性）。" >&2
  return 1
}

cmd_status() {
  if obs_running; then
    echo "OBS: 起動中 (pid $(pgrep -x obs | head -1))"
  else
    echo "OBS: 停止"
    return 0
  fi

  local stream_json stream_rc record_json record_rc scene_json scene_rc
  stream_rc=0
  record_rc=0
  scene_rc=0
  stream_json="$("${OBSWS_PYTHON}" "${OBSWS}" GetStreamStatus 2>&1)" || stream_rc=$?
  record_json="$("${OBSWS_PYTHON}" "${OBSWS}" GetRecordStatus 2>&1)" || record_rc=$?
  scene_json="$("${OBSWS_PYTHON}" "${OBSWS}" GetCurrentProgramScene 2>&1)" || scene_rc=$?

  python3 - "${stream_rc}" "${stream_json}" "${record_rc}" "${record_json}" "${scene_rc}" "${scene_json}" <<'PY'
import json
import sys


def parse_payload(raw):
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line).get("responseData") or {}
    return {}


def duration(data):
    timecode = str(data.get("outputTimecode") or "").strip()
    if timecode:
        return timecode.split(".", 1)[0]
    millis = data.get("outputDuration")
    if isinstance(millis, (int, float)) and millis >= 0:
        seconds = int(millis // 1000)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return "--:--:--"


def bytes_mb(value):
    if not isinstance(value, (int, float)):
        return None
    return value / 1024 / 1024


def error_summary(raw):
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return "詳細なし"
    return lines[-1]


stream_rc = int(sys.argv[1])
stream_raw = sys.argv[2]
record_rc = int(sys.argv[3])
record_raw = sys.argv[4]
scene_rc = int(sys.argv[5])
scene_raw = sys.argv[6]

if scene_rc == 0:
    scene = parse_payload(scene_raw)
    scene_name = str(scene.get("currentProgramSceneName") or "").strip()
    if scene_name:
        print(f"シーン: {scene_name}")
    else:
        print("シーン: 状態取得失敗（シーン名が空です）")
else:
    print(f"シーン: 状態取得失敗（{error_summary(scene_raw)}）")

if stream_rc == 0:
    stream = parse_payload(stream_raw)
    if stream.get("outputActive"):
        parts = [f"配信中 {duration(stream)}"]
        mb = bytes_mb(stream.get("outputBytes"))
        if mb is not None:
            parts.append(f"出力 {mb:.1f} MB")
        skipped = stream.get("outputSkippedFrames")
        if isinstance(skipped, int):
            parts.append(f"スキップ {skipped} frames")
        reconnecting = stream.get("outputReconnecting")
        warning = " ⚠️ 再接続中" if reconnecting else ""
        print(f"配信: {' / '.join(parts)}{warning}")
    else:
        print("配信: 停止")
else:
    print(f"配信: 状態取得失敗（{error_summary(stream_raw)}）")

if record_rc == 0:
    record = parse_payload(record_raw)
    if record.get("outputActive"):
        print(f"録画: 録画中 {duration(record)}")
    else:
        print("録画: 停止")
else:
    print(f"録画: 状態取得失敗（{error_summary(record_raw)}）")
PY
}

cmd_status_json() {
  if obs_running; then
    echo "OBS: 起動中 (pid $(pgrep -x obs | head -1))"
    "${OBSWS_PYTHON}" "${OBSWS}" GetStreamStatus || true
    "${OBSWS_PYTHON}" "${OBSWS}" GetRecordStatus || true
  else
    echo "OBS: 停止"
  fi
}

cmd_screenshot() {
  local output_path output_dir
  output_path="${1:-${HOME}/kai-screenshots/$(date +%Y%m%d-%H%M%S).png}"
  output_dir="$(dirname "${output_path}")"

  mkdir -p "${output_dir}"
  scrot -o "${output_path}"
  printf '%s\n' "${output_path}"
}

cmd_scene() {
  local name="${1:-}"
  if [[ -z "${name}" ]]; then
    echo "使い方: broadcast.sh scene <シーン名>" >&2
    exit 2
  fi
  "${OBSWS_PYTHON}" "${OBSWS}" SetCurrentProgramScene "{\"sceneName\": \"${name}\"}"
}

cmd_agenda() {
  if [[ $# -eq 0 ]]; then
    echo "使い方: broadcast.sh agenda \"項目1\" [\"項目2\" ...]" >&2
    exit 2
  fi
  local agenda_file="${AGENDA_FILE:-${HOME}/.config/kai/agenda.json}"
  mkdir -p "$(dirname "${agenda_file}")"
  # 引数を JSON 配列に安全にエンコードして書く（スライドが 5 秒以内に反映する）
  python3 - "${agenda_file}" "$@" <<'PY'
import json
import sys

path = sys.argv[1]
items = sys.argv[2:]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"items": items}, f, ensure_ascii=False)
print(f"本日の予定を設定しました（{len(items)} 項目 → {path}）")
PY
}

case "${1:-}" in
  start)        cmd_start ;;
  stream-start) cmd_stream_start ;;
  stream-stop)  cmd_stream_stop ;;
  stop)         cmd_stop ;;
  status)
    case "${2:-}" in
      "")     cmd_status ;;
      --json) cmd_status_json ;;
      *)
        echo "不明な status オプションです: ${2}" >&2
        echo "使い方: broadcast.sh status [--json]" >&2
        exit 2
        ;;
    esac
    ;;
  screenshot)    cmd_screenshot "${2:-}" ;;
  record-start) "${OBSWS_PYTHON}" "${OBSWS}" StartRecord ;;
  record-stop)  "${OBSWS_PYTHON}" "${OBSWS}" StopRecord ;;
  scene)        cmd_scene "${2:-}" ;;
  agenda)       shift; cmd_agenda "$@" ;;
  *)
    sed -n '2,19p' "${BASH_SOURCE[0]}"
    exit 2
    ;;
esac
