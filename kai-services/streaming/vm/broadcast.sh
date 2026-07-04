#!/usr/bin/env bash
# kai 配信制御 CLI（M4）。OBS の起動・配信開始/停止・クリーン終了を一括で行う。
# kai-vm 上でオーナーが手動実行する想定（MVP）。obs-websocket v5 経由（obsws.py）。
#
# 使い方:
#   broadcast.sh start         # OBS 起動 + websocket 疎通待ち（配信はまだ始めない）
#   broadcast.sh stream-start  # 配信開始（YouTube へ出る。明示コマンドに分離）
#   broadcast.sh stream-stop   # 配信停止（OBS は起動したまま）
#   broadcast.sh stop          # 配信停止（必要なら）→ OBS をクリーン終了
#   broadcast.sh status        # OBS プロセス・配信・録画の状態
#   broadcast.sh record-start | record-stop   # 録画（配信に出さない検証用）
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

obs_running() { pgrep -x obs >/dev/null; }

# OBS メインウィンドウの ID（WM_CLASS が obs のもの）を返す。無ければ空。
obs_window() { wmctrl -lx 2>/dev/null | awk '$3 ~ /^obs\./ {print $1; exit}'; }

ws_ok() { python3 "${OBSWS}" GetVersion >/dev/null 2>&1; }

streaming_active() {
  python3 "${OBSWS}" GetStreamStatus 2>/dev/null \
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
  python3 "${OBSWS}" StartStream
  echo "✅ 配信開始を要求しました。実際の出力状態:"
  sleep 2
  python3 "${OBSWS}" GetStreamStatus
}

cmd_stream_stop() {
  python3 "${OBSWS}" StopStream
  echo "✅ 配信停止を要求しました。"
}

cmd_stop() {
  if ! obs_running; then
    echo "OBS は起動していません。"
    return 0
  fi
  if streaming_active; then
    echo "配信中のため先に停止します..."
    python3 "${OBSWS}" StopStream
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
    python3 "${OBSWS}" GetStreamStatus || true
    python3 "${OBSWS}" GetRecordStatus || true
  else
    echo "OBS: 停止"
  fi
}

case "${1:-}" in
  start)        cmd_start ;;
  stream-start) cmd_stream_start ;;
  stream-stop)  cmd_stream_stop ;;
  stop)         cmd_stop ;;
  status)       cmd_status ;;
  record-start) python3 "${OBSWS}" StartRecord ;;
  record-stop)  python3 "${OBSWS}" StopRecord ;;
  *)
    sed -n '2,16p' "${BASH_SOURCE[0]}"
    exit 2
    ;;
esac
