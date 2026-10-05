"use strict";

// Protect code and TeX before processing Markdown: &, <, *, blank lines and
// backticks inside a formula belong to TeX, not to the surrounding paragraph.
window.PaperReaderRichText = (() => {
  const escape = text => String(text).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const delimiters = [
    {open:"$$", close:"$$", display:true},
    {open:"\\[", close:"\\]", display:true},
    {open:"\\(", close:"\\)", display:false},
    {open:"$", close:"$", display:false},
  ];
  function escapedAt(text, index) {
    let count = 0;
    while (index > 0 && text[--index] === "\\") count++;
    return count % 2 === 1;
  }
  function mathEnd(text, start, delimiter) {
    let depth = 0;
    for (let i = start; i < text.length; i++) {
      if (delimiter.open === "$" && text[i] === "\n") return -1;
      if (escapedAt(text, i)) continue;
      if (text[i] === "{") depth++;
      if (text[i] === "}") depth = Math.max(0, depth - 1);
      if (delimiter.open === "$" && !depth && text[i] === "`") return -1;
      if (!depth && text.startsWith(delimiter.close, i)) {
        // Avoid treating prices such as "$5 and $10" as inline math.
        if (delimiter.open === "$" && /\d/.test(text[i + 1] || "")) return -1;
        if (delimiter.open === "$" && (/\s/.test(text[i - 1]) || text[i + 1] === "$")) continue;
        return i;
      }
    }
    return -1;
  }
  function renderMath(source, tex, display) {
    let content;
    try {
      content = window.katex.renderToString(tex, {
        displayMode:display, throwOnError:true, trust:false, strict:"ignore",
        maxExpand:1000, maxSize:20, output:"htmlAndMathml",
      });
    } catch {
      // Unsupported or broken formulas stay readable and cannot interrupt a stream.
      content = `<code class="math-source" title="公式暂无法排版，保留 LaTeX 原文">${escape(source)}</code>`;
    }
    return display ? `<div class="math-block">${content}</div>` : `<span class="math-inline">${content}</span>`;
  }
  function tableCells(line) {
    const row = line.trim(), cells = [];
    let start = 0, pipes = 0;
    for (let i = 0; i < row.length; i++) {
      if (row[i] === "|" && !escapedAt(row, i)) {
        cells.push(row.slice(start, i).trim()); start = i + 1; pipes++;
      }
    }
    cells.push(row.slice(start).trim());
    if (row.startsWith("|")) cells.shift();
    if (row.endsWith("|") && !escapedAt(row, row.length - 1)) cells.pop();
    return {cells, pipes};
  }
  function markdown(input, citationPages = 0, context = {}) {
    const text = String(input).replace(/\r\n?/g, "\n").replace(/\u0000/g, "");
    const protectedParts = [];
    const protectedTypes = [];
    const protect = (html, block = false, type = "") => {
      protectedTypes.push(type);
      const token = `\u0000${protectedParts.push(html) - 1}\u0000`;
      return block ? `\n\n${token}\n\n` : token;
    };
    let safe = "", plain = "";
    const page = Number.isInteger(context.page) && context.page > 0 ? context.page : 0;
    const figures = page && Array.isArray(context.figures) ? context.figures.filter(f => /^\d+$/.test(f.id)) : [];
    const shownFigures = new Set();
    const figureHTML = figure => `<button type="button" class="figure-placeholder" data-figure="${escape(figure.id)}" data-figure-page="${page}" aria-label="查看${escape(figure.label ? `图 ${figure.label}` : '图片')}，原文第 ${page} 页"><span class="figure-placeholder-icon" aria-hidden="true">▧</span><strong>${escape(figure.label ? `图 ${figure.label}` : '原文图片')}</strong><small>点击查看原文对应位置 ↗</small></button>`;
    const flush = () => { safe += escape(plain); plain = ""; };
    for (let i = 0; i < text.length;) {
      if (text.startsWith("```", i)) {
        const end = text.indexOf("```", i + 3);
        const bodyStart = text.indexOf("\n", i + 3);
        const stop = end < 0 ? text.length : end;
        flush();
        safe += protect(`<pre><code>${escape(bodyStart >= 0 && bodyStart < stop ? text.slice(bodyStart + 1, stop) : text.slice(i + 3, stop))}</code></pre>`, true, "code");
        i = end < 0 ? text.length : end + 3; continue;
      }
      if (text[i] === "`") {
        const end = text.indexOf("`", i + 1);
        if (end >= 0 && !text.slice(i + 1, end).includes("\n")) {
          flush(); safe += protect(`<code>${escape(text.slice(i + 1, end))}</code>`, false, "code");
          i = end + 1; continue;
        }
      }
      const figureMarker = text.slice(i).match(/^\[\[PR_FIGURE_(\d+)\]\]/);
      const figure = figureMarker && figures.find(f => f.id === figureMarker[1]);
      if (figure) {
        flush();
        if (!shownFigures.has(figure.id)) safe += protect(figureHTML(figure), true, "figure");
        shownFigures.add(figure.id); i += figureMarker[0].length; continue;
      }
      if (text.startsWith("\\$", i) && !escapedAt(text, i)) {
        plain += "$"; i += 2; continue;
      }
      const delimiter = delimiters.find(d => text.startsWith(d.open, i) && !escapedAt(text, i));
      if (delimiter && !(delimiter.open === "$" && /\s/.test(text[i + 1] || " "))) {
        const start = i + delimiter.open.length, end = mathEnd(text, start, delimiter);
        if (end >= 0) {
          const stop = end + delimiter.close.length;
          const linePrefix = text.slice(text.lastIndexOf("\n", i - 1) + 1, i);
          const inNote = /^\s{0,3}\[\^[^\]\s]+\]:/.test(linePrefix) || /^(?: {4}|\t)/.test(linePrefix);
          flush(); safe += protect(renderMath(text.slice(i, stop), text.slice(start, end), delimiter.display), delimiter.display && !inNote);
          i = stop; continue;
        }
      }
      plain += text[i++];
    }
    flush();
    // Older cached translations have no markers. Use the translated caption
    // when available; retain a link for uncaptained figures as well.
    for (const figure of figures) {
      if (shownFigures.has(figure.id)) continue;
      const token = protect(figureHTML(figure), true, "figure");
      const label = String(figure.label || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const caption = label && new RegExp(`^(?:图|Figure|Fig\\.)\\s*${label}(?=[\\s:：.。]|$)`, "im");
      const match = caption && caption.exec(safe);
      safe = match ? safe.slice(0, match.index) + token + safe.slice(match.index) : safe + token;
    }
    const notes = new Map(), mainLines = [];
    const sourceLines = safe.split("\n");
    for (let i = 0; i < sourceLines.length; i++) {
      const definition = sourceLines[i].match(/^\s{0,3}\[\^([^\]\s]+)\]:\s*(.*)$/);
      if (!definition) { mainLines.push(sourceLines[i]); continue; }
      let body = definition[2];
      while (i + 1 < sourceLines.length && /^(?: {4}|\t)\S?/.test(sourceLines[i + 1])) body += "\n" + sourceLines[++i].replace(/^(?: {4}|\t)/, "");
      const key = definition[1];
      notes.set(key, notes.has(key) ? notes.get(key) + "\n" + body : body);
    }
    safe = mainLines.join("\n");
    const prefix = `pr-note-${page || 'chat'}-`;
    const noteID = key => prefix + Array.from(key).map(c => c.codePointAt(0).toString(16)).join("-");
    const references = new Map();
    const restore = (part, tableCell = false) => part.replace(/\u0000(\d+)\u0000/g, (_, i) => {
      const html = protectedParts[Number(i)] || "";
      return tableCell && protectedTypes[Number(i)] === "code" ? html.replace(/\\\|/g, "|") : html;
    });
    const inline = part => {
      part = part.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      part = part.replace(/\[\^([^\]\s]+)\]/g, (match, key) => {
        if (!notes.has(key)) return match;
        const count = (references.get(key) || 0) + 1; references.set(key, count);
        const id = noteID(key);
        return `<sup class="footnote-ref" id="${id}-ref-${count}"><a href="#${id}" role="doc-noteref" aria-label="脚注 ${escape(key)}">${escape(key)}</a></sup>`;
      });
      if (citationPages) part = part.replace(/\[第\s*(\d+)\s*页\]/g, (match, page) => Number(page) >= 1 && Number(page) <= citationPages ? `<button class="citation" data-cite="${Number(page)}">第 ${Number(page)} 页 ↗</button>` : match);
      return part;
    };
    // Detect tables before inline formatting so pipes cannot split generated HTML.
    const lines = safe.split("\n"), blocks = [];
    const blockStart = line => /^\s*(?:#{1,6} |[-*+] |\d+[.)] |&gt;|\u0000\d+\u0000\s*$)/.test(line);
    for (let i = 0; i < lines.length;) {
      const header = tableCells(lines[i]), delimiter = tableCells(lines[i + 1] || "");
      if (!blockStart(lines[i]) && header.cells.length && (header.pipes || delimiter.pipes) && header.cells.length === delimiter.cells.length && delimiter.cells.every(cell => /^:?-+:?$/.test(cell))) {
        const alignment = delimiter.cells.map(cell => cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : "left");
        const rowHTML = (cells, tag) => `<tr>${header.cells.map((_, index) => {
          const cell = (cells[index] || "").replace(/\\\|/g, "|").replace(/&lt;br\s*\/?&gt;/gi, "<br>");
          return `<${tag}${tag === "th" ? ' scope="col"' : ""} class="table-align-${alignment[index]}">${restore(inline(cell), true)}</${tag}>`;
        }).join("")}</tr>`;
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && !blockStart(lines[i])) {
          // A second header/delimiter pair starts a new table even without a blank line.
          const next = tableCells(lines[i + 1] || "");
          if (next.pipes && next.cells.every(cell => /^:?-+:?$/.test(cell))) break;
          rows.push(rowHTML(tableCells(lines[i]).cells, "td")); i++;
        }
        const html = `<div class="table-scroll" role="region" aria-label="表格，可横向滚动" tabindex="0"><table class="markdown-table"><thead>${rowHTML(header.cells, "th")}</thead>${rows.length ? `<tbody>${rows.join("")}</tbody>` : ""}</table></div>`;
        blocks.push(protect(html, true));
      } else {
        const line = lines[i++];
        blocks.push(/^#{1,6} /.test(line) ? `\n\n${line}\n\n` : line);
      }
    }
    safe = inline(blocks.join("\n"));
    const rendered = [], paragraph = [], list = [];
    const flushParagraph = () => {
      if (paragraph.length) {
        const body = paragraph.join("\n"), caption = /^(?:图\s*\d|Figure\s*\d|Fig\.\s*\d)/i.test(body);
        rendered.push(`<p${caption ? ' class="figure-caption"' : ''}>${body}</p>`); paragraph.length = 0;
      }
    };
    const flushList = () => { if (list.length) { rendered.push(`<ul>${list.map(line => `<li>${line}</li>`).join("")}</ul>`); list.length = 0; } };
    for (const line of safe.split("\n")) {
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
      if (!line.trim() || heading || /^\u0000\d+\u0000$/.test(line.trim())) {
        flushParagraph(); flushList();
        if (heading) rendered.push(`<h${heading[1].length}>${heading[2]}</h${heading[1].length}>`);
        else if (line.trim()) rendered.push(line.trim());
      } else if (/^\s*[-*+] /.test(line)) {
        flushParagraph(); list.push(line.replace(/^\s*[-*+] /, ""));
      } else {
        flushList(); paragraph.push(line);
      }
    }
    flushParagraph(); flushList();
    const footnotes = notes.size ? `<footer class="page-footnotes" role="doc-endnotes" aria-label="本页脚注"><hr>${[...notes].map(([key, body]) => {
      const id = noteID(key), count = references.get(key) || 0;
      return `<div class="footnote-item" id="${id}"><span class="footnote-label">${escape(key)}</span><div class="footnote-content">${inline(body)}${count ? ` <a class="footnote-back" href="#${id}-ref-1" aria-label="返回脚注 ${escape(key)} 的正文角标">↩</a>` : ''}</div></div>`;
    }).join("")}</footer>` : "";
    return restore(rendered.join("") + footnotes);
  }
  return {markdown};
})();
