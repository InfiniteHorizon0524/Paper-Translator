import pymupdf

from paperreader import app as app_module, storage
from paperreader.documents import extract_pdf, page_layout, translation_with_figures
from test_app import CONFIG, client, events


def illustrated_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), "Research with Illustrations", fontsize=20, fontname="tibo")
        page.insert_text((50, 105), "1 Introduction", fontsize=14, fontname="tibo")
        page.insert_text((50, 140), "This is body text with a footnote", fontsize=12)
        page.insert_text((218, 136), "1", fontsize=8)
        page.draw_rect((60, 180, 340, 285), color=(0, 0, 0))
        page.insert_text((75, 215), "INTERNAL GRAPHIC LABEL", fontsize=11)
        page.insert_text((60, 315), "Figure 1: Original English caption.", fontsize=11)
        page.insert_text((50, 375), "The body explains experimental results.\n" * 6, fontsize=12)
        page.draw_line((50, 680), (210, 680))
        page.insert_text((50, 704), "1 A note about this experiment.", fontsize=9)
        page.insert_text((50, 738), "31st Conference on Research.", fontsize=9)
        page = doc.new_page()
        page.insert_text((50, 60), "2 Results", fontsize=16, fontname="tibo")
        page.insert_text((50, 100), "Body text continues here.\n" * 5, fontsize=12)
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 80, 60), False)
        pix.clear_with(220)
        page.insert_image((60, 230, 340, 400), stream=pix.tobytes("png"))
        page.insert_text((60, 425), "Figure 2: Raster image caption.", fontsize=11)
        return doc.tobytes()


def test_layout_titles_figures_notes_and_non_note_numbers():
    _, pages, metadata = extract_pdf(illustrated_pdf(), "illustrated.pdf")
    first, second = metadata["reading_layout"]["pages"]
    assert "## Research with Illustrations" in first["text"]
    assert "## 1 Introduction" in first["text"]
    assert "INTERNAL GRAPHIC LABEL" not in first["text"]
    assert "INTERNAL GRAPHIC LABEL" in pages[0]  # Retrieval retains original text.
    assert "[[PR_FIGURE_1]]\n\nFigure 1:" in first["text"]
    assert "[^1]: A note about this experiment." in first["text"]
    assert "[^31]" not in first["text"]
    assert "31st Conference" in first["text"]
    assert len(first["figures"]) == len(second["figures"]) == 1
    assert second["figures"][0]["label"] == "2"
    assert all(0 <= v <= 1 for v in first["figures"][0]["bbox"])
    assert first["text"].rfind("[^1]:") > first["text"].find("The body")


def test_legacy_layout_migration_and_translation_input(client, monkeypatch):
    paper = client.post("/api/papers/upload", files={"file": ("sample.pdf", illustrated_pdf(), "application/pdf")}).json()
    pid = paper["id"]
    paper["metadata"].pop("reading_layout")
    storage.save_metadata(pid, paper["metadata"])
    migrated = client.get(f"/api/papers/{pid}").json()
    assert migrated["metadata"]["reading_layout"]["pages"][0]["figures"]
    assert storage.load_paper(pid)["metadata"]["reading_layout"]
    assert "reading_layout" not in client.get("/api/papers").json()[0]["metadata"]
    calls = []
    async def complete(config, messages):
        calls.append(messages)
        yield "## 引言\n\n正文[^1]。\n\n[[PR_FIGURE_1]]\n\n图 1：中文图题。\n\n[^1]: 中文脚注。"
    monkeypatch.setattr(app_module, "completion", complete)
    parsed = events(client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": [1]}))
    assert parsed[-1][0] == "done"
    assert "INTERNAL GRAPHIC LABEL" not in calls[0][1]["content"]
    assert "Figure 1: Original English caption." in calls[0][1]["content"]
    assert "[[PR_FIGURE_1]]" in calls[0][1]["content"]
    assert "[^1]:" in calls[0][1]["content"]
    assert "只翻译图外" in calls[0][0]["content"]
    assert "脚注" in calls[0][0]["content"]


def test_rotation_and_multiple_figures_have_separate_targets():
    with pymupdf.open(stream=illustrated_pdf(), filetype="pdf") as doc:
        first = page_layout(doc[0])["figures"][0]["bbox"]
        doc[0].set_rotation(90)
        rotated = page_layout(doc[0])["figures"][0]["bbox"]
        expected = [1 - first[3], first[0], 1 - first[1], first[2]]
        assert all(abs(actual - target) < .00001 for actual, target in zip(rotated, expected))
        page = doc[1]
        page.draw_rect((60, 480, 340, 550), color=(0, 0, 0))
        page.insert_text((60, 578), "Figure 3: A second separate graphic.", fontsize=11)
        figures = page_layout(page)["figures"]
        assert len(figures) == 2
        assert figures[0]["label"] == "2" and figures[1]["label"] == "3"
        assert figures[0]["bbox"][3] < figures[1]["bbox"][1]


def opening_diagram_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page()
        for left in [75, 350]:
            page.draw_rect((left, 80, left + 140, 175), color=(0, 0, 0))
            page.insert_text((left + 12, 100), "Sum\nLinear\nAttention", fontsize=9)
            page.insert_text((left, 210), "(a) Multi-Head Attention" if left == 75 else "(b) Multi-Channel Update", fontsize=9)
        page.insert_text((60, 250), "Figure 2: Computation graphs for the two models.", fontsize=11)
        page.insert_text((60, 310), "The body explains the method and must remain.\n" * 8, fontsize=12)
        page.insert_text((60, 540), "Figure 3 compares the full models.\nThis is prose, not a figure caption.", fontsize=12)
        return doc.tobytes()


def test_opening_figure_replaces_legacy_graph_labels_without_losing_body():
    _, _, metadata = extract_pdf(opening_diagram_pdf(), "diagram.pdf")
    layout = metadata["reading_layout"]["pages"][0]
    assert layout["text"].startswith("[[PR_FIGURE_1]]")
    assert len(layout["figures"]) == 1
    assert "Linear" not in layout["text"] and "(a) Multi" not in layout["text"]
    assert "Figure 2: Computation graphs" in layout["text"]
    assert "Figure 3 compares the full models." in layout["text"]
    old = "Sum\n\nLinear\n\n$W_i$\n\n(a) 多头注意力\n\n单通道更新\n\n(b) 多通道更新\n\n图 2: 两种模型的计算图。\n\n后续正文 $W^Q$。\n\n图 3 比较完整模型。"
    cleaned = translation_with_figures(old, layout)
    assert cleaned == "[[PR_FIGURE_1]]\n\n图 2: 两种模型的计算图。\n\n后续正文 $W^Q$。\n\n图 3 比较完整模型。"
    assert translation_with_figures(cleaned, layout) == cleaned
    assert translation_with_figures("(a) Multi-Head Attention\n\nh\n\n(b) Multi-Channel Update\n\n" + cleaned, layout) == cleaned
    assert translation_with_figures(cleaned.replace("[[PR_FIGURE_1]]", "[[PR_FIGURE_1]]\n\n(a) Multi-Head Attention\n\nh\n\n(b) Multi-Channel Update"), layout) == cleaned
    for caption in ["**图 2：** 说明。", "## 图 2：说明。", "Figure 2: Caption."]:
        assert translation_with_figures("Sum\n\n" + caption, layout) == "[[PR_FIGURE_1]]\n\n" + caption
    assert translation_with_figures("没有图题的正文不能猜测删除。", layout) == "没有图题的正文不能猜测删除。"
    middle = dict(layout, text="前面的正文\n\n" + layout["text"])
    assert translation_with_figures(old, middle) == old


def test_saved_legacy_figure_display_and_cache_skip_need_no_model_call(client, monkeypatch):
    paper = client.post("/api/papers/upload", files={"file": ("diagram.pdf", opening_diagram_pdf(), "application/pdf")}).json()
    pid = paper["id"]
    old = "Sum\n\nLinear\n\n(a) 多头注意力\n\n图 2：两种模型的计算图。\n\n真实正文。"
    storage.save_translation(pid, 1, old)
    async def no_call(*args):
        raise AssertionError("Cached figure cleanup must not call an API")
        yield ""
    monkeypatch.setattr(app_module, "completion", no_call)
    saved = client.post(f"/api/papers/{pid}/translations", json={"config": CONFIG}).json()
    expected = "[[PR_FIGURE_1]]\n\n图 2：两种模型的计算图。\n\n真实正文。"
    assert saved["1"] == expected
    cached = events(client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": [1]}))
    assert cached[0][1]["text"] == expected and cached[0][1]["cached"]
    assert storage.translations(pid)["1"] == old
    async def legacy_output(config, messages):
        assert "Sum" not in messages[1]["content"] and "Linear" not in messages[1]["content"]
        yield "(a) Multi-Head Attention\n\nh\n\n(b) Multi-Channel Update\n\n" + expected
    monkeypatch.setattr(app_module, "completion", legacy_output)
    refreshed = events(client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": [1], "force": True}))
    assert next(data["text"] for event, data in refreshed if event == "page_done") == expected
    assert storage.translations(pid)["1"] == expected
