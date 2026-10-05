"""Verify automatic preference saving across a real server and browser restart."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CONFIG = {"base_url": "https://example.com/v1", "model": "test-model", "api_key": "settings-test-key"}


@contextmanager
def running_server(data_dir):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ, PAPERREADER_DATA_DIR=str(data_dir), PYTHONPATH=str(ROOT / ".deps"))
    executable = os.environ.get("PAPERREADER_TEST_EXE")
    command = [executable] if executable else [sys.executable, str(ROOT / "launcher.py")]
    process = subprocess.Popen(command + ["--server", "--port", str(port)],
                               env=environment, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode(errors="replace"))
            try:
                urllib.request.urlopen(url + "/api/session", timeout=.5).close()
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("Server did not start")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def select_effort(page, effort):
    with page.expect_response(lambda response: response.url.endswith("/api/config/reasoning-effort") and response.request.method == "PATCH") as saved:
        page.locator("#reasoning-effort").select_option(effort)
        page.locator("[data-close='settings-dialog']").click()
    assert saved.value.status == 200
    page.wait_for_function("() => !document.querySelector('#reasoning-effort').disabled")


@pytest.mark.parametrize("configured", [False, True])
def test_reasoning_autosave_survives_close_and_restart(tmp_path, configured):
    data_dir = tmp_path / "library"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        with running_server(data_dir) as url:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            if configured:
                token = page.evaluate("state.token")
                response = page.request.post(url + "/api/config", data={"config": CONFIG}, headers={"X-PaperReader-Token": token})
                assert response.status == 200
                page.reload(wait_until="networkidle")
            page.locator("#settings-top").click()
            assert page.locator("#reasoning-effort").input_value() == "default"
            # Draft changes to other fields must not be saved by this selector.
            page.locator("#model").fill("unsaved-model")
            page.locator("#api-key").fill("unsaved-key")
            select_effort(page, "max")
            page.locator("#settings-top").click()
            assert page.locator("#reasoning-effort").input_value() == "max"
            select_effort(page, "none")
            page.reload(wait_until="networkidle")
            page.locator("#settings-top").click()
            assert page.locator("#reasoning-effort").input_value() == "none"
            # Failed saving restores the previous selection and reports the error.
            page.route("**/api/config/reasoning-effort", lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "保存失败"})))
            page.locator("#reasoning-effort").select_option("high")
            page.wait_for_function("() => !document.querySelector('#reasoning-effort').disabled")
            assert page.locator("#reasoning-effort").input_value() == "none"
            assert page.locator("#settings-error").inner_text() == "保存失败"
            page.close()
        # A fresh browser context at a new port cannot recover from localStorage.
        with running_server(data_dir) as url:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            page.locator("#settings-top").click()
            assert page.locator("#reasoning-effort").input_value() == "none"
            assert page.locator("#model").input_value() == (CONFIG["model"] if configured else "")
            assert page.locator("#api-key").input_value() == (CONFIG["api_key"] if configured else "")
            assert page.evaluate("state.config?.reasoning_effort ?? null") == ("none" if configured else None)
            assert "settings-test-key" not in page.evaluate("JSON.stringify(localStorage)")
            page.close()
        browser.close()
