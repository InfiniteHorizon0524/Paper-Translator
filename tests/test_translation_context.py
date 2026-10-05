import json
import re

import pymupdf
import pytest

from paperreader import app as app_module, storage
from paperreader.documents import reading_block_kind, reading_layout
from paperreader.translation import TranslationContext, sentence_ends, translation_chunks
from test_app import CONFIG, client, events


def layout(*blocks):
    segments = [{"kind": kind, "text": text} for kind, text in blocks]
    return {"text": "\n\n".join(s["text"] for s in segments), "segments": segments, "figures": []}


def whole(page):
    return translation_chunks(page["text"], limit=100000)[0]


def prompt_data(messages):
    user = messages[1]["content"]
    context = json.loads(re.search(r"<translation_context>\n(.*?)\n</translation_context>", user, re.S)[1])
    source = re.search(r"<source>\n(.*?)\n</source>", user, re.S)[1]
    return context, source


def interrupted_pages():
    return [layout(("body", "Earlier work is limited. Our method learns a representation that"),
                   ("footnote", "[^1]: A note that is not a continuation."), ("furniture", "1")),
            layout(("figure", "[[PR_FIGURE_1]]"), ("caption", "Figure 1: Model architecture."),
                   ("caption", "Table 2: Comparison of the baselines."),
                   ("table", "Model\nAccuracy\nBase\n80\nOurs\n92"),
                   ("body", "preserves semantic relationships. The next sentence stays on page two."))]


def test_cross_page_sentence_bypasses_figures_tables_notes_and_page_numbers():
    pages = interrupted_pages()
    builder = TranslationContext(pages)
    first = builder.for_chunk(1, whole(pages[0]))
    second = builder.for_chunk(2, whole(pages[1]), "已有研究存在局限。我们的方法学习一种表示，该表示")
    expected = "Our method learns a representation that preserves semantic relationships."
    assert expected in first["boundary_sentences"]
    assert expected in second["boundary_sentences"]
    assert first["following_body"] == [{"page": 2, "text": pages[1]["segments"][-1]["text"]}]
    assert "该表示" in second["previous_translation"]
    assert all("Figure" not in sentence and "Table" not in sentence and "[^" not in sentence
               for sentence in first["boundary_sentences"])


def test_sentence_can_cross_a_page_containing_only_floating_material():
    pages = [layout(("body", "The approach produces results that")),
             layout(("caption", "Figure 1: Results."), ("table", "| Method | Score |\n| --- | --- |\n| A | 42 |")),
             layout(("body", "generalize to unseen tasks."))]
    builder = TranslationContext(pages)
    for number in (1, 3):
        assert "The approach produces results that generalize to unseen tasks." in builder.for_chunk(number, whole(pages[number - 1]))["boundary_sentences"]


def test_in_page_float_does_not_break_the_reconstructed_sentence():
    pages = [layout(("body", "We optimize a loss that"), ("figure", "[[PR_FIGURE_1]]"),
                    ("caption", "Figure 3: A loss curve."), ("body", "penalizes inconsistent predictions."))]
    context = TranslationContext(pages).for_chunk(1, whole(pages[0]))
    assert context["boundary_sentences"] == ["We optimize a loss that penalizes inconsistent predictions."]


def test_section_heading_is_a_hard_context_boundary():
    pages = [layout(("body", "An incomplete discussion that")),
             layout(("heading", "## 2 Results"), ("body", "New results are reported."))]
    builder = TranslationContext(pages)
    assert builder.for_chunk(1, whole(pages[0]))["following_body"] == []
    assert builder.for_chunk(2, whole(pages[1]))["boundary_sentences"] == []


def test_page_hyphenation_and_abbreviations_are_preserved_in_context():
    pages = [layout(("body", "We compare methods, e.g. the cross-")),
             layout(("body", "validation procedure described in Fig. 2. Other findings follow."))]
    context = TranslationContext(pages).for_chunk(1, whole(pages[0]))
    assert context["boundary_sentences"] == ["We compare methods, e.g. the crossvalidation procedure described in Fig. 2."]
    example = "e.g. a score of 1.5 is used. Another sentence."
    assert [example[:end] for end in sentence_ends(example)] == [
        "e.g. a score of 1.5 is used.", example,
    ]


def test_context_is_bounded_and_keeps_nearest_prose():
    pages = [layout(("body", "old " * 1000 + "nearest previous words that")),
             layout(("body", "continue here")), layout(("body", "nearest following words " + "new " * 1000))]
    context = TranslationContext(pages, context_limit=120).for_chunk(2, whole(pages[1]))
    assert sum(len(s["text"]) for s in context["preceding_body"]) <= 120
    assert sum(len(s["text"]) for s in context["following_body"]) <= 120
    assert context["preceding_body"][-1]["text"].endswith("nearest previous words that")
    assert context["following_body"][0]["text"].startswith("nearest following words")


def test_chunks_prefer_whole_sentences_and_cover_source_exactly():
    text = "First sentence is complete. " * 8 + "A longer final sentence stays together across the soft limit."
    chunks = translation_chunks(text, limit=80)
    assert len(chunks) > 2
    assert "".join(c.text for c in chunks) == text
    assert all(c.text.strip().endswith(".") for c in chunks)
    pages = [layout(("body", text))]
    context = TranslationContext(pages).for_chunk(1, chunks[1], "前一片段的中文译文。")
    assert context["preceding_body"] and context["following_body"]
    assert context["previous_translation"] == "前一片段的中文译文。"


def test_long_sentence_table_and_display_math_are_atomic():
    sentence = "An exceptionally long sentence " + "with details " * 30 + "ends here."
    table = "| Method | Score |\n| --- | --- |\n" + "| Ours | 92 |\n" * 20
    formula = "$$\n" + r"\begin{aligned} x &= y \\" * 20 + "\n$$"
    text = "\n\n".join([sentence, table, formula])
    chunks = translation_chunks(text, limit=100)
    assert [c.text for c in chunks] == [sentence, table.rstrip(), formula]
    assert all(text[c.start:c.end] == c.text for c in chunks)


@pytest.mark.parametrize("text,kind", [
    ("Table 2: Accuracy across tasks.", "caption"), ("Table 2 shows the results.", "body"),
    ("Figure 3 compares all models.", "body"), ("42", "furniture"),
    ("Method\nAccuracy\nBaseline\n80\nOurs\n92", "table"),
    ("[^1]: A footnote.", "footnote"),
])
def test_material_classification(text, kind):
    assert reading_block_kind(text) == kind


def cross_page_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 65), "Continuity Research Paper", fontsize=18, fontname="tibo")
        page.insert_text((50, 740), "Our method learns a representation that", fontsize=12)
        page.insert_text((280, 805), "1", fontsize=9)
        page = doc.new_page()
        page.draw_rect((60, 90, 340, 190), color=(0, 0, 0))
        page.insert_text((60, 220), "Figure 1: An unrelated model illustration.", fontsize=11)
        page.insert_text((50, 300), "preserves semantic relationships. The next result is separate.", fontsize=12)
        page.insert_text((280, 805), "2", fontsize=9)
        return doc.tobytes()


def test_pdf_layout_exposes_structural_segments_and_stitches_prose():
    with pymupdf.open(stream=cross_page_pdf(), filetype="pdf") as doc:
        pages = reading_layout(doc)["pages"]
    assert "[[PR_FIGURE_1]]" in pages[1]["text"]
    assert any(s["kind"] == "caption" for s in pages[1]["segments"])
    context = TranslationContext(pages).for_chunk(1, whole(pages[0]))
    assert "Our method learns a representation that preserves semantic relationships." in context["boundary_sentences"]


def test_single_page_translation_has_both_sides_without_translating_other_pages(client, monkeypatch):
    paper = client.post("/api/papers/upload", files={"file": ("continuity.pdf", cross_page_pdf(), "application/pdf")}).json()
    calls = []
    async def complete(config, messages):
        calls.append(messages)
        yield "保持语义关系。下一项结果是独立的。"
    monkeypatch.setattr(app_module, "completion", complete)
    parsed = events(client.post(f"/api/papers/{paper['id']}/translate", json={"config": CONFIG, "pages": [2]}))
    assert parsed[-1][0] == "done" and len(calls) == 1
    context, source = prompt_data(calls[0])
    assert "Our method learns a representation that preserves semantic relationships." in context["boundary_sentences"]
    assert "Our method learns" not in source and "Figure 1:" in source
    assert set(storage.translations(paper["id"])) == {"2"}


def test_full_translation_passes_newly_saved_tail_and_preserves_cache(client, monkeypatch):
    paper = client.post("/api/papers/upload", files={"file": ("continuity.pdf", cross_page_pdf(), "application/pdf")}).json()
    calls = []
    async def complete(config, messages):
        context, source = prompt_data(messages)
        calls.append((context, source))
        yield "我们的方法学习一种表示，该表示" if "Our method learns" in source else "保持语义关系。"
    monkeypatch.setattr(app_module, "completion", complete)
    url = f"/api/papers/{paper['id']}/translate"
    parsed = events(client.post(url, json={"config": CONFIG, "pages": [1, 2]}))
    assert parsed[-1][0] == "done"
    assert calls[1][0]["previous_translation"] == "我们的方法学习一种表示，该表示"
    saved = storage.translations(paper["id"])
    assert saved == {"1": "我们的方法学习一种表示，该表示", "2": "保持语义关系。"}
    events(client.post(url, json={"config": CONFIG, "pages": [1, 2]}))
    assert len(calls) == 2
    events(client.post(url, json={"config": CONFIG, "pages": [2], "force": True}))
    assert len(calls) == 3 and calls[-1][0]["previous_translation"] == saved["1"]
