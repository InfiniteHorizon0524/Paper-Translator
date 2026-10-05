import math
import re
from collections import Counter
from urllib.parse import urlparse, unquote

import pymupdf
from fastapi import HTTPException


MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500


def normalize_arxiv(value):
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        if url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise HTTPException(400, "请输入 arXiv 编号或 arxiv.org 链接。")
        value = unquote(url.path)
        if not value.startswith(("/abs/", "/pdf/")):
            raise HTTPException(400, "请使用 arXiv 的 abs 或 pdf 链接。")
        value = value[5:]
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    value = re.sub(r"\.pdf$", "", value)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]+/\d{7})(?:v[1-9]\d*)?", value):
        raise HTTPException(400, "arXiv 编号格式不正确，例如 1706.03762 或 hep-th/9901001。")
    return value


def ordered_blocks(blocks, width):
    # Keep common two-column academic layouts in column order.
    mid = width / 2
    left = [b for b in blocks if b[2] <= mid + 18]
    right = [b for b in blocks if b[0] >= mid - 18 and b not in left]
    spanning = [b for b in blocks if b not in left and b not in right]
    if len(left) >= 3 and len(right) >= 3:
        output, floor = [], -1
        for wide in sorted(spanning, key=lambda b: b[1]):
            for column in (left, right):
                output.extend(b for b in sorted(column, key=lambda b: b[1]) if floor <= b[1] < wide[1])
            output.append(wide)
            floor = wide[1]
        for column in (left, right):
            output.extend(b for b in sorted(column, key=lambda b: b[1]) if b[1] >= floor)
        return output
    return sorted(blocks, key=lambda b: (b[1], b[0]))


def page_text(page):
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    text = "\n\n".join(b[4] for b in ordered_blocks(blocks, page.rect.width))
    return re.sub(r"(\w)-\n(?=[a-z])", r"\1", text).strip()


READING_LAYOUT_VERSION = 3
CAPTION = re.compile(
    r"^(?:Figure|Fig\.)[ \t]*(\d+[a-z]?)(?:[ \t]*[:.]|[ \t]*\n|"
    r"[ \t]+(?![ \t]*(?:compares?|shows?|illustrates?|depicts?|presents?|summari[sz]es?)\b))", re.I,
)
TABLE_CAPTION = re.compile(
    r"^(?:Table|Tab\.)[ \t]*(?:\d+[a-z]?|[IVX]+)(?:[ \t]*[:.]|[ \t]*\n|"
    r"[ \t]+(?![ \t]*(?:compares?|shows?|lists?|presents?|summari[sz]es?)\b))", re.I,
)


def reading_block_kind(text):
    """Keep floating material out of the prose used for sentence context."""
    if re.fullmatch(r"\[\[PR_FIGURE_\d+\]\]", text.strip()):
        return "figure"
    if text.startswith("##"):
        return "heading"
    if CAPTION.match(text) or TABLE_CAPTION.match(text):
        return "caption"
    if re.match(r"\[\^[^\]]+\]:", text):
        return "footnote"
    if re.fullmatch(r"\s*(?:\d+|[ivx]+)\s*", text, re.I):
        return "furniture"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Extracted tables often have one cell per line, with no pipe delimiters.
    if (text.lstrip().startswith("|") or (len(lines) >= 4
            and sum(len(line.split()) <= 4 for line in lines) / len(lines) >= .8
            and sum(bool(re.search(r"\d", line)) for line in lines) >= 2)):
        return "table"
    return "body"


def page_layout(page):
    """Prepare reading/translation text while keeping figures in the original PDF.

    Figure coordinates are normalized to the page. Caption-anchored vector
    regions also cover disconnected axes/labels; standalone raster images need
    no caption. Footnote recognition is intentionally conservative.
    """
    bounds = page.rect * page.derotation_matrix
    width, height = bounds.width, bounds.height
    raw = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    blocks = []
    sizes = Counter()
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        lines = []
        spans = []
        for line in block["lines"]:
            lines.append("".join(s["text"] for s in line["spans"]))
            spans.extend(line["spans"])
        text = "\n".join(lines).strip()
        if not text:
            continue
        for span in spans:
            sizes[round(span["size"], 1)] += len(span["text"].strip())
        blocks.append({"bbox": pymupdf.Rect(block["bbox"]), "text": text, "spans": spans, "lines": block["lines"]})
    body_size = sizes.most_common(1)[0][0] if sizes else 11
    two_columns = (sum(b["bbox"].x1 <= width / 2 + 18 and len(b["text"]) > 100 for b in blocks) >= 3
                   and sum(b["bbox"].x0 >= width / 2 - 18 and len(b["text"]) > 100 for b in blocks) >= 3)
    drawings = page.get_drawings()
    raster = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()
              if pymupdf.Rect(info["bbox"]).width >= 36 and pymupdf.Rect(info["bbox"]).height >= 30]
    figures = []
    captions = [b for b in blocks if CAPTION.match(b["text"])]
    for caption in sorted(captions, key=lambda b: (b["bbox"].y0, b["bbox"].x0)):
        cap = caption["bbox"]
        left, right = (0, width) if not two_columns or cap.width > width * .55 else ((0, width / 2) if cap.x0 < width / 2 else (width / 2, width))
        previous = [b["bbox"].y1 for b in blocks if b is not caption and b["bbox"].y1 < cap.y0
                    and b["bbox"].x0 < right and b["bbox"].x1 > left
                    and (len(b["text"]) > 100 or CAPTION.match(b["text"]))]
        floor = max(previous, default=35)
        candidates = raster + [pymupdf.Rect(d["rect"]) for d in drawings]
        region = pymupdf.Rect()
        for rect in candidates:
            if rect.x0 >= left - 10 and rect.x1 <= right + 10 and rect.y0 >= floor - 2 and rect.y1 <= cap.y0 + 2 and rect.height > 1:
                region |= rect
        # Captions may accompany diagrams consisting only of font glyphs.
        if region.is_empty:
            region = pymupdf.Rect(max(left + 20, cap.x0), floor + 8, min(right - 20, cap.x1), cap.y0 - 6)
        if region.is_empty or region.height < 25:
            continue
        for block in blocks:
            rect = block["bbox"]
            if block is not caption and rect.x0 >= region.x0 - 35 and rect.x1 <= region.x1 + 35 and rect.y0 >= max(floor, region.y0 - 25) and rect.y1 < cap.y0 and len(block["text"]) <= 100:
                region |= rect
        figures.append({"rect": region, "caption": caption, "label": CAPTION.match(caption["text"]).group(1)})
    for rect in raster:
        if not any((rect & f["rect"]).get_area() > rect.get_area() * .5 for f in figures):
            figures.append({"rect": rect, "caption": None, "label": ""})
    figures.sort(key=lambda f: (f["rect"].y0, f["rect"].x0))
    for index, figure in enumerate(figures, 1):
        figure["id"] = str(index)

    # A short horizontal rule is strong evidence of a page footnote separator.
    rules = []
    for drawing in drawings:
        rect = pymupdf.Rect(drawing["rect"])
        if rect.y0 > height * .6 and rect.height < 2 and 30 < rect.width < width * .55:
            rules.append(rect)
    footnotes, content = [], []
    last_note_rect = None
    note_start = re.compile(r"^([*∗†‡])\s*|^([0-9]{1,2})(?:[.)]?\s+|(?=[A-Z]))")
    for index, block in enumerate(blocks):
        # PDF text blocks can combine a diagram label with nearby prose. Filter
        # individual lines so labels cannot leak through a mostly-body block.
        if not CAPTION.match(block["text"]):
            kept = []
            for line in block["lines"]:
                line_rect = pymupdf.Rect(line["bbox"])
                if not any(line_rect.get_area() and (line_rect & f["rect"]).get_area() / line_rect.get_area() > .5 for f in figures):
                    kept.append(line)
            if not kept:
                continue
            if len(kept) != len(block["lines"]):
                block = dict(block, lines=kept,
                             text="\n".join("".join(s["text"] for s in line["spans"]) for line in kept),
                             spans=[s for line in kept for s in line["spans"]])
                rect = pymupdf.Rect()
                for line in kept:
                    rect |= pymupdf.Rect(line["bbox"])
                block["bbox"] = rect
        rect, text, spans = block["bbox"], block["text"], block["spans"]
        # Text inside a figure is part of the image, never translation input.
        if any(rect.get_area() and (rect & f["rect"]).get_area() / rect.get_area() > .7 for f in figures):
            continue
        small = max((s["size"] for s in spans), default=body_size) < body_size * .94
        symbol = note_start.match(text)
        after_rule = any(rect.y0 >= rule.y0 - 2 and rect.y0 - rule.y0 < 130 and rect.x0 < rule.x1 and rect.x1 > rule.x0 for rule in rules)
        close_to_note = (any(0 <= rect.y0 - rule.y0 <= 20 for rule in rules)
                         or (last_note_rect is not None and -body_size < rect.y0 - last_note_rect.y1 < body_size * 1.5))
        if small and rect.y0 > height * .6 and (symbol or (after_rule and close_to_note)) and len(text) > 12:
            for line in text.splitlines():
                start = note_start.match(line.strip())
                if start:
                    footnotes.append((start.group(1) or start.group(2), line.strip()[start.end():]))
                elif footnotes:
                    key, previous_text = footnotes[-1]
                    footnotes[-1] = (key, previous_text + "\n" + line)
                else:
                    footnotes.append(("note1", line))
            last_note_rect = rect
            continue
        content.append((*rect, index, block))
    note_keys = {key for key, _ in footnotes}
    output = []
    for item in content:
        block = item[5]
        spans = [s for s in block["spans"] if s["text"].strip()]
        short = len(block["text"]) < 160 and all(abs(line["dir"][1]) < .1 for line in block["lines"])
        bold = spans and all(s["flags"] & 16 for s in spans)
        is_heading = short and not CAPTION.match(block["text"]) and not TABLE_CAPTION.match(block["text"]) and (
            (bold and (re.match(r"^(?:\d+(?:\.\d+)*\.?\s+|Abstract\b|References\b|Conclusions?\b)", block["text"], re.I) or min(s["size"] for s in spans) > body_size * 1.1))
            or (len(block["text"]) < 100 and spans and min(s["size"] for s in spans) > body_size * 1.5)
        )
        if is_heading:
            heading = " ".join(block["text"].split())
            level = "###" if re.match(r"^\d+\.\d", heading) else "##"
            output.append((*block["bbox"], item[4], f"{level} {heading}"))
            continue
        lines = []
        for line in block["lines"]:
            def span_text(span):
                key = span["text"].strip()
                if span["flags"] & 1:
                    if key in note_keys:
                        return f"[^{key}]"
                    if key and all(c in note_keys for c in key) and not key.isdigit():
                        return "".join(f"[^{c}]" for c in key)
                return span["text"]
            stripped = "".join(span_text(s) for s in line["spans"]).strip()
            lines.append(stripped)
        text = "\n".join(lines)
        output.append((*block["bbox"], item[4], text))
    for figure in figures:
        rect = figure["rect"]
        # Anchor at the caption so the placeholder precedes its explanation.
        anchor = figure["caption"]["bbox"] if figure["caption"] else rect
        output.append((rect.x0, anchor.y0 - .1, rect.x1, anchor.y0, -1, f"[[PR_FIGURE_{figure['id']}]]"))
    segments = [{"text": re.sub(r"(\w)-\n(?=[a-z])", r"\1", item[5]),
                 "kind": reading_block_kind(item[5]),
                 "bbox": [round(item[0] / width, 6), round(item[1] / height, 6),
                          round(item[2] / width, 6), round(item[3] / height, 6)]}
                for item in ordered_blocks(output, width)]
    if footnotes:
        segments.extend({"text": f"[^{key}]: " + value.replace("\n", "\n    "), "kind": "footnote"}
                        for key, value in footnotes)
    text = "\n\n".join(segment["text"] for segment in segments)
    positions = []
    for figure in figures:
        rect = (figure["rect"] * page.rotation_matrix) & page.rect
        positions.append({"id": figure["id"], "label": figure["label"], "bbox": [
            round(v, 6) for v in (rect.x0 / page.rect.width, rect.y0 / page.rect.height,
                                 rect.x1 / page.rect.width, rect.y1 / page.rect.height)]})
    return {"text": text, "figures": positions, "segments": segments}


def reading_layout(document):
    pages = [page_layout(page) for page in document]
    edge_counts = Counter()
    for page in pages:
        edge_counts.update({segment["text"] for segment in page["segments"]
                            if "bbox" in segment and (segment["bbox"][3] < .1 or segment["bbox"][1] > .9)
                            and len(segment["text"]) < 160})
    for page in pages:
        for segment in page["segments"]:
            if (segment["kind"] == "body" and edge_counts[segment["text"]] >= 2
                    and "bbox" in segment and (segment["bbox"][3] < .1 or segment["bbox"][1] > .9)):
                segment["kind"] = "furniture"
    return {"version": READING_LAYOUT_VERSION, "pages": pages}


def translation_with_figures(content, layout):
    """Adapt old translations of opening figures without altering saved text.

    When the extracted page starts with a figure, everything before its matching
    translated caption belongs to that figure. Replacing that prefix is safe;
    middle-of-page content is left intact because prose cannot be located from
    a translated caption alone. New translations already contain the marker.
    """
    opening = re.match(r"\s*\[\[PR_FIGURE_(\d+)\]\]", layout.get("text", ""))
    if not opening:
        return content
    figure = next((f for f in layout.get("figures", []) if f["id"] == opening.group(1)), None)
    if not figure or not figure.get("label"):
        return content
    marker = f"[[PR_FIGURE_{figure['id']}]]"
    caption = re.search(
        rf"^[ \t]*(?:\#{{1,6}}[ \t]+)?(?:\*\*)?(?:图|Figure|Fig\.)[ \t]*{re.escape(figure['label'])}(?=[ \t:：.。])",
        content, re.M | re.I,
    )
    if not caption:
        return content
    # Early versions could retain labels on either side of the marker. All text
    # before this caption belongs to the opening figure, regardless of markers.
    return marker + "\n\n" + content[caption.start():].lstrip().replace(marker, "", 1)


def extract_pdf(data, filename):
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "PDF 不能超过 50 MB。")
    if not data.lstrip().startswith(b"%PDF-"):
        raise HTTPException(400, "文件不是有效的 PDF。")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.is_encrypted:
                raise HTTPException(400, "该 PDF 有密码保护，请先解密后上传。")
            if not 0 < len(doc) <= MAX_PAGES:
                raise HTTPException(400, "支持 1–500 页的 PDF。")
            pages = [page_text(page) for page in doc]
            metadata = doc.metadata or {}
            title = (metadata.get("title") or "").strip()
            if not title or title.lower() in {"untitled", "microsoft word"}:
                title = next((s.strip() for s in pages[0].splitlines() if len(s.strip()) > 12), filename.removesuffix(".pdf"))[:200]
            if sum(len(p.strip()) for p in pages) < 40:
                raise HTTPException(422, "未找到可提取文字。这可能是扫描版 PDF；请上传带文字层的版本。")
            return title, pages, {"author": metadata.get("author", ""), "filename": filename, "empty_pages": [i + 1 for i, t in enumerate(pages) if not t.strip()], "reading_layout": reading_layout(doc)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "PDF 无法解析，文件可能已损坏。") from None


def split_text(text, limit=4500):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text.strip():
        chunks.append(text)
    return chunks


def tokens(text):
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())


def retrieve(pages, query, max_chars=24000):
    chunks = []
    for index, text in enumerate(pages):
        for part in split_text(text, 1600):
            chunks.append({"page": index + 1, "text": part, "terms": Counter(tokens(part))})
    if not chunks:
        return []
    terms = set(tokens(query))
    avg_length = sum(sum(c["terms"].values()) for c in chunks) / len(chunks) or 1
    df = {t: sum(t in c["terms"] for c in chunks) for t in terms}
    for c in chunks:
        length = sum(c["terms"].values())
        c["score"] = sum(math.log(1 + (len(chunks) - df[t] + 0.5) / (df[t] + 0.5)) * (c["terms"][t] * 2.2 / (c["terms"][t] + 1.2 * (0.25 + 0.75 * length / avg_length))) for t in terms if c["terms"][t])
    ranked = sorted(chunks, key=lambda c: c["score"], reverse=True)
    # Include the opening and ending for broad paper-level questions.
    candidates = [chunks[0], chunks[-1]] + ranked
    selected, seen, used = [], set(), 0
    for c in candidates:
        key = (c["page"], c["text"])
        if key in seen or used + len(c["text"]) > max_chars:
            continue
        seen.add(key)
        selected.append({"page": c["page"], "text": c["text"]})
        used += len(c["text"])
    return sorted(selected, key=lambda c: c["page"])
