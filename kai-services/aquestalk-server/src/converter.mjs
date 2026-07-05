/**
 * speechText（漢字かな交じり）を AquesTalk10 音声記号列に変換するモジュール。
 *
 * 変換パイプライン:
 *   speechText → 前処理（例外ワード・記号タグ化） → kuromoji トークナイズ → カナ変換 → 音声記号列整形
 *
 * kai の packages/tts/src/converter.ts を素の JS/ESM に移植したもの。
 * ロガーは @kai/logger ではなく console ベースに置き換えている。
 */

import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";

import { generateKoeLlm, sanitizeLlmKoe, ensureSentenceEnd, mapNonTagParts } from "./koe-llm.mjs";
import { validateKoe } from "./koe-validate.mjs";

const _require = createRequire(import.meta.url);

function logWarn(message, meta) {
  console.warn(`[tts-converter] ${message}`, meta ?? "");
}

// ---------------------------------------------------------------------------
// 例外辞書（technical-terms.json）
//
// 運用方針（docs/kai/design/tts-reading-rules.md §4.4）: 事前に語彙を網羅する
// 読み辞書ではなく、汎用機構（品詞規則 + LLM）でもまだ読み間違いを観測した語だけを
// 登録する「観測ベースの例外辞書」。エントリの一括拡充はしない。
// ---------------------------------------------------------------------------

const TERMS_JSON_PATH = join(dirname(fileURLToPath(import.meta.url)), "technical-terms.json");

/** 例外辞書: { 用語: 読み } */
const TECHNICAL_TERMS = JSON.parse(readFileSync(TERMS_JSON_PATH, "utf-8"));

/**
 * 正規表現メタキャラクター（C++・node.js 等）をエスケープする。
 */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * 用語に適した境界パターンを返す。
 * - 英数字で始まる/終わる場合は \b（単語境界）を使う
 * - 記号で始まる/終わる場合（例: C++, .env）は (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) を使う
 */
function termBoundaryPattern(term) {
  const escaped = escapeRegExp(term);
  const prefix = /^\w/.test(term) ? "\\b" : "(?<![A-Za-z0-9])";
  const suffix = /\w$/.test(term) ? "\\b" : "(?![A-Za-z0-9])";
  return `${prefix}${escaped}${suffix}`;
}

/** buildExceptionDict() のキャッシュ。例外辞書はプロセス起動後は不変なので一度だけ構築する */
let exceptionDictCache = null;

/**
 * 大文字小文字非区別にしてよい用語か。英字 3 文字以上のみ非区別
 * （kai / Kai / KAI をすべて拾う）。2 文字以下の頭字語（AI・CI・PR 等）は
 * 一般語への誤マッチ（例: AI が air に部分適用）を防ぐため完全一致のみ。
 */
function isCaseInsensitiveTerm(term) {
  return (term.match(/[A-Za-z]/g) ?? []).length >= 3;
}

/**
 * EXCEPTION_DICT 互換の [RegExp, string][] を生成する。
 * 長い用語を先に並べることで、前方一致の誤変換を防ぐ（例: Issues > Issue）。
 * 境界条件で英単語の一部分へのマッチと記号付き用語の取りこぼしを両立する。
 * 結果はキャッシュされる。
 */
export function buildExceptionDict() {
  if (exceptionDictCache !== null) return exceptionDictCache;
  exceptionDictCache = Object.entries(TECHNICAL_TERMS)
    .sort(([a], [b]) => b.length - a.length)
    .map(([term, reading]) => [
      new RegExp(termBoundaryPattern(term), isCaseInsensitiveTerm(term) ? "gi" : "g"),
      reading,
    ]);
  return exceptionDictCache;
}

// ---------------------------------------------------------------------------
// kuromoji 初期化
// ---------------------------------------------------------------------------

/** kuromoji 辞書ディレクトリのパス */
function getKuromojiDictPath() {
  const kuromojiMain = _require.resolve("kuromoji");
  return resolve(dirname(kuromojiMain), "..", "dict");
}

/** グローバルトークナイザ（初期化後は再利用） */
let tokenizerPromise = null;

/**
 * kuromoji トークナイザを初期化する。
 * プロセス起動時に一度だけ呼び出すことを推奨（辞書読み込みに数秒かかるため）。
 * 初期化失敗時は null を返し、TTS はフォールバックモードで継続する。
 *
 * @returns {Promise<import("kuromoji").Tokenizer | null>}
 */
export function initTokenizer() {
  if (tokenizerPromise !== null) return tokenizerPromise;

  tokenizerPromise = new Promise((resolvePromise) => {
    try {
      const dictPath = getKuromojiDictPath();
      const kuromojiLib = _require("kuromoji");
      kuromojiLib.builder({ dicPath: dictPath }).build((err, tokenizer) => {
        if (err) {
          logWarn("kuromoji 初期化失敗", String(err));
          resolvePromise(null);
          return;
        }
        resolvePromise(tokenizer);
      });
    } catch (err) {
      logWarn("kuromoji ロード失敗", String(err));
      resolvePromise(null);
    }
  });

  return tokenizerPromise;
}

// モジュール読み込み時に初期化を開始（プロセス起動時のウォームアップ）
initTokenizer();

/** 絵文字（主要 Unicode 範囲） */
const EMOJI_REGEX = /[\u{1F300}-\u{1F9FF}\u{2600}-\u{27BF}\u{FE00}-\u{FE0F}\u{20E3}]/gu;

/** 制御文字 */
const CONTROL_CHARS_REGEX = /[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F]/g;

/** 三点リーダー・省略記号 */
const ELLIPSIS_REGEX = /[…]+/g;

// ---------------------------------------------------------------------------
// 前処理
// ---------------------------------------------------------------------------

/**
 * 前処理: 例外ワード置換・記号タグ化・絵文字除去。
 * 純粋関数（副作用なし）。
 *
 * @param {string} text 変換前のテキスト（漢字かな交じり）
 * @returns {string} タグ化済みテキスト（日本語部分はそのまま残る）
 */
export function preprocessSymbols(text) {
  if (!text) return "";

  let result = text;

  // 1. 例外ワード辞書（技術用語を日本語読みに置換）
  for (const [pattern, reading] of buildExceptionDict()) {
    result = result.replace(pattern, reading);
  }

  // 2. 絵文字・制御文字を除去
  result = result.replace(EMOJI_REGEX, "");
  result = result.replace(CONTROL_CHARS_REGEX, "");

  // 3. 三点リーダーを除去
  result = result.replace(ELLIPSIS_REGEX, "");

  // 4. 数字・英字を一括タグ化（単一パスで処理。既にタグ化した内容は再処理しない）
  result = tagifyNumbersAndAlphas(result);

  return result;
}

/**
 * 数字列・英字列をAquesTalk タグに変換する。
 * 単一パスで左から右へ処理し、以下の順でパターンにマッチする。
 *
 * マッチ優先順序（正規表現内の順序と対応）:
 *   1. 負の小数: -3.14 → まいなす<NUMK VAL=3>てん<NUMK VAL=14>
 *      ※数字直後のハイフンは負号として解釈しない（例: 1-3.14 はハイフン保持）
 *   2. 正の小数: 3.14 → <NUMK VAL=3>てん<NUMK VAL=14>
 *   3. 負の整数: -5 → まいなす<NUMK VAL=5>
 *      ※数字直後のハイフンは負号として解釈しない（例: 1-5 はハイフン保持）
 *   4. #数字: #42 → <NUMK VAL=42>
 *   5. 整数: 1234 → <NUMK VAL=1234>
 *   6. 英字列: PR → <ALPHA VAL=PR>
 *
 * 単一パス処理のため、タグ生成後の英字（NUMK, VAL 等）が再変換される問題を防ぐ。
 */
function tagifyNumbersAndAlphas(text) {
  return text.replace(
    /(?<!\d)-(\d+)\.(\d+)|(\d+)\.(\d+)|(?<!\d)-(\d+)|#(\d+)|(\d+)|([A-Za-z]+)/g,
    (match, negInt, negFrac, posInt, posFrac, neg, hashNum, num, alpha) => {
      if (negInt !== undefined) {
        // -3.14 → まいなす<NUMK VAL=3>てん<NUMK VAL=14>
        return `まいなす<NUMK VAL=${negInt}>てん<NUMK VAL=${negFrac}>`;
      }
      if (posInt !== undefined) {
        // 3.14 → <NUMK VAL=3>てん<NUMK VAL=14>
        return `<NUMK VAL=${posInt}>てん<NUMK VAL=${posFrac}>`;
      }
      if (neg !== undefined) {
        // -5 → まいなす<NUMK VAL=5>
        return `まいなす<NUMK VAL=${neg}>`;
      }
      if (hashNum !== undefined) {
        // #42 → <NUMK VAL=42>
        return `<NUMK VAL=${hashNum}>`;
      }
      if (num !== undefined) {
        // 1234 → <NUMK VAL=1234>
        return `<NUMK VAL=${num}>`;
      }
      if (alpha !== undefined) {
        // PR → <ALPHA VAL=PR>, test → <ALPHA VAL=TEST>
        return `<ALPHA VAL=${alpha.toUpperCase()}>`;
      }
      return match;
    },
  );
}

// ---------------------------------------------------------------------------
// kuromoji トークナイズ
// ---------------------------------------------------------------------------

const TAG_REGEX_SOURCE = "<(?:NUMK|ALPHA) VAL=[^>]+>";

/**
 * 前処理済みテキストをトークナイズしてカタカナ列に変換する。
 * タグ部分（<NUMK VAL=...> 等）はトークナイズをスキップして保持する。
 */
function tokenizeToKana(tokenizer, text) {
  const TAG_PATTERN = new RegExp(TAG_REGEX_SOURCE, "g");
  const segments = [];
  let lastIndex = 0;
  let match;

  TAG_PATTERN.lastIndex = 0;
  while ((match = TAG_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, match.index) });
    }
    segments.push({ type: "tag", value: match[0] });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }

  return segments
    .map((seg) => {
      if (seg.type === "tag") return seg.value;
      return tokenizeTextSegment(tokenizer, seg.value);
    })
    .join("");
}

/**
 * 1 トークンの読み（カタカナ）を返す。品詞ベースの汎用規則で読み分ける
 * （個別語の列挙はしない — docs/kai/design/tts-reading-rules.md §4.3-2）。
 */
function tokenReading(token) {
  const surface = token.surface_form;
  // 係助詞「は」→「ワ」（「今日は」「駅では」の は もこれでカバー）
  if (surface === "は" && token.pos_detail_1 === "係助詞") {
    return "ワ";
  }
  // 接続詞・感動詞で表層形が「は」で終わる語 → 読み末尾の「ハ」を「ワ」に
  //（では・それでは・こんにちは・こんばんは 等が個別列挙なしでカバーされる）
  if ((token.pos === "接続詞" || token.pos === "感動詞") && surface.endsWith("は")) {
    const reading = token.reading ?? surface;
    return reading.replace(/ハ$/, "ワ").replace(/は$/, "わ");
  }
  // 格助詞「へ」→「エ」（語中の「へ」= 部屋・経る等は品詞が違うので影響しない）
  if (surface === "へ" && token.pos === "助詞" && token.pos_detail_1 === "格助詞") {
    return "エ";
  }
  // 助詞「を」→「オ」（AquesTalk 公式推奨の読み）
  if (surface === "を" && token.pos === "助詞") {
    return "オ";
  }
  return token.reading ?? surface;
}

/**
 * 息継ぎ用の読点を補ってよい長さか（目安 25 モーラ。小書き文字・促音・長音は
 * モーラに数えない近似）。
 */
function isLongUtterance(kana) {
  const mora = kana.replace(/[ァィゥェォャュョッーぁぃぅぇぉゃゅょっ]/g, "").length;
  return mora > 25;
}

/**
 * テキストセグメントをトークナイズしてカタカナ列に変換する。
 * 品詞ベースの読み分け（tokenReading）と、読点のない長文への読点補完
 * （息継ぎ — 接続助詞の直後に「、」）を行う。
 */
function tokenizeTextSegment(tokenizer, text) {
  if (!text) return "";
  const tokens = tokenizer.tokenize(text);
  const readings = tokens.map(tokenReading);
  const joined = readings.join("");

  // 息継ぎ: もともと読点を含まない長い文に限り、接続助詞（て・で・ので・から等）の
  // 直後に読点を補う。重複・句点直前の読点は convertKanaText 側で整理される。
  if (!text.includes("、") && isLongUtterance(joined)) {
    return tokens
      .map((token, i) => {
        const isConjunctive = token.pos === "助詞" && token.pos_detail_1 === "接続助詞";
        return isConjunctive ? `${readings[i]}、` : readings[i];
      })
      .join("");
  }

  return joined;
}

// ---------------------------------------------------------------------------
// 音声記号列整形
// ---------------------------------------------------------------------------

/**
 * カタカナ列（kuromoji 変換後）を AquesTalk10 音声記号列に整形する。
 * - カタカナ → ひらがな変換
 * - 句切記号の正規化（!→。、?→？など）
 * - スペース除去
 * - タグ（<NUMK VAL=...> 等）はそのまま保持
 * - 文末に。を付与
 *
 * 純粋関数（副作用なし）。
 *
 * @param {string} kana カタカナ列（kuromoji 変換後）またはタグ混在テキスト
 * @returns {string} AquesTalk10 音声記号列
 */
export function formatKoe(kana) {
  if (!kana) return "。";

  // タグ部分と日本語テキスト部分に分割して処理
  const TAG_PATTERN = new RegExp(TAG_REGEX_SOURCE, "g");
  const parts = [];
  let remaining = kana;

  while (remaining.length > 0) {
    TAG_PATTERN.lastIndex = 0;
    const tagMatch = TAG_PATTERN.exec(remaining);
    if (tagMatch && tagMatch.index === 0) {
      // 先頭がタグ → そのまま保持
      parts.push(tagMatch[0]);
      remaining = remaining.slice(tagMatch[0].length);
    } else {
      // 次のタグまたは末尾までテキスト処理
      const nextTagIdx = remaining.search(new RegExp(TAG_REGEX_SOURCE));
      const textPart = nextTagIdx >= 0 ? remaining.slice(0, nextTagIdx) : remaining;
      parts.push(convertKanaText(textPart));
      remaining = nextTagIdx >= 0 ? remaining.slice(nextTagIdx) : "";
    }
  }

  let result = parts.join("");

  // 文末処理: タグを除いたテキストが句切記号で終わっているか確認
  const textOnly = result.replace(new RegExp(TAG_REGEX_SOURCE, "g"), "");
  if (!textOnly.endsWith("。") && !textOnly.endsWith("？")) {
    result += "。";
  }

  return result;
}

/**
 * テキスト部分のカナ変換・句切記号変換を行う。
 */
function convertKanaText(text) {
  // カタカナ（U+30A1〜U+30F6）→ ひらがな（U+3041〜U+3096）
  // 長音符 ー（U+30FC）は変換対象外
  let result = text.replace(/[ァ-ヶ]/g, (ch) =>
    String.fromCharCode(ch.charCodeAt(0) - 0x60),
  );

  // AquesTalk10 が解釈できない仮名の正規化（づ・ぢは音声記号列で未定義 — 実機確認済み）
  result = result.replace(/づ/g, "ず").replace(/ぢ/g, "じ");

  // ゔ（ヴ）は未対応 → ば行で代替（拗音を先に、単独ゔは「ぶ」）
  result = result
    .replace(/ゔぁ/g, "ば")
    .replace(/ゔぃ/g, "び")
    .replace(/ゔぇ/g, "べ")
    .replace(/ゔぉ/g, "ぼ")
    .replace(/ゔゃ/g, "びゃ")
    .replace(/ゔゅ/g, "びゅ")
    .replace(/ゔょ/g, "びょ")
    .replace(/ゔ/g, "ぶ");

  // ぐ行拗音（ぐぃ等）は未対応 → 直音で代替
  result = result
    .replace(/ぐぃ/g, "ぎ")
    .replace(/ぐぅ/g, "ぐ")
    .replace(/ぐぇ/g, "げ")
    .replace(/ぐぉ/g, "ご");

  // を → お（AquesTalk 公式推奨。品詞規則を通らず残った分の安全網）
  result = result.replace(/を/g, "お");

  // 全角・半角括弧とその内容を除去（AquesTalk10非対応文字）
  result = result
    .replace(/[（(][^（()）)]*[）)]/g, "")
    .replace(/[（）()]/g, "");

  // 句切記号の正規化
  result = result
    .replace(/[！!]/g, "。") // 感嘆符 → 長めポーズ
    .replace(/[？?]/g, "？") // 疑問符 → 疑問文末（音上昇）
    .replace(/[、，,]/g, "、") // 読点系 → 短めポーズ
    .replace(/[。．.]/g, "。") // 句点系 → 長めポーズ
    .replace(/[・：:]/g, ";") // 中点・コロン → ポーズなし区切り
    .replace(/[-‐‑–—−]/g, "、"); // ハイフン類 → 短めポーズ（素通しすると合成失敗 — 実機確認済み）

  // 読点補完などで生じた重複読点・句切記号直前の読点を整理
  result = result.replace(/、+/g, "、").replace(/、([。？])/g, "$1");

  // スペース（全角・半角）を除去
  result = result.replace(/[\s　]+/g, "");

  // 最終サニタイズ（許可リスト方式）: AquesTalk10 音声記号列として有効な
  // ひらがな・長音符・句切記号以外（kuromoji が読めず残った漢字・ASCII 残渣等）を
  // 除去する。残すとその文がまるごと合成失敗して字幕のみ縮退になるため、
  // 一部欠落しても発話を継続できる方を取る。
  result = result.replace(/[^ぁ-ゖー。？、;/]/g, "");

  return result;
}

// ---------------------------------------------------------------------------
// LLM 主経路（設計書 §5.2）
// ---------------------------------------------------------------------------

/**
 * 環境変数から LLM 設定を読む。KOE_LLM_BASE_URL が空なら null（= ルールベースのみ）。
 */
function llmConfigFromEnv() {
  const baseUrl = process.env.KOE_LLM_BASE_URL;
  if (!baseUrl) return null;
  return {
    baseUrl,
    model: process.env.KOE_LLM_MODEL ?? "qwen3.6-35b-a3b",
    timeoutMs: Number(process.env.KOE_LLM_TIMEOUT_MS ?? 2500),
    promptVersion: process.env.KOE_PROMPT_VERSION ?? "v2",
  };
}

/**
 * LLM 出力への「は」読み分けの安全網（旧プロジェクトの二段構えに忠実）。
 * プロンプト指示が漏れたケースを kuromoji の品詞判定で確定的に直す。
 * 対象は係助詞「は」と、接続詞・感動詞で「は」で終わる語（では・こんにちは等）
 * — フォールバック経路の tokenReading と同じ汎用規則。
 */
async function applyParticleCorrection(koe) {
  const tokenizer = await initTokenizer();
  if (!tokenizer) return koe;
  return mapNonTagParts(koe, (part) =>
    tokenizer
      .tokenize(part)
      .map((token) => {
        const surface = token.surface_form;
        if (surface === "は" && token.pos_detail_1 === "係助詞") return "わ";
        if ((token.pos === "接続詞" || token.pos === "感動詞") && surface.endsWith("は")) {
          return surface.replace(/は$/, "わ");
        }
        return surface;
      })
      .join(""),
  );
}

// ---------------------------------------------------------------------------
// メイン変換関数
// ---------------------------------------------------------------------------

/**
 * ルールベース変換（kuromoji）。LLM 不達・出力不正時のフォールバック経路。
 *
 * @param {string} text kai の speechText
 * @returns {Promise<string>} AquesTalk10 音声記号列
 */
export async function toKoeRuleBased(text) {
  if (!text) return "";

  try {
    // Step 1: 前処理（例外ワード・記号タグ化）
    const preprocessed = preprocessSymbols(text);

    // Step 2: kuromoji でカナ変換
    const tokenizer = await initTokenizer();
    let kana;

    if (tokenizer) {
      kana = tokenizeToKana(tokenizer, preprocessed);
    } else {
      // kuromoji 未初期化・初期化失敗時はそのまま使用
      kana = preprocessed;
    }

    // Step 3: 音声記号列に整形
    return formatKoe(kana);
  } catch (err) {
    logWarn("音声記号列変換失敗。元テキストで試みます", String(err));
    return text; // フォールバック
  }
}

/**
 * speechText（漢字かな交じり）を AquesTalk10 音声記号列に変換する。
 *
 * 主経路は LLM 変換（KOE_LLM_BASE_URL 設定時）。LLM の不達・タイムアウト・
 * バリデーション違反時はルールベース経路へフォールバックし、発話は決して
 * 止めない（設計書 §5.2・§6）。
 *
 * @param {string} text kai の speechText（例: 「Issue #42 の実装を開始します」）
 * @param {{llm?: object|null}} [options] テスト用に LLM 設定を注入可能
 *        （省略時は環境変数から。null を明示すると LLM を使わない）
 * @returns {Promise<string>} AquesTalk10 音声記号列
 */
export async function toKoe(text, options = {}) {
  if (!text) return "";

  const llmConfig = "llm" in options ? options.llm : llmConfigFromEnv();
  if (!llmConfig) return toKoeRuleBased(text);

  // ルールベース変換を先に行う（v2 プロンプトの「参考よみ」とフォールバックを兼ねる。
  // 漢字の読みは kuromoji のほうが正確 — 実機評価 2026-07-05）
  const ruleBased = await toKoeRuleBased(text);

  try {
    const raw = await generateKoeLlm(text, {
      ...llmConfig,
      terms: TECHNICAL_TERMS,
      referenceKana: ruleBased,
    });
    const koe = ensureSentenceEnd(await applyParticleCorrection(sanitizeLlmKoe(raw)));
    const issues = validateKoe(koe);
    if (koe && issues.length === 0) {
      return koe;
    }
    logWarn("LLM koe がバリデーション違反。ルールベースへフォールバック", {
      issues,
      koe,
    });
  } catch (err) {
    logWarn("LLM koe 生成失敗。ルールベースへフォールバック", String(err));
  }

  return ruleBased;
}
