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

const _require = createRequire(import.meta.url);

function logWarn(message, meta) {
  console.warn(`[tts-converter] ${message}`, meta ?? "");
}

// ---------------------------------------------------------------------------
// 技術用語辞書（technical-terms.json を読み込み、例外辞書を構築する）
// ---------------------------------------------------------------------------

const TERMS_JSON_PATH = join(dirname(fileURLToPath(import.meta.url)), "technical-terms.json");

/** 技術用語辞書: { 用語: 読み } */
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

/** buildExceptionDict() のキャッシュ。技術用語辞書はプロセス起動後は不変なので一度だけ構築する */
let exceptionDictCache = null;

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
    .map(([term, reading]) => [new RegExp(termBoundaryPattern(term), "g"), reading]);
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
 * テキストセグメントをトークナイズしてカタカナ列に変換する。
 * 助詞「は」（係助詞）は「ワ」に変換する。
 */
function tokenizeTextSegment(tokenizer, text) {
  if (!text) return "";
  const tokens = tokenizer.tokenize(text);
  return tokens
    .map((token) => {
      // 助詞「は」（係助詞）→「ワ」
      if (token.surface_form === "は" && token.pos_detail_1 === "係助詞") {
        return "ワ";
      }
      return token.reading ?? token.surface_form;
    })
    .join("");
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
    .replace(/[・：:]/g, ";"); // 中点・コロン → ポーズなし区切り

  // スペース（全角・半角）を除去
  result = result.replace(/[\s　]+/g, "");

  return result;
}

// ---------------------------------------------------------------------------
// メイン変換関数
// ---------------------------------------------------------------------------

/**
 * speechText（漢字かな交じり）を AquesTalk10 音声記号列に変換する。
 *
 * @param {string} text kai の speechText（例: 「Issue #42 の実装を開始します」）
 * @returns {Promise<string>} AquesTalk10 音声記号列（例: 「いっしゅー<NUMK VAL=42>のじっそうをかいしします。」）
 *          変換失敗時は元のテキストをそのまま返す（フォールバック）
 */
export async function toKoe(text) {
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
