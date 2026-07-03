# kai-services/streaming — M0 ランブック

Oracle ARM VM 上に kai の配信スタック（永続デスクトップ + 音声経路 + OBS + VNC）を構築する手順。設計は `docs/kai/design/streaming.md`、検証基準は同 §9。

## 0. 前提（オーナーが事前に済ませること）

| #   | 項目                                                                                                            | 備考                                                |
| --- | --------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 1   | Oracle Cloud アカウント作成（ホームリージョン: 東京） + **PAYG へアップグレード**                               | 無料枠 4 OCPU / 24GB の維持とアイドル回収回避のため |
| 2   | A1 インスタンス作成: Ubuntu 24.04 (aarch64), 4 OCPU / 24GB / ブートボリューム 100GB 以上                        | A1 容量エラー時は時間帯を変えてリトライ             |
| 3   | クラウド側 FW（Security List）: **公開 22 番も閉じる**（Tailscale 経由に統一。閉じるのは Tailscale SSH 確認後） | 公開ポートゼロが原則                                |
| 4   | Tailscale: tailnet に VM を追加（`curl -fsSL https://tailscale.com/install.sh \| sh && sudo tailscale up`）     | Mac / Windows と同じ tailnet                        |
| 5   | YouTube: チャンネルの**ライブ配信を有効化**（初回は有効化後 24 時間待ち）                                       | 最初に申請しておく                                  |

## 1. セットアップ

```bash
# VM 上で（Tailscale 経由 SSH）
git clone git@github.com:seiichi3141/kai-agent.git ~/kai-agent   # デプロイキー等は M1 で整備。M0 は https+PAT でも可
cd ~/kai-agent
bash kai-services/streaming/setup.sh
```

スクリプトは冪等。VNC パスワードは初回に対話で設定する。

## 2. 検証（設計 §9 の 1〜4）

```bash
# 注意: Tailscale SSH セッションは XDG_RUNTIME_DIR を設定しないため、
# pactl / paplay を手動実行する前に必ず export する（systemd unit 経由なら不要）。
export XDG_RUNTIME_DIR=/run/user/$(id -u)

DISPLAY=:0 xdpyinfo | grep dimensions        # → 1920x1080
pactl list short sinks | grep kai_speaker
ffmpeg -f lavfi -i "sine=frequency=440:duration=2" -y /tmp/test-tone.wav 2>/dev/null
paplay --device=kai_speaker /tmp/test-tone.wav
```

Mac の VNC クライアント（Finder → 「サーバへ接続」→ `vnc://<VMのTailscale IP>:5900`）で XFCE デスクトップが見えること。

## 3. OBS 初期設定（VNC 内で手動・初回のみ）

1. XFCE のターミナルで `obs` を起動
2. ソース: 「画面キャプチャ (XSHM)」→ Screen 0
3. 音声: 設定 → 音声 → デスクトップ音声 = `Monitor of kai speaker`
4. 配信: サービス = YouTube、**ストリームキーを設定**（キーは画面共有中・配信中に表示しない）
5. 出力: x264 / veryfast / 1080p30 / 6000kbps（性能不足なら 720p30 / 4500kbps）
6. ツール → obs-websocket 設定: 有効化、ポート 4455、**localhost のみ**（認証パスワード設定）

## 4. テスト配信（限定公開・30 分）

1. YouTube Studio で限定公開のライブ配信を作成し、OBS で「配信開始」
2. 別端末で視聴し、映像・音声（`paplay` テスト音）を確認
3. 記録する: OBS 統計のドロップフレーム率 / `top` の CPU 使用率 / 体感遅延
4. 30 分安定したら M0 の DoD（設計 §11）をチェック

## 5. トラブルシュート

| 症状                 | 対処                                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------- |
| Xorg が起動しない    | `journalctl --user -u kai-xorg -e`。`/etc/X11/Xwrapper.config` を確認                         |
| OBS（arm64）が不安定 | `fallback-ffmpeg.sh`（未作成。必要になったら設計 §10-3 に従い追加）で ffmpeg x11grab 直配信へ |
| 音が OBS に乗らない  | `pactl list short sinks` で kai_speaker を確認 → OBS のデスクトップ音声デバイスを再選択       |
| A1 の CPU が足りない | 720p30 へ。それでも不足なら GCP x86_64 へ移行（設計 §13-1）                                   |

## 実機検証後のルール

実機で加えた修正は必ずこのディレクトリのスクリプト/設定に反映してコミットする（スクリプトが常に実態を表す）。
