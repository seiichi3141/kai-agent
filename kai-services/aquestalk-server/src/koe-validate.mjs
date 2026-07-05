/**
 * koe（AquesTalk10 音声記号列）の静的バリデーション。
 *
 * CLI を呼ばずに「合成に失敗しそうな koe」を検出する。旧 kai プロジェクト
 * scripts/koe-validate.ts の移植（設計書 §5.2-4）。LLM 出力の採用可否の
 * ゲートとして使い、違反が 1 つでもあればルールベース経路へフォールバックする。
 */

import { TAG_REGEX_SOURCE } from "./koe-llm.mjs";

/**
 * @param {string} koe 検証対象の音声記号列
 * @returns {string[]} 違反理由の配列（空なら合成可能と判断）
 */
export function validateKoe(koe) {
  const issues = [];
  // タグ内の英数字は正当なので、タグを除いた文字列でも判定する
  const outsideTags = koe.replace(new RegExp(TAG_REGEX_SOURCE, "g"), "");

  if (/[「」『』（）()]/.test(koe)) issues.push("鉤括弧・括弧が含まれている");
  if (/[a-zA-Z]/.test(outsideTags)) issues.push("タグ外に半角英字が含まれている");
  if (/[ａ-ｚＡ-Ｚ]/.test(koe)) issues.push("全角英字が含まれている");
  if (/[一-鿿]/.test(koe)) issues.push("漢字が含まれている");
  if (/[ァ-ヶ]/.test(outsideTags)) issues.push("タグ外にカタカナが含まれている");
  if (/[0-9]/.test(outsideTags)) issues.push("タグ外に半角数字が含まれている");
  if (/ゔ/.test(koe)) issues.push("ゔが含まれている");
  if (/[！!]/.test(koe)) issues.push("感嘆符が含まれている");
  if (/:/.test(outsideTags)) issues.push("コロンが含まれている");
  if (/-/.test(outsideTags)) issues.push("ハイフンが含まれている");

  return issues;
}
