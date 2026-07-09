# VM デプロイ手順（kai-vm への安全な反映）

`main` の変更を kai-vm（`ssh kai@<Tailscale IP>`）へ反映する手順。**`git pull` だけでは
壊れる/反映されないケース**があるため、常駐サービスの再起動とサービスごとの
破壊的変更に注意する。証跡（loop contract P5）として各段階の health 出力を残す。

原則:

- **`git pull` は常駐サービスの unit を更新しない。** unit を変えた PR は
  `install.sh` の再実行（または手動 `daemon-reload` + `restart`）が要る。
- **hermes 本体（`plugins/kai_*`）は `-z` 一発起動で常駐していない。** plugin の
  変更（narrator 等）は pull すれば次回の hermes 実行時に自動反映される。restart 不要。
- 各段階で health を確認し、赤なら次に進まない。TTS が絡む変更は「health だけでなく
  実際に音が出る/koe が返る」まで確認する（`streaming-preflight.md` と同じ思想）。

---

## 0. 事前確認

```bash
ssh kai@<Tailscale IP>
cd ~/kai-agent
git fetch origin && git log --oneline HEAD..origin/main   # これから入る差分
systemctl --user list-units --type=service | grep -iE 'koe|speechd|trace'
```

## 1. pull（コードだけ更新。常駐は後で個別に反映）

```bash
cd ~/kai-agent && git switch main && git pull origin main
git rev-parse HEAD   # 反映後の HEAD を証跡に残す
```

## 2. speechd（低リスク・restart で反映）

マスク強化（#81）・`/say` の Origin 検査（#83）はコード変更のみ。bind・TTS_URL は
変わらないので unit はそのまま、restart だけでよい。

```bash
systemctl --user restart speechd.service
curl -fsS http://127.0.0.1:8900/health          # {"ok": true}
# 実発話まで確認（producer は Origin を送らないので従来どおり通る）
curl -fsS -X POST http://127.0.0.1:8900/say \
  -H 'Content-Type: application/json' -d '{"text":"デプロイ後のテストだよ"}'
```

## 3. hermes 本体（pull のみ。次回 `-z` 起動で反映）

narrator（#70/#72/#73/#75/#78/#79）・kai_trace（#81）は plugin。restart 不要。
次回の作業/ドライランで新コードが読まれる。**ここでは何もしない**（4 の koe-bridge を
先に直さないと、ドライランの発話で koe 生成が不通になる）。

## 4. koe-bridge（★破壊的・順序厳守）

PR #82 でコードの既定 bind が `0.0.0.0`→`127.0.0.1` に、認証が追加された。新コードは
**「非 loopback bind かつトークン未設定」だと fail-closed で起動拒否**する。VM は
Mac から Tailscale 越しに koe-bridge を叩くため bind は `0.0.0.0` を維持する必要があり、
**そのままではトークン無しで起動できなくなる**。順序を守って移行する。

```bash
# 4-1. 共有トークンを作る（VM 側）
mkdir -p ~/.config/kai
python3 -c "import secrets; print('KOE_BRIDGE_TOKEN=' + secrets.token_urlsafe(32))" \
  > ~/.config/kai/koe-bridge.env
chmod 600 ~/.config/kai/koe-bridge.env
TOKEN=$(cut -d= -f2 ~/.config/kai/koe-bridge.env)

# 4-2. unit を更新（BIND=0.0.0.0 を維持しつつ EnvironmentFile でトークン供給）
#      install.sh は BIND 行を落とすので、ここでは手動で両立させる
UNIT=~/.config/systemd/user/koe-bridge.service
grep -q 'EnvironmentFile=.*koe-bridge.env' "$UNIT" || \
  sed -i '/Environment=KOE_BRIDGE_PORT=8930/a EnvironmentFile=%h/.config/kai/koe-bridge.env' "$UNIT"
# bind は 0.0.0.0 のまま（Mac から届く必要がある）。トークンがあるので起動する
systemctl --user daemon-reload

# 4-3. Mac 側にトークンを渡す（別ターミナル・Mac で）
#   kai-services/aquestalk-server/.env に次を追記（TOKEN は 4-1 の値）:
#     KOE_LLM_API_KEY=<TOKEN>
#   その後 launchctl kickstart -k gui/$(id -u)/com.kai.aquestalk-server

# 4-4. koe-bridge を restart して health と認証を確認（VM 側）
systemctl --user restart koe-bridge.service
sleep 1
systemctl --user is-active koe-bridge.service         # active（fail-closed で死んでいないこと）
curl -fsS http://127.0.0.1:8930/health                # {"ok": true, ...}
# 認証: トークン無しは 401、有りは 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8930/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"x"}]}'   # 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8930/v1/chat/completions \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"短く挨拶して"}]}' # 200
```

**ロールバック**: koe-bridge が起動しない/Mac から届かない場合、`~/.config/kai/koe-bridge.env`
を消し unit の `EnvironmentFile` 行を削除して `daemon-reload` + `restart` すると
旧挙動（`KOE_BRIDGE_BIND=0.0.0.0`・認証なし）に戻る。TTS の読み仮名がルールベースに
縮退するだけで配信は継続できる（koe-bridge は主経路だがフォールバックあり）。

## 5. trace（今回は対象外）

VM で動いているのは旧 `trace-viewer.service`（Python）。#84 の認証・bind 変更は新
`trace-web`（Next.js）向けで、まだ VM に配備していない。**今回は触らない**。
trace-web へ移行する際は `kai-services/trace-web/README.md` の認証手順に従う。

## 6. 実機ドライラン（配信なし・narrator/security の実挙動を確認）

```bash
# 配信を出さずに軽い作業を1つ通し、trace とスクショを残す（証跡 P5）
cd ~/kai-agent
.venv/bin/hermes -z "テスト用の軽い作業を1つして。PR は作らず verify まで"
```

確認ポイント:

- 冒頭に kickoff（今日やることの説明）が出るか（#72 FR8）
- 実況が単調でないか・作話（接地外）が無いか（#73/#75。本番は gpt-5.4-mini）
- 発話・字幕・trace に秘密が漏れていないか（#71/#81）
- koe 生成が効いて読み上げが自然か（4 の移行が成功しているか）

トレースは `~/.hermes/kai_trace/YYYY-MM-DD.jsonl`、スクショは `scrot`。所見を
`docs/kai/stream-review/` に残し、必要なら follow-up Issue を切る。
