// Turn any well-formed GitHub-markdown tables inside a block of text into TAB-delimited rows, so a
// bubble copied out of the console pastes into Slack (and Excel/Sheets) as real columns instead of a
// pipe-run. ONLY contiguous runs that are an actual markdown table — piped rows WITH a `|---|---|`
// separator line — are transformed; every other line (prose, code, a lone sentence containing a
// pipe) is left exactly as it was, so this can never mangle ordinary text.

import { escapeHtml, tableToHtml } from "./tableexport";

const isRow = (line: string) => /^\s*\|.*\|\s*$/.test(line);
// The header/body divider: pipes around only dashes, colons and whitespace.
const isDivider = (line: string) => /^\s*\|(?:\s*:?-+:?\s*\|)+\s*$/.test(line);

function rowToCells(line: string): string[] {
  const t = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  // Split on unescaped pipes; unescape \| back to | in the cell text.
  return t
    .split(/(?<!\\)\|/)
    .map((c) => c.replace(/\\\|/g, "|").replace(/[\t\r\n]+/g, " ").trim());
}

type Segment =
  | { kind: "text"; lines: string[] }
  | { kind: "table"; columns: string[]; rows: string[][] };

/** The bubble split into prose runs and real markdown tables — one parser, so the tab-delimited
 * and the HTML copies can never disagree about what counts as a table. */
function parse(text: string): Segment[] {
  const lines = text.split("\n");
  const out: Segment[] = [];
  let i = 0;
  const pushText = (line: string) => {
    const last = out[out.length - 1];
    if (last && last.kind === "text") last.lines.push(line);
    else out.push({ kind: "text", lines: [line] });
  };
  while (i < lines.length) {
    // A table needs at least a header row, a divider, and the run of piped rows around it.
    if (isRow(lines[i]) && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const columns = rowToCells(lines[i]);
      let j = i + 2;
      const rows: string[][] = [];
      while (j < lines.length && isRow(lines[j]) && !isDivider(lines[j])) {
        rows.push(rowToCells(lines[j]));
        j++;
      }
      out.push({ kind: "table", columns, rows });
      i = j;
    } else {
      pushText(lines[i]);
      i++;
    }
  }
  return out;
}

export function tablesToTabs(text: string): string {
  return parse(text)
    .map((s) =>
      s.kind === "table"
        ? [s.columns, ...s.rows].map((r) => r.join("\t")).join("\n")
        : s.lines.join("\n"))
    .join("\n");
}

/** The same bubble as HTML: every markdown table becomes a real <table>, the prose around it keeps
 * its line breaks. What a mail client reads when the bubble is copied. */
export function tablesToHtml(text: string): string {
  return parse(text)
    .map((s) =>
      s.kind === "table"
        ? tableToHtml({ columns: s.columns, rows: s.rows })
        : '<div style="font:13px Arial, Helvetica, sans-serif;white-space:pre-wrap;">' +
          `${escapeHtml(s.lines.join("\n"))}</div>`)
    .join("");
}
