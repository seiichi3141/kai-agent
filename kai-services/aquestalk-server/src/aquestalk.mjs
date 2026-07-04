/**
 * aquestalk_cli を subprocess として呼び出し、音声記号列（koe）から WAV バイナリを生成する。
 *
 * kai の packages/tts/src/cli-wrappers.ts の synthesize() 部分（AquesTalk CLI 呼び出しと
 * koe のサニタイズ・リトライロジック）を素の JS/ESM に移植したもの。
 * LLM による koe 生成・リップシンク解析・再生バックエンドは持たない（純粋な TTS API のため）。
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

/** aquestalk_cli の出力（WAV）想定最大サイズ。長文でも十分な余裕を持たせる */
const MAX_BUFFER_BYTES = 64 * 1024 * 1024;

/**
 * 6 回以上連続する同一文字を 6 回に切り詰める（AquesTalk CLI の暴走防止）。
 */
function trimRepeatedCharacters(koe) {
  return koe.replace(/(.)\1{6,}/gu, "$1$1$1$1$1$1");
}

/**
 * 句切記号を正規化し、AquesTalk CLI が解釈しづらい連続記号を整える。
 */
function normalizeAquestalkKoePunctuation(koe) {
  return koe
    .replace(/[！!]+/g, "。")
    .replace(/。{2,}/g, "。")
    .replace(/\/{2,}/g, "/")
    .replace(/([。？、,;/])ー/g, "$1")
    .replace(/ー{2,}/g, "ー")
    .replace(/っ{2,}/g, "っ")
    .replace(/っ([。？、])/g, "$1");
}

/**
 * AquesTalk CLI に渡すと危険・不要な文字（制御文字・引用符・Markdown 記号等）を除去する。
 * NUMK/ALPHA タグは preprocessSymbols() の意図に反して素通しされると CLI が誤解釈するため除去する。
 */
function stripAquestalkCliDangerousChars(koe) {
  return koe
    .replace(/<(?:ALPHA|NUMK) VAL=[^>]+>/gi, "")
    .replace(/<[^>]*$/g, "")
    .replace(/```+/g, "")
    .replace(/[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F]/g, "")
    .replace(/[{}[\]()"'“”‘’「」『』]/g, "")
    .replace(/[`*_~|\\<>]/g, "")
    .replace(/[ 　\t\r\n]+/g, "");
}

function sanitizeAquestalkCliKoe(koe) {
  return trimRepeatedCharacters(
    normalizeAquestalkKoePunctuation(stripAquestalkCliDangerousChars(koe)),
  ).trim();
}

/**
 * 通常のサニタイズ後もなお CLI が失敗する場合の再試行用: スラッシュ（ポーズ記号）も除去する。
 */
function sanitizeAquestalkCliRetryKoe(koe) {
  return sanitizeAquestalkCliKoe(koe).replace(/\//g, "").trim();
}

function buildSynthesizeArgs(koe, voice, speed) {
  return [koe, voice, String(speed)];
}

/**
 * koe（AquesTalk10 音声記号列）を aquestalk_cli で WAV バイナリに変換する。
 *
 * 必須環境変数:
 *   AQUESTALK_CLI_PATH - aquestalk_cli バイナリのパス
 *   AQUESTALK_SDK_DIR  - libAquesTalk10.dylib のあるディレクトリ（DYLD_LIBRARY_PATH に設定）
 * 任意環境変数:
 *   AQUESTALK_DEV_KEY, AQUESTALK_USR_KEY - ライセンスキー（process.env 経由で subprocess に継承される）
 *
 * @param {string} koe AquesTalk10 音声記号列
 * @param {{ voice?: string, speed?: number }} [options]
 * @returns {Promise<Buffer>} WAV バイナリ
 */
export async function synthesize(koe, options = {}) {
  const voice = options.voice ?? "F1";
  const speed = options.speed ?? 120;

  const cliPath = process.env.AQUESTALK_CLI_PATH;
  const sdkDir = process.env.AQUESTALK_SDK_DIR;

  if (!cliPath) {
    throw new Error("AQUESTALK_CLI_PATH environment variable is not set");
  }
  if (!sdkDir) {
    throw new Error("AQUESTALK_SDK_DIR environment variable is not set");
  }

  // AQUESTALK_DEV_KEY / AQUESTALK_USR_KEY を含め、process.env はそのまま subprocess に継承される。
  // DYLD_LIBRARY_PATH のみ SDK ディレクトリで上書きする。
  const env = { ...process.env, DYLD_LIBRARY_PATH: sdkDir };

  const rawKoe = koe ?? "";
  const sanitized = sanitizeAquestalkCliKoe(rawKoe);
  const args = buildSynthesizeArgs(sanitized, voice, speed);
  const execOptions = { encoding: "buffer", maxBuffer: MAX_BUFFER_BYTES, env };

  try {
    const { stdout } = await execFileAsync(cliPath, args, execOptions);
    return Buffer.from(stdout);
  } catch (error) {
    const retryKoe = sanitizeAquestalkCliRetryKoe(sanitized);
    if (retryKoe.length > 0 && retryKoe !== sanitized) {
      const retryArgs = buildSynthesizeArgs(retryKoe, voice, speed);
      const { stdout } = await execFileAsync(cliPath, retryArgs, execOptions);
      return Buffer.from(stdout);
    }
    throw error;
  }
}
