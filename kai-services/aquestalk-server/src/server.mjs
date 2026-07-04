/**
 * aquestalk-server — Mac 上で常駐する汎用日本語 TTS HTTP サーバー。
 *
 * AquesTalk10 (aquestalk_cli) を使い「日本語テキスト → 音声」を提供する自己完結サービス。
 * kai 固有ロジックは持たない（純粋な TTS API）。
 *
 * API:
 *   GET  /health      -> {"ok": true}
 *   POST /synthesize   -> NDJSON ストリーミング（1 文につき 1 行）
 */

import { createServer } from "node:http";
import { toKoe } from "./converter.mjs";
import { synthesize } from "./aquestalk.mjs";
import { splitSentences } from "./text-splitter.mjs";

// .env があれば読み込む（Node 20.12+ / 21.7+ の process.loadEnvFile を使用）。
// 既に環境変数が設定済み（systemd の EnvironmentFile 等）の場合はそのまま尊重される。
try {
  process.loadEnvFile();
} catch {
  // .env が存在しない場合は何もしない（環境変数が別途設定されている前提）
}

const PORT = Number(process.env.PORT ?? 8890);
const BIND_ADDR = process.env.BIND_ADDR ?? "127.0.0.1";

const DEFAULT_VOICE = "F1";
const DEFAULT_SPEED = 120;

function log(...args) {
  console.log(new Date().toISOString(), "[aquestalk-server]", ...args);
}

function logError(...args) {
  console.error(new Date().toISOString(), "[aquestalk-server]", ...args);
}

/**
 * リクエストボディを読み取り JSON として parse する。
 * @param {import("node:http").IncomingMessage} req
 * @returns {Promise<any>}
 */
async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  const raw = Buffer.concat(chunks).toString("utf-8");
  if (raw.trim().length === 0) return {};
  return JSON.parse(raw);
}

function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

/**
 * POST /synthesize ハンドラ。
 * text を文単位に分割し、各文を koe 変換・音声合成して NDJSON で逐次ストリーミングする。
 * 個別の文の変換・合成が失敗しても、その文だけ error を出して次の文へ継続する。
 */
async function handleSynthesize(req, res) {
  let body;
  try {
    body = await readJsonBody(req);
  } catch (err) {
    sendJson(res, 400, { error: `invalid JSON body: ${String(err && err.message ? err.message : err)}` });
    return;
  }

  const text = typeof body.text === "string" ? body.text : "";
  const voice = typeof body.voice === "string" && body.voice.length > 0 ? body.voice : DEFAULT_VOICE;
  const speed = typeof body.speed === "number" && Number.isFinite(body.speed) ? body.speed : DEFAULT_SPEED;

  if (!text) {
    sendJson(res, 400, { error: "text is required" });
    return;
  }

  const sentences = splitSentences(text);

  res.writeHead(200, {
    "Content-Type": "application/x-ndjson",
    "Transfer-Encoding": "chunked",
  });

  for (let seq = 0; seq < sentences.length; seq++) {
    const sentence = sentences[seq];
    let koe;
    try {
      koe = await toKoe(sentence);
      const wav = await synthesize(koe, { voice, speed });
      res.write(
        JSON.stringify({ seq, text: sentence, koe, wav_base64: wav.toString("base64") }) + "\n",
      );
    } catch (err) {
      const message = String(err && err.message ? err.message : err);
      logError("sentence synthesis failed", { seq, text: sentence, error: message });
      res.write(JSON.stringify({ seq, text: sentence, error: message }) + "\n");
    }
  }

  res.end();
}

const server = createServer((req, res) => {
  Promise.resolve()
    .then(async () => {
      if (req.method === "GET" && req.url === "/health") {
        sendJson(res, 200, { ok: true });
        return;
      }

      if (req.method === "POST" && req.url === "/synthesize") {
        await handleSynthesize(req, res);
        return;
      }

      sendJson(res, 404, { error: "not found" });
    })
    .catch((err) => {
      logError("unhandled request error", String(err));
      if (!res.headersSent) {
        sendJson(res, 500, { error: "internal server error" });
      } else {
        res.end();
      }
    });
});

server.listen(PORT, BIND_ADDR, () => {
  log(`listening on http://${BIND_ADDR}:${PORT}`);
});

process.on("SIGTERM", () => {
  log("received SIGTERM, shutting down");
  server.close(() => process.exit(0));
});
process.on("SIGINT", () => {
  log("received SIGINT, shutting down");
  server.close(() => process.exit(0));
});
