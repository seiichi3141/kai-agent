/**
 * テキストを文単位に分割するユーティリティ。
 * 句点「。」「！」「？」の直後、および改行で分割する。空文は除去する。
 */

/** 分割対象の句切記号（全角）。改行はこれとは別に無条件で分割点になる */
const SENTENCE_END_PATTERN = /(?<=[。！？])|\r\n|\n/;

/**
 * テキストを文単位に分割する。
 *
 * @param {string} text 分割対象のテキスト
 * @returns {string[]} 空文を除いた文の配列
 */
export function splitSentences(text) {
  if (!text) return [];

  return text
    .split(SENTENCE_END_PATTERN)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence.length > 0);
}
