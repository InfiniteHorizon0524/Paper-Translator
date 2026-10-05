"""Verify paper-scoped caching and independent background translation in a real browser."""
import json
import os
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
from scripts.ui_smoke import available_port


class ControlledProvider(BaseHTTPRequestHandler):
    gates = {name: threading.Event() for name in ("original", "changed", "retry")}
    calls = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError):
            pass  # Expected when the UI explicitly stops a streaming request.

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.calls.append((self.path, body))
        model = body["model"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        try:
            prefix = f"{model} 模型的中文译文。"
            delta = {"choices":[{"delta":{"content":prefix},"finish_reason":None}]}
            self.wfile.write(("data: " + json.dumps(delta, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
            if not self.gates[model].wait(45):
                return
            delta["choices"][0]["delta"]["content"] = "完整结束。"
            self.wfile.write(("data: " + json.dumps(delta, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def main():
    test_dir = Path(os.environ.get("PAPERREADER_TEST_ROOT", ROOT / ".test-data" / "background"))
    test_dir.mkdir(parents=True, exist_ok=True)
    for name in ("First paper", "Second paper", "Range paper"):
        with pymupdf.open() as doc:
            doc.set_metadata({"title":name})
            for number in range(1, 5 if name == "Range paper" else 3):
                doc.new_page().insert_text((60, 80), f"{name}\nPage {number}\nOriginal English research content.")
            doc.save(test_dir / f"{name}.pdf")
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ControlledProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = available_port()
    environment = dict(os.environ, PAPERREADER_DATA_DIR=str(test_dir / uuid.uuid4().hex), PYTHONPATH=str(ROOT / ".deps"))
    executable = os.environ.get("PAPERREADER_TEST_EXE")
    command = [executable] if executable else [sys.executable, str(ROOT / "launcher.py")]
    proc = subprocess.Popen(command + ["--server", "--port", str(port)], env=environment, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode(errors="replace"))
            try:
                urllib.request.urlopen(url + "/api/session", timeout=.5)
                break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("Server did not start")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width":1440, "height":950})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            translation_requests = []
            page.on("request", lambda request: translation_requests.append(request.post_data_json) if request.url.endswith("/translate") else None)
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") or route.request.url.startswith("blob:") else route.abort())
            page.goto(url)
            page.locator("#welcome h1").wait_for()

            def choose(title):
                page.locator(".library-item").filter(has_text=title).click()
                page.wait_for_function("title => document.querySelector('#paper-title').textContent === title", arg=title)

            def configure(model, path="v1", effort="default"):
                page.locator("#settings-top").click()
                page.locator("#base-url").fill(f"http://127.0.0.1:{provider.server_port}/{path}")
                page.locator("#model").fill(model)
                page.locator("#api-key").fill("test-key-" + model)
                page.locator("#reasoning-effort").select_option(effort)
                page.get_by_role("button", name="保存设置", exact=True).click()

            def saved(paper_id):
                return page.evaluate("id => jsonApi(`/api/papers/${id}/translations`)", paper_id)

            def sidebar():
                return page.locator(".library-item strong").all_text_contents()

            def progress(title):
                return page.locator(".library-item").filter(has_text=title).locator(".library-translation-progress").inner_text()

            for title in ("First paper", "Second paper"):
                page.locator("#import-open").click()
                page.locator("#file-input").set_input_files(str(test_dir / f"{title}.pdf"))
                page.locator("#import-submit").click()
                page.wait_for_function("title => document.querySelector('#paper-title').textContent === title", arg=title)
                page.locator("#import-dialog").wait_for(state="hidden")
            ids = page.evaluate("Object.fromEntries(state.papers.map(p => [p.title, p.id]))")
            order = ["Second paper", "First paper"]
            assert sidebar() == order
            assert progress("First paper") == progress("Second paper") == "已翻译 0/2 页"
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "false"
            choose("First paper")
            assert sidebar() == order
            configure("original", effort="high")
            page.locator("#translate-all").click()
            page.wait_for_function("() => document.querySelector('#translation-text').textContent.includes('original 模型')")
            assert progress("First paper") == "已翻译 0/2 页 · 翻译中"
            assert page.locator("#translate-from-current").is_disabled()
            configure("changed", "other-api", effort="low")
            assert page.locator("#translate-stop").is_visible()
            choose("Second paper")
            assert "original 模型" not in page.locator("#translation-text").inner_text()
            assert page.locator("#translate-all").is_enabled()
            page.locator("#translate-all").click()
            page.wait_for_function("() => document.querySelector('#translation-text').textContent.includes('changed 模型')")
            assert page.evaluate("[...state.translationSessions.values()].filter(s => s.job).length") == 2
            assert sidebar() == order
            ControlledProvider.gates["changed"].set()
            page.wait_for_function("() => document.querySelector('#translation-status').textContent.includes('已翻译 2 / 2')")
            assert len(saved(ids["Second paper"])) == 2
            assert progress("Second paper") == "已翻译 2/2 页"
            assert saved(ids["First paper"]) == {}
            choose("First paper")
            assert "original 模型" in page.locator("#translation-text").inner_text()
            assert page.locator("#translate-all").is_disabled()
            page.locator("#reader-back").click()
            page.locator("#library-sort").select_option("opened")
            assert sidebar() == order
            page.locator("#library-sort").select_option("title")
            assert page.locator(".paper-row strong").all_text_contents() == ["First paper", "Second paper"]
            assert sidebar() == order
            choose("Second paper")
            ControlledProvider.gates["original"].set()
            page.wait_for_function("() => [...state.translationSessions.values()].every(s => !s.job)")
            assert "changed 模型" in page.locator("#translation-text").inner_text()
            assert len(saved(ids["First paper"])) == 2
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == 4
                original_calls = [call for call in ControlledProvider.calls if call[1]["model"] == "original"]
                assert len(original_calls) == 2 and all(path == "/v1/chat/completions" for path, _ in original_calls)
                assert all(body["reasoning_effort"] == "high" for _, body in original_calls)
                assert all(body["reasoning_effort"] == "low" for _, body in ControlledProvider.calls if body["model"] == "changed")
            choose("First paper")
            assert "original 模型" in page.locator("#translation-text").inner_text()
            page.locator("#translate-all").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == 4  # Different service/model reused both cached pages.

            # Stop and delete affect the selected paper, leaving another task active.
            configure("retry", effort="medium")
            page.locator("#translate-current").click()
            page.wait_for_function("() => document.querySelector('#translation-text').textContent.includes('retry 模型')")
            choose("Second paper")
            page.locator("#translate-current").click()
            page.wait_for_function("() => document.querySelector('#translation-text').textContent.includes('retry 模型')")
            page.locator("#translate-stop").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert page.evaluate("id => !!state.translationSessions.get(id).job", ids["First paper"])
            assert "changed 模型" in saved(ids["Second paper"])["1"]
            page.locator("#translate-current").click()
            page.locator(".streaming-marker").wait_for()
            page.locator("#paper-delete").click()
            page.locator("#delete-confirm").click()
            page.locator("#delete-dialog").wait_for(state="hidden")
            assert page.evaluate("id => !!state.translationSessions.get(id).job", ids["First paper"])
            assert page.locator(".library-item").count() == 1
            ControlledProvider.gates["retry"].set()
            choose("First paper")
            page.wait_for_function("() => document.querySelector('#translation-status').textContent.includes('已翻译 2 / 2')")
            assert "retry 模型" in saved(ids["First paper"])["1"]
            assert "original 模型" in saved(ids["First paper"])["2"]
            page.screenshot(path=str(test_dir / "completed.png"), full_page=True)
            # Restart the server process as well as the page to verify disk restoration.
            proc.terminate()
            proc.wait(timeout=10)
            proc = subprocess.Popen(command + ["--server", "--port", str(port)], env=environment, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(150):
                try:
                    urllib.request.urlopen(url + "/api/session", timeout=.5)
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError("Server restart did not complete")
            page.reload()
            page.locator(".library-item").wait_for()
            assert progress("First paper") == "已翻译 2/2 页"
            assert page.evaluate("state.translationSessions.size") == 0
            choose("First paper")
            assert "retry 模型" in page.locator("#translation-text").inner_text()
            assert "original 模型" in page.locator("#translation-text").inner_text()
            page.locator("#settings-top").click()
            assert page.locator("#api-key").input_value() == "test-key-retry"
            assert page.locator("#model").input_value() == "retry"
            assert page.locator("#reasoning-effort").input_value() == "medium"
            assert page.locator("#base-url").input_value() == f"http://127.0.0.1:{provider.server_port}/v1"
            page.locator("[data-close='settings-dialog']").click()
            page.locator("#translate-current").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert page.locator("#settings-dialog").is_hidden()
            with ControlledProvider.lock:
                before_retranslation = len(ControlledProvider.calls)
            page.locator("#retranslate-all").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert all("retry 模型" in content for content in saved(ids["First paper"]).values())
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == before_retranslation + 2

            # Range translation includes the current page, skips cached pages,
            # and counts saved pages across stops, background work and reloads.
            page.locator("#import-open").click()
            page.locator("#file-input").set_input_files(str(test_dir / "Range paper.pdf"))
            page.locator("#import-submit").click()
            page.wait_for_function("() => document.querySelector('#paper-title').textContent === 'Range paper'")
            page.locator("#import-dialog").wait_for(state="hidden")
            range_id = page.evaluate("state.paper.id")
            assert progress("Range paper") == "已翻译 0/4 页"
            page.locator("#page-number").fill("4")
            page.locator("#page-number").dispatch_event("change")
            page.locator("#translate-from-current").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert translation_requests[-1]["pages"] == [4] and translation_requests[-1]["force"] is False, translation_requests[-1]["pages"]
            assert set(saved(range_id)) == {"4"}
            assert progress("Range paper") == "已翻译 1/4 页"
            page.locator("#page-number").fill("2")
            page.locator("#page-number").dispatch_event("change")
            ControlledProvider.gates["retry"].clear()
            page.locator("#translate-from-current").click()
            page.locator(".streaming-marker").wait_for()
            assert translation_requests[-1]["pages"] == [2, 3, 4]
            assert page.locator("#translate-from-current").is_disabled()
            assert progress("Range paper") == "已翻译 1/4 页 · 翻译中"
            page.locator("#translate-stop").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert set(saved(range_id)) == {"4"}
            assert progress("Range paper") == "已翻译 1/4 页"
            page.locator("#page-number").fill("2")
            page.locator("#page-number").dispatch_event("change")
            page.locator("#translate-from-current").click()
            page.locator(".streaming-marker").wait_for()
            assert translation_requests[-1]["pages"] == [2, 3, 4], translation_requests[-1]["pages"]
            page.wait_for_function("id => state.translationSessions.get(id).partial[2]?.includes('retry 模型')", arg=range_id)
            choose("First paper")
            assert page.locator("#translate-from-current").is_enabled()
            assert progress("Range paper") == "已翻译 1/4 页 · 翻译中"
            with ControlledProvider.lock:
                before_range_completion = len(ControlledProvider.calls)
            ControlledProvider.gates["retry"].set()
            page.wait_for_function("() => [...state.translationSessions.values()].every(s => !s.job)")
            assert set(saved(range_id)) == {"2", "3", "4"}, set(saved(range_id))
            assert progress("Range paper") == "已翻译 3/4 页"
            assert progress("First paper") == "已翻译 2/2 页"
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == before_range_completion + 1  # Page 3; page 4 stayed cached.
            choose("Range paper")
            page.locator("#page-number").fill("1")
            page.locator("#page-number").dispatch_event("change")
            page.locator("#translate-from-current").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert translation_requests[-1]["pages"] == [1, 2, 3, 4]
            assert set(saved(range_id)) == {"1", "2", "3", "4"}
            assert progress("Range paper") == "已翻译 4/4 页"
            with ControlledProvider.lock:
                before_cached_range = len(ControlledProvider.calls)
            page.locator("#page-number").fill("4")
            page.locator("#page-number").dispatch_event("change")
            page.locator("#translate-from-current").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            assert progress("Range paper") == "已翻译 4/4 页"
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == before_cached_range
            page.reload()
            page.locator(".library-item").first.wait_for()
            assert progress("Range paper") == "已翻译 4/4 页"
            assert page.evaluate("state.translationSessions.size") == 0
            choose("Range paper")
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "false"
            page.locator("#translation-lock").click()
            page.wait_for_function("() => document.querySelector('#translation-lock').getAttribute('aria-pressed') === 'true'")
            controls = ["translate-current", "translate-from-current", "translate-all", "retranslate-all"]
            assert all(page.locator("#" + control).is_disabled() for control in controls)
            assert progress("Range paper") == "已翻译 4/4 页 · 已锁定"
            before_locked_requests = len(translation_requests)
            with ControlledProvider.lock:
                before_locked_calls = len(ControlledProvider.calls)
            page.evaluate("async () => { for (const scope of ['current', 'from-current', 'all']) await translate(scope); await translate('all', true); }")
            assert len(translation_requests) == before_locked_requests
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == before_locked_calls
            page.reload()
            page.locator(".library-item").first.wait_for()
            assert progress("Range paper") == "已翻译 4/4 页 · 已锁定"
            choose("Range paper")
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "true"
            assert all(page.locator("#" + control).is_disabled() for control in controls)
            choose("First paper")
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "false"
            assert all(page.locator("#" + control).is_enabled() for control in controls)
            before_running_lock = saved(ids["First paper"])
            ControlledProvider.gates["retry"].clear()
            page.locator("#translate-current").click()
            page.locator(".streaming-marker").wait_for()
            page.locator("#translation-lock").click()
            page.wait_for_function("() => document.querySelector('#translation-lock').getAttribute('aria-pressed') === 'true'")
            page.locator("#translate-stop").wait_for(state="hidden")
            assert saved(ids["First paper"]) == before_running_lock
            assert all("完整结束" in text for text in page.locator(".reading-page .page-body").all_text_contents())
            assert all(page.locator("#" + control).is_disabled() for control in controls)
            assert page.locator("#export-translation").is_enabled()
            # A process restart must restore each paper's lock independently.
            proc.terminate()
            proc.wait(timeout=10)
            proc = subprocess.Popen(command + ["--server", "--port", str(port)], env=environment, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(150):
                try:
                    urllib.request.urlopen(url + "/api/session", timeout=.5)
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError("Server restart did not restore translation locks")
            page.reload()
            page.locator(".library-item").first.wait_for()
            assert progress("Range paper") == "已翻译 4/4 页 · 已锁定"
            assert progress("First paper") == "已翻译 2/2 页 · 已锁定"
            choose("Range paper")
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "true"
            page.locator("#translation-lock").click()
            page.wait_for_function("() => document.querySelector('#translation-lock').getAttribute('aria-pressed') === 'false'")
            assert all(page.locator("#" + control).is_enabled() for control in controls)
            choose("First paper")
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "true"
            page.locator("#translation-lock").click()
            page.wait_for_function("() => document.querySelector('#translation-lock').getAttribute('aria-pressed') === 'false'")
            ControlledProvider.gates["retry"].set()
            with ControlledProvider.lock:
                before_unlocked_translation = len(ControlledProvider.calls)
            page.locator("#translate-current").click()
            page.locator("#translate-stop").wait_for(state="hidden")
            with ControlledProvider.lock:
                assert len(ControlledProvider.calls) == before_unlocked_translation + 1
            assert page.locator("#translation-lock").get_attribute("aria-pressed") == "false"
            choose("Range paper")
            page.locator("#translation-lock").click()
            page.wait_for_function("() => document.querySelector('#translation-lock').getAttribute('aria-pressed') === 'true'")
            for width in (1440, 1100, 980, 640, 390):
                page.set_viewport_size({"width": width, "height": 950})
                toolbar = page.locator(".translation-toolbar").bounding_box()
                for control in ("translation-lock", "translate-from-current"):
                    button = page.locator("#" + control).bounding_box()
                    assert button["x"] >= toolbar["x"] and button["x"] + button["width"] <= toolbar["x"] + toolbar["width"] + 1, width
                    assert button["y"] + button["height"] <= toolbar["y"] + toolbar["height"] + 1, width
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
                page.evaluate("message => toast(message)", "《" + "长论文标题需要自动换行" * 12 + "》\n翻译已锁定，已完成译文已保存")
                notice = page.locator("#toast").bounding_box()
                assert notice["x"] >= 0 and 15 <= width - notice["x"] - notice["width"] <= 25, width
                assert 60 <= notice["y"] <= 100 and notice["height"] > 50, width
                assert page.locator("#toast").evaluate("e => e.scrollWidth <= e.clientWidth && getComputedStyle(e).whiteSpace === 'pre-line'")
            page.set_viewport_size({"width": 1440, "height": 950})
            page.evaluate("message => toast(message)", "《Range paper》\n翻译已锁定，已完成译文已保存")
            page.screenshot(path=str(test_dir / "translation-lock.png"), full_page=True)
            assert not errors, errors
            browser.close()
        print(json.dumps({"ok":True, "packaged":bool(executable), "checks":["paper-only cache", "API change preserves translations", "running job retains original config", "two background tasks", "return to streaming paper", "library navigation preserves task", "sidebar import order", "main sort independent of sidebar", "stop/delete scoped to paper", "explicit page retranslation", "process restart restores API settings", "translation after restart without reconfiguring", "range translation from first/middle/last page", "range stop and background retry", "cached range skips API calls", "sidebar completed page totals persist after reload", "per-paper translation locks", "locked controls make no API requests", "locking stops an active translation", "lock restoration after process restart", "unlock restores translation", "top-right multiline notices", "no JS errors"]}))
    finally:
        for gate in ControlledProvider.gates.values():
            gate.set()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        provider.shutdown()


if __name__ == "__main__":
    main()
