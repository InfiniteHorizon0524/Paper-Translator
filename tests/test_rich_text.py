"""Browser regressions for TeX/Markdown boundaries and untrusted model output."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def renderer():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True
        )
        page = browser.new_page()
        page.set_content('<div id="result"></div>')
        page.add_script_tag(path=str(ROOT / "static/vendor/katex/katex.min.js"))
        page.add_script_tag(path=str(ROOT / "static/rich-text.js"))
        page.add_style_tag(path=str(ROOT / "static/styles.css"))
        yield page
        browser.close()


def render(page, text):
    page.evaluate("text => { document.querySelector('#result').innerHTML = PaperReaderRichText.markdown(text, 2); }", text)
    return page.locator("#result")


@pytest.mark.parametrize("source,display", [
    (r"行内 $x_i^2$ 后文", False),
    (r"行内 \(\frac{a}{b}\) 后文", False),
    (r"前文 $$\sum_{i=1}^n x_i\tag{2}$$ 后文", True),
    (r"\[\begin{pmatrix}1 & 2 \\ 3 & 4\end{pmatrix}\]", True),
    ("$$\n" + r"\begin{aligned}a &= b \\" + "\n\n" + r"c &= d\end{aligned}" + "\n$$", True),
    (r"$a < b \text{ \& `value` }$", False),
    (r"**$x*y$** [第2页]", False),
])
def test_formula_delimiters_and_markdown_boundaries(renderer, source, display):
    result = render(renderer, source)
    assert result.locator(".katex").count() == 1
    assert result.locator(".katex-display").count() == int(display)
    assert result.locator(".math-source").count() == 0
    assert result.locator("annotation[encoding='application/x-tex']").text_content()
    assert result.locator("p .math-block").count() == 0
    if "**" in source:
        assert result.locator("strong .katex").count() == 1
        assert result.locator(".citation[data-cite='2']").count() == 1


def test_code_currency_and_html_stay_literal(renderer):
    source = '金额 $5 and $10；' + r'转义 \$20；`$x$ [第1页]`' + '\n\n```latex\n$$a**b$$\n```\n\n<img src=x onerror="alert(1)">'
    result = render(renderer, source)
    assert result.locator(".katex, img, script, .citation").count() == 0
    assert result.locator("code").count() == 2
    assert result.locator("pre code").text_content() == "$$a**b$$\n"
    assert "金额 $5 and $10；转义 $20" in result.text_content()
    assert '<img src=x onerror="alert(1)">' in result.text_content()


def test_partial_invalid_and_following_formulas(renderer):
    source = r"前文 $x_i^2$ 后文 $$\frac{1}{"
    for stop in range(1, len(source) + 1):
        render(renderer, source[:stop])
    result = renderer.locator("#result")
    assert result.locator(".katex").count() == 1
    assert r"$$\frac{1}{" in result.text_content()
    result = render(renderer, r"$$\unsupportedcommand{x}$$ 后文 $y^2$")
    assert result.locator(".math-source").text_content() == r"$$\unsupportedcommand{x}$$"
    assert result.locator(".katex").count() == 1


def test_tex_cannot_create_links_or_remote_images(renderer):
    result = render(renderer, r"$\href{javascript:alert(1)}{click}$ $\includegraphics{https://example.com/x.png}$ $\text{<script>alert(1)</script>}$")
    assert result.locator("a, img, script, [href], [src], [onclick], [onerror]").count() == 0


def test_formula_citations_and_macros_are_isolated(renderer):
    result = render(renderer, r"$\text{[第1页]}$ [第2页] $\gdef\myvar{x}\myvar$ $\myvar$")
    assert result.locator(".citation").count() == 1
    assert result.locator(".citation").get_attribute("data-cite") == "2"
    assert result.locator(".math-source").text_content() == r"$\myvar$"


@pytest.mark.parametrize("source", [
    "| 方法 | 准确率 |\n| --- | ---: |\n| 基线 | 80% |",
    "方法 | 准确率\n--- | ---:\n基线 | 80%",
    "| 方法 | 准确率\n--- | ---: |\n基线 | 80% |",
    "| 方法 | 准确率 |\r\n| - | -: |\r\n| 基线 | 80% |",
])
def test_table_syntax_variants(renderer, source):
    result = render(renderer, source)
    assert result.locator("table").count() == 1
    assert result.locator("thead th").all_text_contents() == ["方法", "准确率"]
    assert result.locator("tbody td").all_text_contents() == ["基线", "80%"]
    assert result.locator("th").first.get_attribute("scope") == "col"
    assert result.locator("p table, p .table-scroll").count() == 0
    assert result.locator(".table-scroll").get_attribute("tabindex") == "0"
    assert result.locator("td").last.evaluate("e => getComputedStyle(e).textAlign") == "right"


def test_table_formulas_code_escaped_pipes_and_citations(renderer):
    source = "| **指标** | 表达式 | 引用 |\n| :--- | :---: | ---: |\n" + r"| a\|b | $\|x\|$ 与 `a\|b` | [第2页] |"
    result = render(renderer, source)
    assert result.locator("th strong").text_content() == "指标"
    assert result.locator("tbody td").count() == 3
    assert result.locator("td").first.text_content() == "a|b"
    assert result.locator("td code").text_content() == "a|b"
    assert result.locator("td .katex").count() == 1
    assert result.locator("annotation").text_content() == r"\|x\|"
    assert result.locator("td .citation").get_attribute("data-cite") == "2"
    assert result.locator("td").nth(1).evaluate("e => getComputedStyle(e).textAlign") == "center"


def test_table_boundaries_short_rows_and_empty_body(renderer):
    source = "前文\n## 结果\n| A | B |\n| --- | --- |\n| 1 |\n| 2 | 3 | ignored |\n\n后文\n\n| X |\n| --- |\n## 结论\n- 一项\n- 二项"
    result = render(renderer, source)
    assert result.locator("table").count() == 2
    assert result.locator("table").first.locator("tbody td").all_text_contents() == ["1", "", "2", "3"]
    assert result.locator("table").last.locator("tbody").count() == 0
    assert result.locator("h2").all_text_contents() == ["结果", "结论"]
    assert result.locator("li").all_text_contents() == ["一项", "二项"]
    assert "前文" in result.text_content() and "后文" in result.text_content()


def test_table_stream_and_untrusted_cell_content(renderer):
    source = '| A | B |\n| --- | --- |\n| $x_i^2$ | <img src=x onerror="alert(1)"><br>下一行 |'
    for stop in range(1, len(source) + 1):
        render(renderer, source[:stop])
    result = renderer.locator("#result")
    assert result.locator("table .katex").count() == 1
    assert result.locator("td br").count() == 1
    assert result.locator("img, script, [onerror]").count() == 0
    assert '<img src=x onerror="alert(1)">' in result.text_content()


@pytest.mark.parametrize("source", [
    "| A | B |\n| --- |\n| 1 | 2 |",
    "| A | B |\n| --x | --- |\n| 1 | 2 |",
    "```markdown\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```",
    "`A | B`\n`--- | ---`",
])
def test_non_tables_and_code_are_not_tables(renderer, source):
    result = render(renderer, source)
    assert result.locator("table").count() == 0


def test_wide_table_scrolls_within_reading_column(renderer):
    source = '| ' + ' | '.join(f'列{i}' for i in range(12)) + ' |\n'
    source += '| ' + ' | '.join(['---'] * 12) + ' |\n'
    source += '| ' + ' | '.join(['12345'] * 12) + ' |'
    result = render(renderer, source)
    result.evaluate("e => { e.className = 'page-body'; e.style.width = '300px'; }")
    block = result.locator(".table-scroll")
    assert block.evaluate("e => e.scrollWidth > e.clientWidth")
    assert block.bounding_box()["width"] <= 300
    assert block.evaluate("e => { e.scrollLeft = e.scrollWidth; return e.scrollLeft > 0; }")


def test_heading_sizes_follow_body_size_and_notes_are_page_scoped(renderer):
    source = "前文\n## 标题 Heading\n正文[^1]，引用 [12] 和公式 $x^2$。\n\n[^1]: 注释含 $y_i$。\n    第二行。\n\n最后一段。"
    renderer.evaluate("""source => {
        document.querySelector('#result').className = '';
        document.querySelector('#result').innerHTML = [1,2].map(page =>
            `<section class="reading-page"><div class="page-body">${PaperReaderRichText.markdown(source, 0, {page})}</div></section>`).join('');
    }""", source)
    result = renderer.locator("#result")
    assert result.locator("h2").count() == 2
    assert result.locator(".footnote-ref").count() == 2
    assert result.locator(".page-footnotes hr").count() == 2
    assert result.locator(".katex").count() == 4
    assert "[12]" in result.inner_text()
    assert result.locator(".footnote-ref a").first.get_attribute("href") != result.locator(".footnote-ref a").last.get_attribute("href")
    assert result.locator(".page-body").first.locator(":scope > :last-child").get_attribute("class") == "page-footnotes"
    assert "第二行。" in result.locator(".page-footnotes").first.inner_text()
    for size in [18, 26]:
        renderer.evaluate("size => document.documentElement.style.setProperty('--reading-size', `${size}px`)", size)
        geometry = result.locator(".page-body").first.evaluate("""e => ({
            body: parseFloat(getComputedStyle(e).fontSize),
            heading: parseFloat(getComputedStyle(e.querySelector('h2')).fontSize),
            note: parseFloat(getComputedStyle(e.querySelector('.page-footnotes')).fontSize),
            weight: getComputedStyle(e.querySelector('h2')).fontWeight,
            family: getComputedStyle(e).fontFamily
        })""")
        assert geometry["heading"] == size * 1.5
        assert geometry["note"] == size * .75
        assert geometry["weight"] == "700"
        assert geometry["family"].startswith('"Times New Roman", SimSun')
    renderer.evaluate("document.documentElement.style.removeProperty('--reading-size')")


def test_figure_placeholders_caption_fallback_and_literal_code(renderer):
    source = "正文。\n\n[[PR_FIGURE_1]]\n\n图 1：说明。\n\n正文后续。\n\n[^a]: 页脚。"
    context = {"page": 3, "figures": [{"id": "1", "label": "1"}]}
    for text in [source, source.replace("[[PR_FIGURE_1]]\n\n", "")]:
        renderer.evaluate("args => document.querySelector('#result').innerHTML = PaperReaderRichText.markdown(args.text, 0, args.context)", {"text": text, "context": context})
        result = renderer.locator("#result")
        assert result.locator(".figure-placeholder").count() == 1
        assert result.locator(".figure-placeholder").get_attribute("data-figure-page") == "3"
        assert result.locator(".figure-placeholder + .figure-caption").text_content() == "图 1：说明。"
        assert result.locator("img, script, iframe").count() == 0
        assert result.locator(".page-footnotes").count() == 1
    result = render(renderer, "`[^1]`\n\n```\n[^1]: literal\n[[PR_FIGURE_1]]\n```\n\n正文 [12]")
    assert result.locator(".page-footnotes, .footnote-ref, .figure-placeholder").count() == 0
    assert "[^1]: literal" in result.inner_text()


def test_footnote_display_formula_stays_in_footer(renderer):
    result = render(renderer, "正文[^1]。\n\n[^1]: 注释中的公式 $$x^2$$。\n    $$y_i = 2$$\n\n最后一段正文。")
    assert result.locator(".page-footnotes .math-block").count() == 2
    assert result.locator(".page-footnotes .katex").count() == 2
    assert result.locator(".page-footnotes").evaluate("e => e === e.parentElement.lastElementChild")
    assert "最后一段正文。" not in result.locator(".page-footnotes").inner_text()
