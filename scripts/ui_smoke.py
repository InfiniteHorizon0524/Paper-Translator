"""End-to-end UI verification with real PDF parsing and a local mock LLM API."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".deps"))
sys.path.insert(0, str(ROOT))
import pymupdf
from playwright.sync_api import sync_playwright


class MockProvider(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(body)
        messages = body["messages"]
        if "Convert the research question" in messages[0].get("content", ""):
            text = "attention transformer translation accuracy"
        elif "学术论文译者" in messages[0].get("content", ""):
            text = (
                "## 中文译文\n\n论文提出了一种注意力机制，改善了翻译准确率。\n\n"
                r"行内公式 $x_i^2$，以及 \(\alpha + \beta\)。" + "\n\n"
                r"$$\mathrm{Attention}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V\tag{1}$$" + "\n\n"
                r"\[\begin{aligned}a &= b+c \\ d &= \begin{pmatrix}1 & 0 \\ 0 & 1\end{pmatrix}\end{aligned}\]" + "\n\n"
                "**实验结果**：准确率提高 12%。\n\n"
                "表 1：实验结果\n\n| 方法 | 准确率 | 表达式 |\n| :--- | ---: | :---: |\n"
                r"| **本文方法** | 92% | $\|x\|_2$ |" + "\n| 基线 | 80% | — |"
            )
            if "[[PR_FIGURE_1]]" in messages[-1]["content"]:
                text += "\n\n正文角标[^1]。\n\n[[PR_FIGURE_1]]\n\n图 1：注意力机制示意图。\n\n后续正文说明实验结果。\n\n[^1]: 这是一条页末注释，字号小于正文。"
        elif len(messages) == 1:
            text = "OK"
        else:
            text = "## 核心贡献\n\n论文提出了一种注意力机制 $x_i^2$。[第1页]\n\n实验表明准确率提高了 12%。[第2页]\n\n| 方法 | 准确率 | 来源 |\n| --- | ---: | --- |\n| 本文方法 | 92% | [第2页] |"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        try:
            for start in range(0, len(text), 8):
                delta = {"choices": [{"delta": {"content":text[start:start + 8]},"finish_reason":None}]}
                self.wfile.write(("data: " + json.dumps(delta,ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(.025)
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def main():
    test_dir = Path(os.environ.get("PAPERREADER_TEST_ROOT", ROOT / ".test-data" / "ui"))
    test_dir.mkdir(parents=True,exist_ok=True)
    pdf = test_dir / "sample.pdf"
    with pymupdf.open() as doc:
        for content in ["Sample Research Paper\n\nAbstract\nWe propose a transformer attention mechanism.\nThe attention mechanism improves translation accuracy.\n\n1. Introduction\nAcademic papers contain useful ideas.","2. Results\n\nThe attention mechanism improves accuracy by 12 percent.\nSmall datasets are a limitation of this approach."]:
            page = doc.new_page(); page.insert_text((60,80),content,fontsize=14)
            if len(doc) == 1:
                page.draw_rect((60, 350, 440, 500), color=(.3, .3, .3))
                page.insert_text((80, 400), "ORIGINAL IMAGE LABEL", fontsize=14)
                page.insert_text((60, 530), "Figure 1: Attention mechanism diagram.", fontsize=12)
                page.draw_line((60, 690), (220, 690))
                page.insert_text((60, 716), "1 Footnote about the experiment.", fontsize=10)
        doc.save(pdf)
    provider = ThreadingHTTPServer(("127.0.0.1",0),MockProvider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    port = available_port()
    environment = dict(os.environ,PAPERREADER_DATA_DIR=str(test_dir / ("library-" + uuid.uuid4().hex[:8])),PYTHONPATH=str(ROOT / ".deps"))
    executable = os.environ.get("PAPERREADER_TEST_EXE")
    command = [executable] if executable else [sys.executable, str(ROOT / "launcher.py")]
    proc = subprocess.Popen(command + ["--server","--port",str(port)],env=environment,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode(errors="replace"))
            try:
                urllib.request.urlopen(url + "/api/session", timeout=.5); break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("Server did not start")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width":1440,"height":950},device_scale_factor=1)
            errors = []
            page.on("pageerror",lambda error: errors.append(str(error)))
            page.add_init_script("window.policyViolations = []; document.addEventListener('securitypolicyviolation', e => window.policyViolations.push(e.violatedDirective));")
            # Formula rendering must work with external browser requests blocked.
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") or route.request.url.startswith("blob:") else route.abort())
            page.goto(url); page.locator("#welcome h1").wait_for()
            page.screenshot(path=str(test_dir / "welcome.png"),full_page=True)
            if os.environ.get("PAPERREADER_TEST_ARXIV"):
                page.locator("#welcome-arxiv").click(); page.locator("#arxiv-id").fill("1706.03762"); page.locator("#import-submit").click()
                page.locator("#paper-title").filter(has_text="Attention Is All You Need").wait_for(timeout=120000)
                assert page.locator("#paper-pages").inner_text() == "15 页"
                page.locator("#import-open").click(); page.locator("[data-import='upload']").click()
            else:
                page.locator("#welcome-upload").click()
            page.locator("#file-input").set_input_files(str(pdf)); page.locator("#import-submit").click()
            page.locator("#reader").wait_for(state="visible")
            page.wait_for_function("() => document.querySelector('#pdf-page').naturalWidth > 0")
            assert page.locator("#chat-panel").is_hidden()
            assert page.locator("#assistant-toggle").get_attribute("aria-expanded") == "false"
            assert page.locator("#original-pane").is_visible() and page.locator("#translation-pane").is_visible()
            reader_width = page.locator(".document-panel").bounding_box()["width"]
            assert reader_width >= 1440 * .79, reader_width
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(253, 252, 248)"
            page.locator("#zoom-in").click(); assert page.locator("#zoom-reset").inner_text() == "125%"
            page.locator("#zoom-reset").click(); assert page.locator("#zoom-reset").inner_text() == "100%"
            page.locator("#settings-top").click(); page.locator("#base-url").fill(f"http://127.0.0.1:{provider.server_port}/v1"); page.locator("#model").fill("local-test-model"); page.locator("#api-key").fill("test-only-key")
            assert page.locator("#reasoning-effort").input_value() == "default"
            page.locator("#reasoning-effort").select_option("high")
            page.locator("#test-api").click()
            page.get_by_text("连接成功，可以开始翻译和问答。").wait_for()
            page.get_by_role("button",name="保存设置",exact=True).click()
            page.locator("#settings-dialog").wait_for(state="hidden")
            assert page.evaluate("state.config.reasoning_effort") == "high"
            page.locator("#translate-all").click()
            page.wait_for_function("() => document.querySelector('#translation-status').textContent.includes('已翻译 2 / 2')")
            assert "注意力机制" in page.locator("#translation-text").inner_text()
            assert page.locator("#translation-text .katex").count() == 10
            assert page.locator("#translation-text .katex-display").count() == 4
            assert page.locator("#translation-text .math-source").count() == 0
            assert page.locator("#translation-text table").count() == 2
            assert page.locator("#translation-text table").first.locator("tbody td").count() == 6
            assert page.locator("#translation-text th").nth(1).evaluate("e => getComputedStyle(e).textAlign") == "right"
            assert page.locator("#translation-text td .katex").count() == 2
            page.evaluate("() => document.fonts.ready")
            assert page.evaluate("document.fonts.check('18px KaTeX_Main')")
            assert page.evaluate("() => [...document.querySelectorAll('.math-block')].every(e => e.clientHeight >= e.scrollHeight - 1)")
            assert page.evaluate("() => [...document.querySelectorAll('.katex .strut')].every(e => e.getBoundingClientRect().height > 0)")
            assert page.evaluate("window.policyViolations") == []
            assert page.locator("#translation-text .figure-placeholder").count() == 1
            assert page.locator("#translation-text .figure-caption").inner_text() == "图 1：注意力机制示意图。"
            assert page.locator("#translation-text .page-footnotes").count() == 1
            assert page.locator("#translation-text .page-body").first.locator(":scope > :last-child").get_attribute("class") == "page-footnotes"
            assert page.evaluate("""() => {
                const body = document.querySelector('.page-body');
                return getComputedStyle(body).fontFamily.startsWith('"Times New Roman", SimSun')
                    && parseFloat(getComputedStyle(body.querySelector('h2')).fontSize) > parseFloat(getComputedStyle(body).fontSize)
                    && getComputedStyle(body.querySelector('h2')).fontWeight === '700'
                    && parseFloat(getComputedStyle(body.querySelector('.page-footnotes')).fontSize) < parseFloat(getComputedStyle(body).fontSize);
            }""")
            page.locator(".footnote-ref a").click()
            assert page.locator(".footnote-item").evaluate("e => e === document.activeElement")
            page.locator(".footnote-back").click()
            assert page.locator(".footnote-ref").evaluate("e => e === document.activeElement")
            page.get_by_role("button",name="译文",exact=True).click()
            page.locator(".figure-placeholder").focus()
            page.locator(".figure-placeholder").press("Enter")
            page.locator("#pdf-figure-highlight").wait_for(state="visible")
            assert page.locator("#original-pane").is_visible()
            assert page.locator("#page-number").input_value() == "1"
            assert page.evaluate("""() => {
                const image = document.querySelector('#pdf-page').getBoundingClientRect();
                const box = document.querySelector('#pdf-figure-highlight').getBoundingClientRect();
                return box.left >= image.left && box.right <= image.right && box.top > image.top && box.bottom < image.bottom;
            }""")
            ratio = page.locator("#pdf-figure-highlight").bounding_box()["width"] / page.locator("#pdf-page").bounding_box()["width"]
            page.locator("#zoom-in").click()
            zoom_ratio = page.locator("#pdf-figure-highlight").bounding_box()["width"] / page.locator("#pdf-page").bounding_box()["width"]
            assert abs(ratio - zoom_ratio) < .002
            page.locator("#zoom-reset").click()
            page.locator("#page-next").click()
            page.locator("#pdf-figure-highlight").wait_for(state="hidden")
            page.locator(".figure-placeholder").click()
            page.locator("#pdf-figure-highlight").wait_for(state="visible")
            assert page.locator("#page-number").input_value() == "1"
            page.locator(".figure-placeholder").scroll_into_view_if_needed()
            page.screenshot(path=str(test_dir / "figures-footnotes.png"), full_page=True, animations="disabled")
            page.evaluate("setPage(1)")
            page.screenshot(path=str(test_dir / "typography.png"), full_page=True, animations="disabled")
            assert page.evaluate("""() => {
                const probe = document.createElement('div');
                probe.innerHTML = PaperReaderRichText.markdown('$$' + Array(80).fill('x_i').join('+') + '$$');
                document.querySelector('.page-body').append(probe);
                const block = probe.querySelector('.math-block'), content = probe.querySelector('.katex-display');
                const valid = block.scrollWidth > block.clientWidth && content.getBoundingClientRect().left >= block.getBoundingClientRect().left;
                block.scrollLeft = block.scrollWidth;
                const scrollable = block.scrollLeft > 0;
                probe.remove(); return valid && scrollable;
            }""")
            page.screenshot(path=str(test_dir / "latex.png"),full_page=True,animations="disabled")
            page.locator("#translation-text .table-scroll").first.scroll_into_view_if_needed()
            page.screenshot(path=str(test_dir / "tables.png"),full_page=True,animations="disabled")
            # Verify stopping a retranslation leaves the completed cached page intact.
            page.locator("#translate-current").click()
            page.wait_for_function("() => document.querySelector('#translation-status').textContent.includes('正在翻译')")
            page.locator(".streaming-marker").wait_for()
            page.locator("#translate-stop").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert "未完成" in page.locator("#translation-status").inner_text()
            page.locator("#translate-current").click()
            page.wait_for_function("() => document.querySelector('#translation-status').textContent.includes('已翻译 2 / 2')")
            with page.expect_download() as download_info:
                page.locator("#export-translation").click()
            downloaded = download_info.value; downloaded.save_as(test_dir / "translation.md")
            assert "第 2 页" in (test_dir / "translation.md").read_text(encoding="utf-8")
            assert r"\frac{QK^\top}{\sqrt{d_k}}" in (test_dir / "translation.md").read_text(encoding="utf-8")
            assert "| 方法 | 准确率 | 表达式 |" in (test_dir / "translation.md").read_text(encoding="utf-8")
            page.locator("#toast").wait_for(state="hidden")
            page.screenshot(path=str(test_dir / "reader-full.png"),full_page=True,animations="disabled")
            page.locator("#assistant-toggle").click()
            assert page.locator("#chat-panel").is_visible()
            assert page.locator("#assistant-toggle").get_attribute("aria-expanded") == "true"
            assert page.locator(".document-panel").bounding_box()["width"] == reader_width
            page.locator("#question").fill("这篇论文的核心贡献是什么？"); page.locator("#chat-send").click()
            page.locator("#chat-close").click()
            assert page.locator("#chat-panel").is_hidden()
            page.wait_for_function("() => document.querySelector('#chat-stop').classList.contains('hidden')")
            page.locator("#assistant-toggle").click()
            page.locator(".message.assistant .citation").first.wait_for()
            assert "12%" in page.locator(".message.assistant").inner_text()
            assert "思考强度：高" in page.locator("#chat-model").inner_text()
            assert page.locator(".message.assistant .katex").count() == 1
            assert page.locator(".message.assistant table").count() == 1
            assert page.locator(".message.assistant td .citation").get_attribute("data-cite") == "2"
            page.screenshot(path=str(test_dir / "reader.png"),full_page=True,animations="disabled")
            page.locator(".message.assistant .citation[data-cite='2']").first.click()
            assert page.locator("#page-number").input_value() == "2"
            assert page.locator("#chat-panel").is_hidden()
            page.locator("#assistant-toggle").click()
            page.locator("#question").fill("这个结果有哪些局限？"); page.locator("#question").press("Enter")
            page.wait_for_function("() => document.querySelectorAll('.message.assistant').length === 2")
            page.wait_for_function("() => document.querySelector('#chat-stop').classList.contains('hidden')")
            page.locator("#question").fill("尚未发送的问题")
            page.locator("#question").press("Escape")
            assert page.locator("#chat-panel").is_hidden()
            assert page.locator("#assistant-toggle").get_attribute("aria-expanded") == "false"
            page.locator("#assistant-toggle").click()
            assert page.locator("#question").input_value() == "尚未发送的问题"
            assert page.locator(".message.assistant").count() == 2
            page.locator("#assistant-toggle").click()
            assert page.locator("#chat-panel").is_hidden()
            for width in [980, 1280, 1920]:
                page.set_viewport_size({"width":width,"height":900})
                assert page.locator(".document-panel").bounding_box()["width"] >= width * .79
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width":1440,"height":950})
            page.get_by_role("button",name="译文",exact=True).click()
            page.screenshot(path=str(test_dir / "translation.png"),full_page=True)
            page.get_by_role("button",name="原文",exact=True).click()
            with page.expect_download() as pdf_download:
                page.locator("#pdf-download").click()
            pdf_download.value.save_as(test_dir / "downloaded.pdf")
            assert (test_dir / "downloaded.pdf").read_bytes().startswith(b"%PDF-")
            # The key is encrypted on disk, excluded from browser storage, and restored on reload.
            assert "test-only-key" not in page.evaluate("JSON.stringify(localStorage)")
            settings_file = Path(environment["PAPERREADER_DATA_DIR"]) / "api-settings.json"
            assert "test-only-key" not in settings_file.read_text(encoding="utf-8")
            page.reload(); page.locator(".library-item").first.click(); page.locator("#reader").wait_for(state="visible")
            page.wait_for_function("() => document.querySelectorAll('#translation-text .katex').length === 10")
            assert page.locator("#api-indicator").inner_text() == "已配置"
            page.locator("#settings-top").click()
            assert page.locator("#api-key").input_value() == "test-only-key"
            assert page.locator("#api-key").get_attribute("type") == "password"
            assert page.locator("#reasoning-effort").input_value() == "high"
            assert Path(page.locator("#settings-storage-path").inner_text()) == settings_file
            page.locator("[data-close='settings-dialog']").click()
            page.set_viewport_size({"width":640,"height":900}); page.screenshot(path=str(test_dir / "mobile.png"),full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#assistant-toggle").click()
            assert page.locator("#chat-panel").is_visible()
            assert page.locator("#chat-panel").bounding_box()["width"] <= 640
            page.locator("#chat-close").click()
            assert page.locator("#chat-panel").is_hidden()
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#assistant-toggle").click()
            assert page.locator("#chat-panel").bounding_box()["width"] <= 390
            page.locator("#chat-close").click()
            assert not errors, errors
            page.set_viewport_size({"width":1440,"height":950})
            page.locator("[data-view='split']").click()
            assert page.locator(".reading-page").count() == 2
            assert "注意力机制" in page.locator(".reading-page").first.inner_text()
            page.locator("#outline-toggle").click()
            page.locator("[data-outline-page='2']").first.click()
            assert page.locator("#page-number").input_value() == "2"
            page.locator("#reading-splitter").focus()
            page.locator("#reading-splitter").press("ArrowLeft")
            assert page.locator("#reading-splitter").get_attribute("aria-valuenow") == "48"
            assert page.locator("#reading-splitter").bounding_box()["width"] == 8
            page.locator("#reading-splitter").press("Home")
            page.locator("#reading-settings-open").click()
            page.locator("#reading-font-size").fill("22")
            assert page.evaluate("getComputedStyle(document.querySelector('.page-body')).fontSize") == "22px"
            page.locator("#reading-reset").click()
            page.get_by_role("button",name="完成",exact=True).click()
            page.locator("#reader-back").click()
            assert page.locator(".paper-row").count() == 1
            assert "读到第 2 页" in page.locator(".paper-row").inner_text()
            page.screenshot(path=str(test_dir / "library.png"),full_page=True)
            page.locator("[data-library-filter='arxiv']").click()
            assert page.locator(".paper-row").count() == 0
            page.locator("[data-library-filter='all']").click()
            page.locator("#search").fill("missing")
            assert page.locator(".paper-row").count() == 0
            page.locator("#search").fill("")
            page.locator(".paper-row").click()
            assert page.locator("#reader").is_visible()
            page.locator("#sidebar-toggle").click()
            assert page.locator("#library-sidebar").is_hidden()
            page.locator("#sidebar-toggle").click()
            assert not errors, errors
            # Saving the restored settings preserves the saved translation.
            page.locator("#settings-top").click()
            page.locator("#api-key").fill("test-only-key")
            page.get_by_role("button",name="保存设置",exact=True).click()
            page.wait_for_function("() => document.querySelectorAll('#translation-text .katex').length === 10")
            assert page.locator("#translation-text table").count() == 2
            assert page.locator("#translation-text .math-source").count() == 0
            page.set_viewport_size({"width":640,"height":900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("() => [...document.querySelectorAll('.math-block')].every(e => e.clientHeight >= e.scrollHeight - 1)")
            assert page.evaluate("window.policyViolations") == []
            assert not errors, errors
            assert MockProvider.calls and all(call.get("reasoning_effort") == "high" for call in MockProvider.calls)
            assert any("学术论文译者" in call["messages"][0].get("content", "") for call in MockProvider.calls)
            assert any("Convert the research question" in call["messages"][0].get("content", "") for call in MockProvider.calls)
            assert any("学术阅读助手" in call["messages"][0].get("content", "") for call in MockProvider.calls)
            page.set_viewport_size({"width":1440,"height":950})
            page.locator("#settings-top").click()
            page.screenshot(path=str(test_dir / "reasoning-settings.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":720})
            dialog = page.locator("#settings-dialog").bounding_box()
            assert dialog["y"] >= 0 and dialog["y"] + dialog["height"] <= 720
            for control in ("#reasoning-effort", "#settings-form [type='submit']"):
                page.locator(control).scroll_into_view_if_needed()
                bounds = page.locator(control).bounding_box()
                assert bounds["y"] >= dialog["y"] and bounds["y"] + bounds["height"] <= dialog["y"] + dialog["height"]
            page.set_viewport_size({"width":1440,"height":950})
            page.locator("[data-close='settings-dialog']").click()
            browser.close()
        checks = ["warm ivory theme","reader uses at least 79% of screen","default split view","assistant hidden by default","assistant toggle + close + Escape","hidden generation continues","draft + conversation preserved","PDF upload","page images + zoom","API config + test","full Chinese translation","offline LaTeX + local fonts","matrices + aligned equations + tags","formula geometry + CSP","LaTeX Markdown export","assistant LaTeX","Markdown tables + alignment","tables with math + citations","saved table reload + export","stop + retry translation","Markdown export","Chinese Q&A","follow-up","page citations","PDF download","encrypted API settings + automatic restore + displayed path","responsive layout","no JS errors","library rows + filters + search","continuous original fallback","outline navigation","splitter keyboard controls","reading typography","reading progress","collapsible sidebar"]
        if os.environ.get("PAPERREADER_TEST_ARXIV"):
            checks.insert(0,"live arXiv import through UI")
        checks.extend(["Song + Times New Roman typography", "bold enlarged headings", "page-end footnotes + return links", "figure placeholders + keyboard activation", "original figure target + zoom alignment", "reasoning effort for translation + retrieval + assistant", "reasoning setting restoration"])
        print(json.dumps({"ok":True,"packaged":bool(executable),"checks":checks},ensure_ascii=False))
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        provider.shutdown()


if __name__ == "__main__":
    main()
