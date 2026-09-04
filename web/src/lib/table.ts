// Turn any well-formed GitHub-markdown tables inside a block of text into TAB-delimited rows, so a
// bubble copied out of the console pastes into Slack (and Excel/Sheets) as real columns instead of a
// pipe-run. ONLY contiguous runs that are an actual markdown table — piped rows WITH a `|---|---|`
// separator line — are transformed; every other line (prose, code, a lone sentence containing a
// pipe) is left exactly as it was, so this can never mangle ordinary text.

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

export function tablesToTabs(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    // A table needs at least a header row, a divider, and the run of piped rows around it.
    if (isRow(lines[i]) && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const header = lines[i];
      let j = i + 2;
      const body: string[] = [];
      while (j < lines.length && isRow(lines[j]) && !isDivider(lines[j])) {
        body.push(lines[j]);
        j++;
      }
      out.push([header, ...body].map((r) => rowToCells(r).join("\t")).join("\n"));
      i = j;
    } else {
      out.push(lines[i]);
      i++;
    }
  }
  return out.join("\n");
}
