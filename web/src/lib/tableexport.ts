// Getting a table OUT of HELIX and into something else — Gmail, Outlook, Word, Slack, Excel.
//
// The old copy button wrote tab-delimited text only. That pastes into Excel as columns, but into
// Gmail as a wall of text: a mail composer reads text/html, and there was none on the clipboard.
// So copyTable() writes BOTH flavours in one clipboard item — text/html (a real <table> with inline
// styles, because mail clients strip stylesheets) and text/plain (the same tabs as before). Each
// target picks the one it understands.
//
// The image path is a hand-drawn canvas rather than SVG-in-an-<img>: an SVG with foreignObject
// taints the canvas in some engines, and toBlob() on a tainted canvas throws. Measuring and
// filling text is a few more lines and always works.
//
// Light palette on purpose: these leave HELIX for an email or a document, where cyan-on-black
// reads as a screenshot of something broken and costs a fortune to print.

import { api } from "./api";

const INK = "#1f2328";
const MUTED = "#57606a";
const LINE = "#d0d7de";
const HEAD_BG = "#f1f5f9";
const ZEBRA = "#f8fafc";
const FONT = "13px Arial, Helvetica, sans-serif";
const HEAD_FONT = "bold 13px Arial, Helvetica, sans-serif";

export const isNumericCell = (v: string) => /^-?[\d,.%$]+$/.test(v.trim());

/** One cell's text with the tabs and newlines that would break a row taken out. */
export const cleanCell = (v: string) => String(v ?? "").replace(/[\t\r\n]+/g, " ").trim();

export const escapeHtml = (v: string) =>
  v.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export interface TableData {
  columns: string[];
  rows: string[][];
  title?: string;
}

/** Tab-delimited: Excel, Sheets and Slack each put a cell in its own column. */
export function tableToTabs({ columns, rows }: TableData): string {
  return [columns, ...rows].map((r) => r.map(cleanCell).join("\t")).join("\n");
}

/** RFC-4180 CSV, for a file someone opens later. */
export function tableToCsv({ columns, rows }: TableData): string {
  const cell = (v: string) => {
    const s = cleanCell(v);
    return /[",]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [columns, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
}

/** A self-contained <table>: every rule inline, so a mail client that drops <style> keeps the grid. */
export function tableToHtml({ columns, rows, title }: TableData): string {
  const th = (c: string) =>
    `<th style="border:1px solid ${LINE};background:${HEAD_BG};color:${INK};font:${HEAD_FONT};` +
    `padding:6px 10px;text-align:left;">${escapeHtml(cleanCell(c))}</th>`;
  const td = (c: string) =>
    `<td style="border:1px solid ${LINE};color:${INK};font:${FONT};padding:6px 10px;` +
    `text-align:${isNumericCell(c) ? "right" : "left"};">${escapeHtml(cleanCell(c))}</td>`;
  const head = `<tr>${columns.map(th).join("")}</tr>`;
  const body = rows
    .map((r, i) => `<tr${i % 2 ? ` style="background:${ZEBRA};"` : ""}>${r.map(td).join("")}</tr>`)
    .join("");
  const caption = title
    ? `<caption style="caption-side:top;text-align:left;color:${MUTED};font:${HEAD_FONT};` +
      `padding:0 0 6px 0;">${escapeHtml(title)}</caption>`
    : "";
  return (
    `<table style="border-collapse:collapse;border:1px solid ${LINE};font:${FONT};color:${INK};">` +
    `${caption}<thead>${head}</thead><tbody>${body}</tbody></table>`
  );
}

/**
 * Put the table on the clipboard as HTML *and* as text. Returns false when the browser has no
 * ClipboardItem (then the caller's plain-text fallback already ran).
 */
/**
 * The old way, and the one that actually works in the app's WebView2: hook the `copy` event, put
 * both flavours on `clipboardData` by hand, and fire it with execCommand. It runs SYNCHRONOUSLY
 * inside the click, so it needs no clipboard permission and no user-activation grace — which is
 * why the async navigator.clipboard.write path silently lost its text/html flavour in the desktop
 * window and tables pasted into Gmail as flattened text.
 */
function copyViaEvent(html: string, text: string): boolean {
  let wrote = false;
  const onCopy = (e: ClipboardEvent) => {
    e.preventDefault();
    e.clipboardData?.setData("text/html", html);
    e.clipboardData?.setData("text/plain", text);
    wrote = true;
  };
  document.addEventListener("copy", onCopy, true);
  const holder = document.createElement("div");
  try {
    // execCommand("copy") needs something selected to act on; an off-screen node gives it one
    // without disturbing what the user has selected on the page.
    holder.setAttribute("aria-hidden", "true");
    holder.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;";
    holder.textContent = text;
    document.body.appendChild(holder);
    const range = document.createRange();
    range.selectNodeContents(holder);
    const selection = window.getSelection();
    const previous = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
    selection?.removeAllRanges();
    selection?.addRange(range);
    const fired = document.execCommand("copy");
    selection?.removeAllRanges();
    if (previous) selection?.addRange(previous);
    wrote = wrote && fired;
  } catch {
    wrote = false;
  } finally {
    holder.remove();
    document.removeEventListener("copy", onCopy, true);
  }
  return wrote;
}

/**
 * Both flavours onto the clipboard. True means the HTML really landed (the caller says so, because
 * a silent drop to text is the bug this function exists to prevent).
 */
export async function copyRich(html: string, text: string): Promise<boolean> {
  if (copyViaEvent(html, text)) return true;
  try {
    if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        }),
      ]);
      return true;
    }
  } catch {
    // A rejected write (permission, an unfocused window) falls through to the text path below.
  }
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Nothing more to try: the caller reports that only text (or nothing) reached the clipboard.
  }
  return false;
}

export function copyTable(data: TableData): Promise<boolean> {
  return copyRich(tableToHtml(data), tableToTabs(data));
}

/** Draw the table into a canvas: measured columns, a header row, zebra rows, right-aligned numbers. */
export function tableToCanvas({ columns, rows, title }: TableData): HTMLCanvasElement {
  const scale = Math.min(3, Math.max(2, Math.round(window.devicePixelRatio || 1) + 1));
  const padX = 12;
  const rowH = 30;
  const titleH = title ? 30 : 0;
  const measure = document.createElement("canvas").getContext("2d")!;
  const widths = columns.map((c, i) => {
    measure.font = HEAD_FONT;
    let w = measure.measureText(cleanCell(c)).width;
    measure.font = FONT;
    for (const r of rows) w = Math.max(w, measure.measureText(cleanCell(r[i] ?? "")).width);
    return Math.ceil(w) + padX * 2;
  });
  const width = widths.reduce((a, b) => a + b, 0) + 1;
  const height = titleH + rowH * (rows.length + 1) + 1;

  const canvas = document.createElement("canvas");
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext("2d")!;
  ctx.scale(scale, scale);
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  if (title) {
    ctx.fillStyle = MUTED;
    ctx.font = HEAD_FONT;
    ctx.fillText(cleanCell(title), 0.5, titleH / 2);
  }

  const line = (x1: number, y1: number, x2: number, y2: number) => {
    ctx.strokeStyle = LINE;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  };

  const cell = (text: string, x: number, w: number, y: number, right: boolean) => {
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, rowH);
    ctx.clip();
    ctx.fillStyle = INK;
    const t = cleanCell(text);
    ctx.textAlign = right ? "right" : "left";
    ctx.fillText(t, right ? x + w - padX : x + padX, y + rowH / 2);
    ctx.restore();
  };

  // Header.
  let y = titleH + 0.5;
  ctx.fillStyle = HEAD_BG;
  ctx.fillRect(0.5, y, width - 1, rowH);
  ctx.font = HEAD_FONT;
  let x = 0.5;
  columns.forEach((c, i) => {
    cell(c, x, widths[i], y, false);
    x += widths[i];
  });

  // Body.
  ctx.font = FONT;
  rows.forEach((row, ri) => {
    y = titleH + 0.5 + rowH * (ri + 1);
    if (ri % 2) {
      ctx.fillStyle = ZEBRA;
      ctx.fillRect(0.5, y, width - 1, rowH);
    }
    x = 0.5;
    columns.forEach((_c, ci) => {
      const v = row[ci] ?? "";
      cell(v, x, widths[ci], y, isNumericCell(v));
      x += widths[ci];
    });
  });

  // Grid: every row line, then every column line.
  for (let r = 0; r <= rows.length + 1; r++) {
    const ly = titleH + 0.5 + rowH * r;
    line(0.5, ly, width - 0.5, ly);
  }
  x = 0.5;
  for (let c = 0; c <= columns.length; c++) {
    line(x, titleH + 0.5, x, height - 0.5);
    x += widths[c] ?? 0;
  }
  return canvas;
}

const canvasBlob = (canvas: HTMLCanvasElement): Promise<Blob | null> =>
  new Promise((resolve) => canvas.toBlob(resolve, "image/png"));

/** The table as a PNG on the clipboard — one paste into Gmail, Slack or a document. */
export async function copyTableImage(data: TableData): Promise<boolean> {
  const blob = await canvasBlob(tableToCanvas(data));
  if (!blob) return false;
  try {
    if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      return true;
    }
  } catch {
    // Fall through: the caller offers the download instead.
  }
  return false;
}

/** Save a blob under `name` — the fallback when the clipboard refuses an image. */
export function download(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

const toBase64 = (blob: Blob): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(blob);
  });

/**
 * Hand the bytes to the backend, which writes them into the user's Downloads folder and answers
 * with the path. The desktop window swallows a plain <a download> (and may refuse the clipboard
 * altogether), so this is the way a file reliably leaves HELIX.
 */
export async function saveFile(name: string, blob: Blob): Promise<string> {
  try {
    const data = await toBase64(blob);
    const res = (await api.post("/api/export/save", { name, data })) as { path?: string };
    if (res?.path) return res.path;
  } catch {
    // Fall through to the browser's own download, which works when the face runs in a real browser.
  }
  download(blob, name);
  return "";
}

export async function saveTableImage(data: TableData, name: string): Promise<string> {
  const blob = await canvasBlob(tableToCanvas(data));
  return blob ? saveFile(name, blob) : "";
}

export function saveTableCsv(data: TableData, name: string): Promise<string> {
  // The BOM is what makes Excel open a UTF-8 CSV without mangling °, — and accented names.
  return saveFile(name, new Blob(["﻿" + tableToCsv(data)], { type: "text/csv;charset=utf-8" }));
}

export async function downloadTableImage(data: TableData, name: string): Promise<boolean> {
  const blob = await canvasBlob(tableToCanvas(data));
  if (!blob) return false;
  download(blob, name);
  return true;
}

export function downloadTableCsv(data: TableData, name: string): void {
  // The BOM is what makes Excel open a UTF-8 CSV without mangling °, — and accented names.
  download(new Blob(["﻿" + tableToCsv(data)], { type: "text/csv;charset=utf-8" }), name);
}

/** A filename from the table's title: 'AFRU fields' → 'afru-fields'. */
export function slugify(title: string | undefined, fallback: string): string {
  const s = (title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s || fallback;
}
