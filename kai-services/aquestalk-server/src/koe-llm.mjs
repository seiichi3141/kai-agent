/**
 * LLM による koe（AquesTalk10 音声記号列）生成 — 主経路。
 *
 * 設計: docs/kai/design/tts-reading-rules.md §5.2。
 * 旧 kai プロジェクト packages/tts/src/provider/text-to-speech-provider.ts の
 * プロンプト（v3/v4）と sanitizeLlmKoe を統合・適応した移植。
 *
 * 方針（同設計書 §4.3）: 英単語・固有名詞の読みと `/` 区切りは LLM の汎用変換に
 * 任せる。例外辞書はテキスト中に該当語があるときだけプロンプトに注入する。
 */

/** タグ（<NUMK VAL=...> / <ALPHA VAL=...>）にマッチする正規表現ソース */
export const TAG_REGEX_SOURCE = "<(?:NUMK|ALPHA) VAL=[^>]*>";

/**
 * タグ以外の部分にだけ変換関数を適用する。
 */
export function mapNonTagParts(koe, fn) {
  const TAG_PATTERN = new RegExp(TAG_REGEX_SOURCE, "g");
  let result = "";
  let lastIndex = 0;
  let match;
  while ((match = TAG_PATTERN.exec(koe)) !== null) {
    result += fn(koe.slice(lastIndex, match.index)) + match[0];
    lastIndex = match.index + match[0].length;
  }
  result += fn(koe.slice(lastIndex));
  return result;
}

// ---------------------------------------------------------------------------
// プロンプト
// ---------------------------------------------------------------------------

/**
 * システムプロンプト v1（旧プロジェクト v3 の短縮構成に v4 の頻出エラー対策を統合）。
 * {{TERMS_SECTION}} は buildSystemPrompt() が実行時に置換する。
 */
const PROMPT_V1 = `あなたは日本語テキストを AquesTalk10 の音声記号列に変換する変換器です。音声記号列のみを出力してください。説明・コードブロック・引用符・前後の空白や改行は一切不要です。

## 最重要ルール（違反すると合成に失敗する）
- 出力に使えるのは: 全角ひらがな、長音「ー」、句切記号「/ 。 ？ 、 ;」、数値タグ <NUMK VAL=数値> のみ
- 漢字・カタカナ・英字・半角数字を残さない。英単語は必ずひらがな読みに変換する
- <ALPHA VAL=...> タグは生成禁止（英字のスペルアウト読みは聞き取れない）
- 括弧・鉤括弧・コロン・ハイフン・感嘆符は使わない（！は 。 にする）
- 「づ」は「ず」、「ぢ」は「じ」、「ゔ」は「ば/び/ぶ/べ/ぼ」で代替
- 長音「ー」を文頭・促音「っ」の直後・句切記号の直後に置かない

## 読みのルール
- 助詞は発音どおり: は→わ、へ→え、を→お
- 数値は <NUMK VAL=数値> タグにする（例: 42個 → <NUMK VAL=42>こ）
- 英単語・ファイル名・関数名は単語として読む。1文字ずつ読まない
  （例: index.ts → いんでっくす/てぃーえす、settings.json → せっていんぐす/じぇいそん、checkoutMain → ちぇっくあうと/めいん）
- kai は配信者の名前で「かい」と読む
{{TERMS_SECTION}}
## 聞きやすさ
- 「/」で単語を区切る（2〜5モーラ目安。ポーズは入らない）
- 文末は「。」（疑問文は「？」）。息継ぎしたい位置に「、」を入れる
- 三点リーダ「...」は削除、「・」は「。」にする

## 変換例
入力: テスト全通過、lintも問題なし。では git diff で自己レビューするよ
出力: てすと/ぜんつうか、りんとも/もんだいなし。でわ/ぎっと/でぃふで/じこ/れびゅーするよ。
入力: PR #651 のコンフリクト解消を開始します
出力: ぴーあーる<NUMK VAL=651>の/こんふりくと/かいしょうお/かいし/します。
入力: useSSE.ts の query 呼び出しを修正する
出力: ゆーず/えすえすいー/てぃーえすの/くえりー/よびだしお/しゅうせい/する。`;

/**
 * システムプロンプト v2（アンカー方式）: 機械変換（kuromoji）の下書きを「参考よみ」
 * として渡し、LLM の仕事を「英単語の読み・/ 区切り・読点」に限定する。
 * 漢字の読みは kuromoji のほうが正確という実機評価（2026-07-05、v1 で
 * 追加→てんか・字幕→じまじ 等の誤読を観測）に基づく役割分担。
 */
const PROMPT_V2 = `あなたは日本語テキストを AquesTalk10 の音声記号列に整える変換器です。音声記号列のみを出力してください。説明・コードブロック・引用符・前後の空白や改行は一切不要です。

入力には「テキスト」（元の文）と「参考よみ」（機械変換の下書き）が与えられます。
参考よみを土台に、次の 3 点だけを直して完成させてください:

1. <ALPHA VAL=英字> タグを、その英単語のひらがな読みに置き換える（1文字ずつ読まない）
   例: <ALPHA VAL=BROADCAST> → ぶろーどきゃすと、<ALPHA VAL=STATUS> → すてーたす、
       <ALPHA VAL=SH> → えすえいち、<ALPHA VAL=GIT> → ぎっと
2. 「/」で単語を区切る（2〜5モーラ目安。ポーズは入らない）。息継ぎしたい位置に「、」を入れる
3. 明らかな読みの誤りだけ直す。それ以外の**ひらがなは参考よみのまま変えない**

## 出力の制約（違反すると合成に失敗する）
- 使えるのは: 全角ひらがな、長音「ー」、句切記号「/ 。 ？ 、 ;」、<NUMK VAL=数値> タグのみ
- <NUMK VAL=数値> タグはそのまま残す
- 漢字・カタカナ・英字・記号・<ALPHA> タグを残さない
- 助詞は発音どおり: は→わ、へ→え、を→お
- kai は配信者の名前で「かい」と読む
{{TERMS_SECTION}}
## 変換例
テキスト: では broadcast.sh の status を修正します
参考よみ: でわ<ALPHA VAL=BROADCAST>、<ALPHA VAL=SH>のすてーたすおしゅうせいします。
出力: でわ/ぶろーどきゃすと/どっと/えすえいちの/すてーたすお/しゅうせい/します。`;

const PROMPTS = { v1: PROMPT_V1, v2: PROMPT_V2 };

/**
 * テキストに含まれる例外辞書語だけを列挙するプロンプト節を作る。
 * 該当がなければ空文字（節ごと消える）。
 *
 * @param {string} text 変換対象テキスト
 * @param {Record<string, string>} terms 例外辞書（用語 → 読み）
 */
export function buildTermsSection(text, terms) {
  const hits = Object.entries(terms).filter(([term]) =>
    text.toLowerCase().includes(term.toLowerCase()),
  );
  if (hits.length === 0) return "";
  const lines = hits.map(([term, reading]) => `- ${term} → ${reading}`);
  return `\n## この発話に含まれる語の読み（必ずこの読みを使う）\n${lines.join("\n")}\n`;
}

/**
 * システムプロンプトを構築する。
 */
export function buildSystemPrompt({ version = "v1", text = "", terms = {} } = {}) {
  const base = PROMPTS[version] ?? PROMPT_V1;
  return base.replace("{{TERMS_SECTION}}", buildTermsSection(text, terms));
}

// ---------------------------------------------------------------------------
// LLM 呼び出し
// ---------------------------------------------------------------------------

/**
 * OpenAI 互換 API（llama.cpp / sei-win）で koe を生成する。
 * タイムアウト・HTTP エラーは例外を投げる（呼び出し側でフォールバック）。
 *
 * @param {string} text 変換対象テキスト（1 文）
 * @param {object} config
 * @param {string} config.baseUrl 例: http://100.98.225.44:8080/v1
 * @param {string} config.model
 * @param {number} config.timeoutMs
 * @param {string} [config.promptVersion]
 * @param {Record<string, string>} [config.terms] 例外辞書
 * @param {string} [config.referenceKana] v2 用: ルールベース変換の下書き（参考よみ）
 * @param {typeof fetch} [config.fetchImpl] テスト用の fetch 差し替え
 * @returns {Promise<string>} LLM の生出力（sanitize 前）
 */
export async function generateKoeLlm(text, config) {
  const {
    baseUrl,
    model,
    timeoutMs,
    promptVersion = "v2",
    terms = {},
    referenceKana = "",
    fetchImpl = fetch,
  } = config;

  const userContent =
    promptVersion === "v2" && referenceKana
      ? `テキスト: ${text}\n参考よみ: ${referenceKana}`
      : text;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        temperature: 0,
        max_tokens: 500,
        messages: [
          { role: "system", content: buildSystemPrompt({ version: promptVersion, text, terms }) },
          { role: "user", content: userContent },
        ],
        // Qwen 系の思考モードを無効化（有効だと max_tokens を食い潰す — 実機知見）
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new Error(`LLM HTTP ${res.status}`);
    }
    const data = await res.json();
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== "string" || content.length === 0) {
      throw new Error("LLM 応答が空");
    }
    return content;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// sanitize（LLM 出力の機械的後処理）
// ---------------------------------------------------------------------------

/**
 * LLM が出力した koe を機械的にクリーンアップする。純粋関数。
 * 旧プロジェクトの sanitizeLlmKoe の移植。変更点（設計書 §4.1 の正典化）:
 * 「;」は AquesTalk10 の有効な区切りなので**保持**（旧実装は除去していた）。
 * 「/」は単語区切りの要なので絶対に除去しない（旧プロジェクトで回帰事故あり）。
 */
export function sanitizeLlmKoe(koe) {
  if (!koe) return "";
  let s = koe.trim();

  // ALPHA タグの値を正規化（大文字化・英数字以外除去・空になったらタグごと削除）
  s = s.replace(/<ALPHA VAL=([^>]*)>/g, (_, v) => {
    const clean = v.toUpperCase().replace(/[^A-Z0-9]/g, "");
    return clean ? `<ALPHA VAL=${clean}>` : "";
  });

  // 閉じられていない不完全タグを除去
  s = s.replace(/<[^>]*$/, "");

  // 括弧・鉤括弧を内容ごと除去（対応が壊れた記号単体も除去）
  s = s.replace(/[（(][^（()）)]*[）)]/g, "").replace(/[（）()]/g, "");
  s = s
    .replace(/「[^「」]*」/g, "")
    .replace(/『[^『』]*』/g, "")
    .replace(/[「」『』]/g, "");

  // タグ以外の部分の記号・かなを正規化
  s = mapNonTagParts(s, (part) => {
    let p = part;
    p = p.replace(/[！!]/g, "。");
    p = p.replace(/[:：]/g, "");
    p = p.replace(/[-‐‑–—−]/g, "");
    p = p.replace(/[_`{}*+]/g, ""); // ; と / は保持（§4.1 正典）
    // 促音の後処理
    p = p.replace(/っー/g, "っ").replace(/っっ+/g, "っ").replace(/っ([。？、])/g, "$1");
    // 長音の後処理（連続・文頭・句切記号直後は不可）
    p = p.replace(/ー{2,}/g, "ー");
    p = p.replace(/^ー+/, "").replace(/([。？、,;/+])ー+/g, "$1");
    // 空白除去
    p = p.replace(/[\s　]+/g, "");
    // 助詞の発音（LLM が指示を取りこぼしても機械的に確定させる）
    p = p.replace(/を/g, "お");
    // 禁止かなの代替
    p = p.replace(/ぢ/g, "じ").replace(/づ/g, "ず");
    p = p
      .replace(/ゔぁ/g, "ば")
      .replace(/ゔぃ/g, "び")
      .replace(/ゔぇ/g, "べ")
      .replace(/ゔぉ/g, "ぼ")
      .replace(/ゔゃ/g, "びゃ")
      .replace(/ゔゅ/g, "びゅ")
      .replace(/ゔょ/g, "びょ")
      .replace(/ゔ/g, "ぶ");
    p = p.replace(/ぐぃ/g, "ぎ").replace(/ぐぅ/g, "ぐ").replace(/ぐぇ/g, "げ").replace(/ぐぉ/g, "ご");
    // AquesTalk10 が解釈できない Unicode 記号（✓ ★ ♪ 等）を除去
    p = p.replace(/[^ -~　-鿿＀-￯]/gu, "");
    return p;
  });

  // 文頭の句切記号（LLM が出力しがちな先頭の / 等）を除去
  s = s.replace(/^[/;、。]+/, "");

  return s;
}

/**
 * 文末を句切記号で終える（AquesTalk10 の書式規定）。
 */
export function ensureSentenceEnd(koe) {
  const s = koe.trim();
  if (!s) return "";
  if (s.endsWith("。") || s.endsWith("？")) return s;
  return `${s}。`;
}
