"""Windows desktop entry point. The frozen build bundles Python and dependencies."""
import argparse
import ctypes
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Source-only convenience; the packaged application never requires this folder.
if not getattr(sys, "frozen", False):
    dependencies = Path(__file__).resolve().parent / ".deps"
    if dependencies.is_dir():
        sys.path.insert(0, str(dependencies))


def main():
    parser = argparse.ArgumentParser(description="PaperReader desktop")
    parser.add_argument("--browser", action="store_true", help="Open the default browser instead of a desktop window")
    parser.add_argument("--server", action="store_true", help="Run only the local server (for development)")
    parser.add_argument("--port", type=int, default=0, help="Local port; 0 selects an available port")
    parser.add_argument("--smoke-test", action="store_true", help="Verify the packaged server and exit")
    parser.add_argument("--desktop-smoke-test", action="store_true", help="Verify a hidden native WebView2 window and exit")
    args = parser.parse_args()
    from paperreader import storage
    from paperreader.app import app
    import uvicorn

    storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(storage.DATA_DIR / "app.log"), level=logging.WARNING, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, loop="asyncio", http="h11")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("本地服务启动失败，请查看 app.log。")
        time.sleep(.05)
    else:
        raise RuntimeError("本地服务启动超时。")
    try:
        if args.desktop_smoke_test:
            import json
            import webview
            result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
            window = webview.create_window("PaperReader verification", url, width=1200, height=850, hidden=True)
            def verify_native():
                try:
                    for _ in range(100):
                        if window.evaluate_js("Boolean(window.document.querySelector('#welcome h1')) && Boolean(state.token)"):
                            break
                        time.sleep(.1)
                    else:
                        raise RuntimeError("Desktop UI did not initialize")
                    result.update(ok=True, title=window.evaluate_js("document.title"), renderer="edgechromium")
                except Exception as error:
                    result["error"] = str(error)
                finally:
                    (storage.DATA_DIR / "desktop-smoke-test.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
                    window.destroy()
            window.events.loaded += verify_native
            watchdog = threading.Timer(30, lambda: window.destroy())
            watchdog.daemon = True
            watchdog.start()
            webview.start(gui="edgechromium", private_mode=True)
            watchdog.cancel()
            if not result["ok"]:
                raise RuntimeError(result.get("error", "Desktop test timed out"))
        elif args.smoke_test:
            import json
            session = json.loads(urllib.request.urlopen(url + "/api/session").read())
            req = urllib.request.Request(url + "/api/papers", headers={"X-PaperReader-Token": session["token"]})
            assert isinstance(json.loads(urllib.request.urlopen(req).read()), list)
            assert b"PaperReader" in urllib.request.urlopen(url).read()
            (storage.DATA_DIR / "smoke-test.json").write_text(json.dumps({"ok": True, "version": session["version"], "frozen": bool(getattr(sys,"frozen",False)), "python": sys.version}), encoding="utf-8")
        elif args.server:
            print(url, flush=True)
            while thread.is_alive():
                time.sleep(.5)
        elif args.browser:
            webbrowser.open(url)
            if sys.platform == "win32":
                ctypes.windll.user32.MessageBoxW(None, "PaperReader 已在浏览器打开。\n使用结束后点击“确定”退出本地服务。", "PaperReader", 0x40)
            else:
                input("Press Enter to close PaperReader…")
        else:
            try:
                import webview
                webview.settings["ALLOW_DOWNLOADS"] = True
                webview.create_window("PaperReader · 中文论文工作台", url, width=1440, height=950, min_size=(980, 680), background_color="#fdfcf8")
                webview.start(gui="edgechromium", private_mode=True)
            except Exception:
                logging.exception("Desktop WebView failed; falling back to browser")
                webbrowser.open(url)
                if sys.platform == "win32":
                    ctypes.windll.user32.MessageBoxW(None, "桌面窗口无法启动，已在默认浏览器打开。\n使用结束后点击“确定”退出。\n安装 Microsoft Edge WebView2 Runtime 可启用桌面窗口。", "PaperReader", 0x40)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.exception("Application failed")
        if sys.platform == "win32" and not any(a in sys.argv for a in ("--smoke-test", "--desktop-smoke-test", "--server")):
            ctypes.windll.user32.MessageBoxW(None, f"PaperReader 无法启动：\n{exc}", "PaperReader", 0x10)
        raise
