// A small Markdown renderer for PQ's answers: headings, bold, italic, inline code, fenced
// code, lists (nested), tables, quotes, links and rules. No dependency.
// Safety: every piece of text is HTML-escaped before any tag is added, and only http(s) and
// mailto links become clickable, so an answer can never inject markup or scripts.
(function () {
  "use strict";

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(text) {
    const codes = [];
    let out = escapeHtml(text);
    // inline code first: nothing inside it is formatted
    out = out.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, function (_m, _ticks, code) {
      codes.push("<code>" + code.trim() + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (match, label, url) {
      return /^(https?:\/\/|mailto:)/i.test(url)
        ? '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>"
        : match;
    });
    out = out.replace(/\*\*([^*\n][^\n]*?)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^\w])__([^_\n][^\n]*?)__(?!\w)/g, "$1<strong>$2</strong>");
    out = out.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^\w])_([^_\s][^_\n]*?)_(?!\w)/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return out.replace(/\u0000(\d+)\u0000/g, function (_m, i) { return codes[Number(i)]; });
  }

  const FENCE = /^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$/;
  const HEADING = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
  const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function cells(line) {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map(function (c) { return c.trim().replace(/\\\|/g, "|"); });
  }

  function codeBlock(lang, lines) {
    const label = escapeHtml(lang || "code");
    return '<div class="code"><div class="code-head"><span>' + label + '</span>' +
      '<button type="button" class="copy-code">Copy</button></div><pre><code>' +
      escapeHtml(lines.join("\n")) + "</code></pre></div>";
  }

  // items: [{indent, ordered, text}] -> nested <ul>/<ol>
  function listHtml(items) {
    let pos = 0;
    function level(indent) {
      const ordered = items[pos].ordered;
      let html = ordered ? "<ol>" : "<ul>";
      while (pos < items.length && items[pos].indent >= indent) {
        const item = items[pos++];
        html += "<li>" + inline(item.text).replace(/\n/g, "<br>");
        if (pos < items.length && items[pos].indent > item.indent) html += level(items[pos].indent);
        html += "</li>";
      }
      return html + (ordered ? "</ol>" : "</ul>");
    }
    let html = "";
    while (pos < items.length) html += level(items[pos].indent);
    return html;
  }

  function render(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) { i++; continue; }

      const fence = line.match(FENCE);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !(lines[i].trim().startsWith(fence[1]) && /^\s*(```+|~~~+)\s*$/.test(lines[i]))) {
          body.push(lines[i++]);
        }
        i++; // the closing fence (or the end of an unfinished answer)
        out.push(codeBlock(fence[2], body));
        continue;
      }

      const heading = line.match(HEADING);
      if (heading) {
        const tag = "h" + Math.min(heading[1].length + 1, 5); // "# x" is a section, not a page title
        out.push("<" + tag + ">" + inline(heading[2]) + "</" + tag + ">");
        i++;
        continue;
      }

      if (RULE.test(line)) { out.push("<hr>"); i++; continue; }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const head = cells(line);
        const aligns = cells(lines[i + 1]).map(function (c) {
          return c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "";
        });
        i += 2;
        let html = '<div class="table-wrap"><table><thead><tr>';
        head.forEach(function (c, n) {
          html += "<th" + (aligns[n] ? ' style="text-align:' + aligns[n] + '"' : "") + ">" + inline(c) + "</th>";
        });
        html += "</tr></thead><tbody>";
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const row = cells(lines[i++]);
          html += "<tr>";
          for (let n = 0; n < head.length; n++) {
            html += "<td" + (aligns[n] ? ' style="text-align:' + aligns[n] + '"' : "") + ">" + inline(row[n] || "") + "</td>";
          }
          html += "</tr>";
        }
        out.push(html + "</tbody></table></div>");
        continue;
      }

      if (/^\s{0,3}>/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
        out.push("<blockquote>" + render(quoted.join("\n")) + "</blockquote>");
        continue;
      }

      if (ITEM.test(line)) {
        const items = [];
        while (i < lines.length) {
          const item = lines[i].match(ITEM);
          if (item) {
            items.push({ indent: item[1].replace(/\t/g, "    ").length, ordered: /\d/.test(item[2]), text: item[3] });
            i++;
          } else if (lines[i].trim() && /^\s+/.test(lines[i]) && !FENCE.test(lines[i])) {
            items[items.length - 1].text += "\n" + lines[i].trim(); // a wrapped line of the item above
            i++;
          } else if (!lines[i].trim() && i + 1 < lines.length && ITEM.test(lines[i + 1])) {
            i++; // a blank line between two items of the same list
          } else {
            break;
          }
        }
        out.push(listHtml(items));
        continue;
      }

      const para = [];
      while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i]) &&
             !RULE.test(lines[i]) && !ITEM.test(lines[i]) && !/^\s{0,3}>/.test(lines[i]) &&
             !(lines[i].includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-"))) {
        para.push(lines[i++].trim());
      }
      if (!para.length) para.push(lines[i++].trim()); // never loop on a line nothing else took
      out.push("<p>" + inline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
    }
    return out.join("\n");
  }

  window.PQMarkdown = { render: render, escapeHtml: escapeHtml };
})();
