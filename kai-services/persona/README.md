# persona

kai の人格ファイル（SOUL.md）の正典。hermes は `<HERMES_HOME>/SOUL.md` を
システムプロンプトの人格として常時読み込むため、デプロイは VM 上でコピーする:

```bash
cp ~/kai-agent/kai-services/persona/SOUL.md ~/.hermes/SOUL.md
```

変更したらこのファイルを直して PR → merge → VM で再コピー（プロセス再起動不要。
次のセッションから反映）。

背景（2026-07-05）: デフォルトの SOUL.md は英語の Hermes 人格で、kai の応答が
英語になり配信の読み上げ・字幕・PR がすべて英語化していた。日本語規定・
話し方・成果物言語を含む kai 人格に置き換えた。
