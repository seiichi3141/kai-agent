/**
 * kai-typewriter — kai（hermes）の編集を受信し、タイピング風に再生する配信演出拡張。
 *
 * 仕組み（設計は Issue #8 / plugins/kai_director）:
 *   1. hermes 側 plugin kai_director が編集完了後（post_tool_call）に
 *      POST http://127.0.0.1:8920/edit {"files": ["/abs/path", ...]} を送る
 *   2. 本拡張は対象ファイルを開き、「前回スナップショット（無ければ現エディタ内容）」と
 *      「ディスク上の最新内容」の差分を求め、変更部分をいったん巻き戻してから
 *      1 文字ずつ挿入してタイピングを再生する
 *   3. 再生完了時点でエディタ内容 == ディスク内容になるため保存しても実害がない
 *      （実ファイルは hermes が既に書き終えている。演出はエディタ側だけで完結）
 *
 * 安全側の原則: 再生はすべて best-effort。差分が取れない・イベントが溜まった等は
 * 「該当ファイルを開いて見せるだけ」に縮退する。拡張の失敗が kai の作業や
 * 実ファイルの内容を壊すことはない（最終状態は常にディスクの内容に収束させる）。
 */

"use strict";

const http = require("http");
const fs = require("fs");
const vscode = require("vscode");

/** @type {Map<string, string>} 最後に再生を終えた時点のファイル内容 */
const snapshots = new Map();

/** @type {string[]} 再生待ちファイルのキュー（直列再生） */
const pending = [];
let playing = false;
let statusBar;

function cfg() {
  const c = vscode.workspace.getConfiguration("kaiTypewriter");
  return {
    port: c.get("port", 8920),
    typeIntervalMs: c.get("typeIntervalMs", 24),
    maxDurationMs: c.get("maxDurationMs", 5000),
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 共通接頭辞・接尾辞を除いた変更領域を返す（文字単位） */
function diffRegion(before, after) {
  let start = 0;
  const minLen = Math.min(before.length, after.length);
  while (start < minLen && before[start] === after[start]) start++;
  let endB = before.length;
  let endA = after.length;
  while (endB > start && endA > start && before[endB - 1] === after[endA - 1]) {
    endB--;
    endA--;
  }
  return { start, removed: before.slice(start, endB), inserted: after.slice(start, endA) };
}

/** ドキュメント全体を text に置き換える（差分適用の土台）。 */
async function setDocumentText(editor, text) {
  const doc = editor.document;
  const fullRange = new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length));
  await editor.edit((eb) => eb.replace(fullRange, text), {
    undoStopBefore: false,
    undoStopAfter: false,
  });
}

/** 1 ファイル分の編集をタイピング再生する。action は "add"（新規）か "update"（更新）。 */
async function playFile(filePath, action) {
  let target;
  try {
    target = fs.readFileSync(filePath, "utf8"); // ディスク上の最終内容が正
  } catch {
    return; // 削除された等 — 何もしない
  }

  const doc = await vscode.workspace.openTextDocument(filePath);
  const editor = await vscode.window.showTextDocument(doc, { preview: false });

  // 新規作成（action=add）は空から全文をタイプする。更新はスナップショット（無ければ
  // 開いた時点の内容）との差分だけタイプする（Issue #32: 新規ファイルが一気に
  // 現れてタイプ演出が見えない不具合の修正）。
  const base = action === "add" ? "" : (snapshots.has(filePath) ? snapshots.get(filePath) : editor.document.getText());
  snapshots.set(filePath, target);

  const { start, inserted } = diffRegion(base, target);
  // 差分なし・極端に巨大な差分（自動生成の大量出力など）は「開いて見せる」だけ
  if (inserted.length === 0 || inserted.length > 100000 || base === target) {
    const pos = editor.document.positionAt(Math.min(start, editor.document.getText().length));
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
    if (editor.document.getText() !== target) {
      await setDocumentText(editor, target);
      await editor.document.save();
    }
    return;
  }

  // 「変更前 + これから打つ部分の手前まで」の状態に巻き戻す
  const prefix = target.slice(0, start);
  const suffix = target.slice(start + inserted.length);
  await setDocumentText(editor, prefix + suffix);

  // 再生速度: 上限時間に収まるよう 1 打鍵あたりの文字数を調整。
  // キューが溜まっているときはさらに倍速で追いつく
  const { typeIntervalMs, maxDurationMs } = cfg();
  const steps = Math.max(1, Math.floor(maxDurationMs / typeIntervalMs));
  let chunk = Math.max(1, Math.ceil(inserted.length / steps));
  if (pending.length > 0) chunk *= 4;

  let typed = 0;
  while (typed < inserted.length) {
    const piece = inserted.slice(typed, typed + chunk);
    const at = editor.document.positionAt(start + typed);
    await editor.edit((eb) => eb.insert(at, piece), {
      undoStopBefore: false,
      undoStopAfter: false,
    });
    typed += piece.length;
    const cursor = editor.document.positionAt(start + typed);
    editor.selection = new vscode.Selection(cursor, cursor);
    editor.revealRange(
      new vscode.Range(cursor, cursor),
      vscode.TextEditorRevealType.InCenterIfOutsideViewport,
    );
    await sleep(typeIntervalMs);
  }

  // 収束の保証: 打鍵の合間に外部変更が挟まっても最終的にディスク内容へ揃える
  if (editor.document.getText() !== target) {
    await setDocumentText(editor, target);
  }
  await editor.document.save();
}

async function drainQueue() {
  if (playing) return;
  playing = true;
  try {
    while (pending.length > 0) {
      const { path: file, action } = pending.shift();
      if (statusBar) statusBar.text = `$(edit) kai: ${file.split("/").pop()}`;
      try {
        await playFile(file, action);
      } catch (e) {
        console.error("[kai-typewriter] play error:", e);
      }
    }
  } finally {
    playing = false;
    if (statusBar) statusBar.text = "$(check) kai typewriter";
  }
}

// --- VSCode ブリッジ（Issue #49）: 状態取得・ファイル操作 ------------------------

/** タブの入力からファイルパスを取り出す（TabInputText 等）。無ければ null。 */
function tabPath(tab) {
  const input = tab && tab.input;
  if (input && input.uri && input.uri.scheme === "file") return input.uri.fsPath;
  return null;
}

/** GET /state: 開いているタブ・アクティブファイル+行・dirty を返す。 */
function getState() {
  const tabs = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const path = tabPath(tab);
      if (path) {
        tabs.push({ path, active: !!tab.isActive, dirty: !!tab.isDirty, group: group.viewColumn });
      }
    }
  }
  const ed = vscode.window.activeTextEditor;
  const active = ed
    ? {
        path: ed.document.uri.fsPath,
        line: ed.selection.active.line + 1, // 1-indexed（人間表示に合わせる）
        column: ed.selection.active.character,
        dirty: ed.document.isDirty,
      }
    : null;
  const visibleEditors = vscode.window.visibleTextEditors.map((e) => e.document.uri.fsPath);
  return { active, tabs, visibleEditors };
}

/** 相対パスはワークスペースルート基準で絶対化する（kai は相対で渡すことがある）。 */
function resolvePath(p) {
  if (typeof p !== "string" || p.startsWith("/")) return p;
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length) {
    const path = require("path");
    return path.join(folders[0].uri.fsPath, p);
  }
  return p;
}

/** POST /open {path, line?}: ファイルを開き該当行へスクロール。 */
async function openFile(path, line) {
  const doc = await vscode.workspace.openTextDocument(resolvePath(path));
  const editor = await vscode.window.showTextDocument(doc, { preview: false });
  if (typeof line === "number" && line > 0) {
    const pos = new vscode.Position(Math.max(0, line - 1), 0);
    editor.selection = new vscode.Selection(pos, pos);
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
  }
  return { opened: path };
}

/** POST /close {path} | {all:true}: タブを閉じる。 */
async function closeTab(data) {
  if (data.all) {
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
    return { closed: "all" };
  }
  const target = resolvePath(data.path);
  const toClose = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      if (tabPath(tab) === target) toClose.push(tab);
    }
  }
  if (toClose.length) await vscode.window.tabGroups.close(toClose);
  return { closed: target, count: toClose.length };
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      try {
        resolve(JSON.parse(body || "{}"));
      } catch {
        resolve({});
      }
    });
  });
}

function sendJson(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(obj));
}

function handleEdit(data) {
  // 新形式: edits=[{path, action}]。旧形式 files=[path] は update 扱い
  const edits = Array.isArray(data.edits)
    ? data.edits
    : (Array.isArray(data.files) ? data.files.map((p) => ({ path: p, action: "update" })) : []);
  for (const e of edits) {
    const path = e && e.path;
    if (typeof path === "string" && path.startsWith("/") && !pending.some((q) => q.path === path)) {
      pending.push({ path, action: e.action === "add" ? "add" : "update" });
    }
  }
  void drainQueue();
  return { queued: pending.length };
}

function activate(context) {
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
  statusBar.text = "$(check) kai typewriter";
  statusBar.show();
  context.subscriptions.push(statusBar);

  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/state") {
        sendJson(res, 200, getState());
        return;
      }
      if (req.method === "POST" && req.url === "/edit") {
        sendJson(res, 200, handleEdit(await readBody(req)));
        return;
      }
      if (req.method === "POST" && req.url === "/open") {
        const d = await readBody(req);
        sendJson(res, 200, await openFile(d.path, d.line));
        return;
      }
      if (req.method === "POST" && req.url === "/close") {
        sendJson(res, 200, await closeTab(await readBody(req)));
        return;
      }
      sendJson(res, 404, { error: "not found" });
    } catch (e) {
      sendJson(res, 400, { error: String(e) });
    }
  });
  server.listen(cfg().port, "127.0.0.1");
  context.subscriptions.push({ dispose: () => server.close() });
  console.log(`[kai-typewriter] bridge listening on 127.0.0.1:${cfg().port}`);
}

function deactivate() {}

module.exports = { activate, deactivate, diffRegion };
