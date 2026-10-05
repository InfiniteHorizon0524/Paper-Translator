import json
import sqlite3
import uuid
from unittest.mock import patch

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from paperreader import app as app_module, llm, storage
from paperreader.documents import extract_pdf, normalize_arxiv, retrieve
from fastapi import HTTPException


CONFIG = {"base_url": "https://example.com/v1", "api_key": "secret-test-key", "model": "test-model", "reasoning_effort": "default"}


def pdf_bytes(texts=None):
    doc = pymupdf.open()
    for text in texts or ["Sample Research Paper\nAbstract\nWe propose a transformer attention mechanism. Attention improves translation accuracy.", "Results\nThe attention method improves accuracy by 12 percent. This approach has limitations on small datasets."]:
        page = doc.new_page()
        page.insert_text((50, 60), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    with TestClient(app_module.app) as c:
        token = c.get("/api/session").json()["token"]
        c.headers["X-PaperReader-Token"] = token
        yield c


@pytest.fixture
def paper(client):
    response = client.post("/api/papers/upload", files={"file": ("sample.pdf", pdf_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def events(response):
    output = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        output.append((lines[0][7:], json.loads(lines[1][6:])))
    return output


def test_import_read_render_download_delete(client, paper):
    pid = paper["id"]
    assert len(paper["pages"]) == 2
    assert "attention" in paper["pages"][0]
    assert client.get("/api/papers").json()[0]["page_count"] == 2
    image = client.get(f"/api/papers/{pid}/page/1/image")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert client.get(f"/api/papers/{pid}/page/3/image").status_code == 400
    assert client.get(f"/api/papers/{pid}/pdf").content.startswith(b"%PDF-")
    assert client.delete(f"/api/papers/{pid}").status_code == 200
    assert client.get(f"/api/papers/{pid}").status_code == 404
    assert not (storage.DATA_DIR / f"{pid}.pdf").exists()


def test_library_counts_completed_pages_per_paper(client, paper):
    pid = paper["id"]
    other = client.post("/api/papers/upload", files={"file": ("other.pdf", pdf_bytes(), "application/pdf")}).json()

    def counts():
        return {item["id"]: item["translated_count"] for item in client.get("/api/papers").json()}

    assert counts() == {pid: 0, other["id"]: 0}
    storage.save_translation(pid, 2, "第二页译文")
    assert counts() == {pid: 1, other["id"]: 0}
    storage.save_translation(pid, 2, "第二页重译")
    assert counts() == {pid: 1, other["id"]: 0}
    storage.save_translation(pid, 1, "第一页译文")
    assert counts() == {pid: 2, other["id"]: 0}
    assert "reading_layout" not in client.get("/api/papers").json()[0]["metadata"]
    client.delete(f"/api/papers/{pid}")
    assert counts() == {other["id"]: 0}


def test_translation_lock_defaults_persists_and_is_isolated(client, paper):
    pid = paper["id"]
    assert paper["translation_locked"] is False
    # An existing library from before translation locks also starts unlocked.
    with storage.connect() as db:
        db.execute("DROP TABLE translation_locks")
    assert client.get(f"/api/papers/{pid}").json()["translation_locked"] is False
    other = client.post("/api/papers/upload", files={"file": ("other.pdf", pdf_bytes(), "application/pdf")}).json()
    url = f"/api/papers/{pid}/translation-lock"
    assert client.patch(url, json={"locked": True}).json() == {"locked": True}
    assert client.get(f"/api/papers/{pid}").json()["translation_locked"] is True
    states = {item["id"]: item["translation_locked"] for item in client.get("/api/papers").json()}
    assert states == {pid: True, other["id"]: False}
    storage.save_metadata(pid, paper["metadata"])
    assert storage.load_paper(pid)["translation_locked"] is True
    assert client.patch(url, json={"locked": False}).json() == {"locked": False}
    assert storage.load_paper(pid)["translation_locked"] is False
    client.patch(url, json={"locked": True})
    client.delete(f"/api/papers/{pid}")
    assert storage.translation_locked(pid) is False
    storage.set_translation_lock(pid, True)
    assert storage.translation_locked(pid) is False
    assert client.patch(url, json={"locked": True}).status_code == 404


@pytest.mark.parametrize("pages,force", [([1], False), ([1], True), ([1, 2], False), ([1, 2], True)])
def test_locked_translation_never_calls_provider(client, paper, monkeypatch, pages, force):
    pid = paper["id"]
    storage.save_translation(pid, 1, "满意的译文")
    client.patch(f"/api/papers/{pid}/translation-lock", json={"locked": True})

    async def no_call(*args):
        raise AssertionError("A locked paper must never call the translation provider")
        yield ""

    monkeypatch.setattr(app_module, "completion", no_call)
    response = client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": pages, "force": force})
    assert response.status_code == 409 and "已锁定" in response.json()["detail"]
    assert storage.translations(pid) == {"1": "满意的译文"}
    client.patch(f"/api/papers/{pid}/translation-lock", json={"locked": False})
    monkeypatch.setattr(app_module, "completion", mock_completion)
    assert events(client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": pages, "force": force}))[-1][0] == "done"


@pytest.mark.parametrize("boundary", ["page", "chunk"])
def test_lock_during_translation_blocks_next_provider_call(client, paper, monkeypatch, boundary):
    pid = paper["id"]
    calls = []
    if boundary == "chunk":
        real_chunks = app_module.translation_chunks
        monkeypatch.setattr(app_module, "translation_chunks", lambda text: real_chunks(text, limit=50))

    async def lock_after_first_call(config, messages):
        calls.append(messages)
        yield "已完成的片段"
        storage.set_translation_lock(pid, True)

    monkeypatch.setattr(app_module, "completion", lock_after_first_call)
    parsed = events(client.post(f"/api/papers/{pid}/translate", json={"config": CONFIG, "pages": [1, 2]}))
    assert len(calls) == 1
    assert parsed[-1][0] == "error" and "已锁定" in parsed[-1][1]["message"]
    assert storage.translations(pid) == ({"1": "已完成的片段"} if boundary == "page" else {})


def test_invalid_pdf_scan_and_encryption(client):
    assert client.post("/api/papers/upload", files={"file": ("fake.pdf", b"not a pdf")}).status_code == 400
    assert client.post("/api/papers/upload", files={"file": ("scan.pdf", pdf_bytes([" "]))}).status_code == 422
    doc = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    assert client.post("/api/papers/upload", files={"file": ("locked.pdf", data)}).status_code == 400
    assert client.post("/api/papers/upload", files={"file": ("corrupt.pdf", b"%PDF-broken")}).status_code == 400


@pytest.mark.parametrize("value,expected", [("1706.03762", "1706.03762"),("arXiv:1706.03762v2", "1706.03762v2"),("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001")])
def test_arxiv_formats(value, expected):
    assert normalize_arxiv(value) == expected


@pytest.mark.parametrize("value", ["https://example.com/pdf/1706.03762", "https://arxiv.org/../foo", "../secret", "1706.03762/../../foo", "1706.03762?x=y"])
def test_arxiv_invalid(value):
    with pytest.raises(HTTPException):
        normalize_arxiv(value)


def test_arxiv_download_and_metadata(client):
    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Real arXiv Title</title><author><name>Sample Author</name></author><summary>Sample abstract.</summary></entry></feed>'
    def respond(request):
        if request.url.host == "arxiv.org":
            assert request.url.path == "/pdf/1706.03762"
            return httpx.Response(200, content=pdf_bytes())
        return httpx.Response(200, text=xml)
    real_client = httpx.AsyncClient
    with patch.object(app_module.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
        response = client.post("/api/papers/arxiv", json={"arxiv_id":"1706.03762"})
    assert response.status_code == 200
    assert response.json()["title"] == "Real arXiv Title"
    assert response.json()["metadata"]["author"] == "Sample Author"


async def mock_completion(config, messages):
    if "Convert the research question" in messages[0].get("content", ""):
        yield "attention transformer translation accuracy"
    elif "学术论文译者" in messages[0].get("content", ""):
        yield "## 中文译文\n\n注意力机制"
        yield "提高了翻译准确率。"
    else:
        yield "论文提出了注意力机制"
        yield "。[第1页] 实验提高了准确率。[第2页]"


def test_translation_cache_follows_paper_across_api_settings(client, paper, monkeypatch):
    monkeypatch.setattr(app_module, "completion", mock_completion)
    pid = paper["id"]
    response = client.post(f"/api/papers/{pid}/translate", json={"config":CONFIG,"pages":[1,2]})
    parsed = events(response)
    assert [e for e,d in parsed].count("page_done") == 2
    assert parsed[-1][0] == "done"
    saved = client.post(f"/api/papers/{pid}/translations", json={"config":CONFIG}).json()
    assert "注意力机制" in saved["1"]
    response = client.post(f"/api/papers/{pid}/translate", json={"config":CONFIG,"pages":[1]})
    assert events(response)[0][1]["cached"] is True
    other_key = dict(CONFIG, api_key="new-key")
    assert client.post(f"/api/papers/{pid}/translations", json={"config":other_key}).json() == saved
    other_model = dict(CONFIG, model="different-model")
    assert client.post(f"/api/papers/{pid}/translations", json={"config":other_model}).json() == saved
    other_service = dict(other_model, base_url="https://other.example.com/v1")
    assert client.get(f"/api/papers/{pid}/translations").json() == saved
    async def no_call(*args):
        raise AssertionError("Changing API settings must reuse the paper's translations")
        yield ""
    monkeypatch.setattr(app_module, "completion", no_call)
    cached = events(client.post(f"/api/papers/{pid}/translate", json={"config":other_service,"pages":[1,2]}))
    assert all(data["cached"] for event, data in cached if event == "page_done")
    other_paper = client.post("/api/papers/upload", files={"file":("other.pdf",pdf_bytes(),"application/pdf")}).json()
    assert client.get(f"/api/papers/{other_paper['id']}/translations").json() == {}
    assert client.post(f"/api/papers/{pid}/translate", json={"config":CONFIG,"pages":[3]}).status_code == 400


def test_legacy_translation_migration_preserves_latest_pages_and_backup(client, paper):
    pid = paper["id"]
    with sqlite3.connect(storage.DATA_DIR / "library.db") as db:
        db.execute("DROP TABLE translations")
        db.execute("CREATE TABLE translations (paper_id TEXT, page INTEGER, profile TEXT, content TEXT, PRIMARY KEY(paper_id,page,profile))")
        db.executemany("INSERT INTO translations VALUES (?,?,?,?)", [
            (pid, 1, "first-service", "较早的译文"),
            (pid, 2, "first-service", "第二页译文"),
            (pid, 1, "second-service", "最近的译文"),
        ])
        db.execute("INSERT OR REPLACE INTO translations VALUES (?,?,?,?)", (pid, 1, "first-service", "最后重译的版本"))
    expected = {"1":"最后重译的版本", "2":"第二页译文"}
    assert client.get("/api/papers").json()[0]["translated_count"] == 2
    assert client.get(f"/api/papers/{pid}/translations").json() == expected
    assert storage.translations(pid) == expected  # A second connection does not re-migrate.
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM translations_by_profile").fetchone()[0] == 3
        assert "profile" not in {row["name"] for row in db.execute("PRAGMA table_info(translations)")}
    storage.save_translation(pid, 1, "新版本")
    assert storage.translations(pid) == {"1":"新版本", "2":"第二页译文"}
    assert client.delete(f"/api/papers/{pid}").status_code == 200
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM translations_by_profile").fetchone()[0] == 0
    storage.save_translation(pid, 1, "不能复活已删除的论文")
    assert storage.translations(pid) == {}


def test_explicit_retranslation_overwrites_only_selected_paper_page(client, paper, monkeypatch):
    pid = paper["id"]
    storage.save_translation(pid, 1, "原第一页")
    storage.save_translation(pid, 2, "原第二页")
    async def replacement(config, messages):
        assert config.model == "new-model"
        yield "重新翻译的第一页"
    monkeypatch.setattr(app_module, "completion", replacement)
    config = dict(CONFIG, model="new-model", base_url="https://new.example.com/v1")
    response = client.post(f"/api/papers/{pid}/translate", json={"config":config, "pages":[1], "force":True})
    assert next(data for event, data in events(response) if event == "page_done")["cached"] is False
    assert client.get(f"/api/papers/{pid}/translations").json() == {"1":"重新翻译的第一页", "2":"原第二页"}


def test_partial_translation_never_saved(client, paper, monkeypatch):
    async def fail(config, messages):
        yield "未完成的译文"
        raise llm.ProviderError("API 请求超时")
    monkeypatch.setattr(app_module,"completion",fail)
    response = client.post(f"/api/papers/{paper['id']}/translate", json={"config":CONFIG,"pages":[1]})
    assert events(response)[-1][0] == "error"
    assert client.post(f"/api/papers/{paper['id']}/translations", json={"config":CONFIG}).json() == {}
    assert client.get("/api/papers").json()[0]["translated_count"] == 0


def test_latex_and_tables_survive_stream_and_saved_translation(client, paper, monkeypatch):
    formula = r"行内 $x_i^2$。" + "\n\n$$\n" + r"\begin{aligned} a &= \frac{1}{2} \\ b &= \sqrt{x} \end{aligned}\tag{1}" + "\n$$\n\n| 方法 | 准确率 |\n| --- | ---: |\n| 本文 | 92% |"
    calls = []
    async def math_completion(config, messages):
        calls.append(messages)
        for start in range(0, len(formula), 3):
            yield formula[start:start + 3]
    monkeypatch.setattr(app_module, "completion", math_completion)
    pid = paper["id"]
    parsed = events(client.post(f"/api/papers/{pid}/translate", json={"config":CONFIG,"pages":[1]}))
    assert "".join(d["text"] for e,d in parsed if e == "delta") == formula
    assert next(d["text"] for e,d in parsed if e == "page_done") == formula
    assert client.post(f"/api/papers/{pid}/translations", json={"config":CONFIG}).json()["1"] == formula
    assert r"\tag{编号}" in calls[0][0]["content"]
    assert "Markdown 管道表格" in calls[0][0]["content"]
    cached = events(client.post(f"/api/papers/{pid}/translate", json={"config":CONFIG,"pages":[1]}))
    assert cached[0][1]["cached"] is True and cached[0][1]["text"] == formula
    assert len(calls) == 1


def test_chinese_question_retrieval_citations_and_followup(client, paper, monkeypatch):
    monkeypatch.setattr(app_module,"completion",mock_completion)
    monkeypatch.setattr(llm,"completion",mock_completion)
    response = client.post(f"/api/papers/{paper['id']}/chat", json={"config":CONFIG,"question":"核心贡献是什么？","history":[{"role":"user","content":"介绍注意力"},{"role":"assistant","content":"它是一种机制"}]})
    parsed = events(response)
    sources = next(d["sources"] for e,d in parsed if e == "sources")
    assert {s["page"] for s in sources} == {1,2}
    answer = "".join(d["text"] for e,d in parsed if e == "delta")
    assert "[第1页]" in answer and "[第2页]" in answer
    assert parsed[-1][0] == "done"


def test_local_access_and_no_key_leaks(client, paper):
    response = client.get("/api/papers", headers={"X-PaperReader-Token":"wrong"})
    assert response.status_code == 403
    response = client.get("/api/session", headers={"Host":"malicious.example"})
    assert response.status_code == 403
    config = dict(CONFIG, base_url="invalid-url")
    response = client.post("/api/config/test", json={"config":config})
    assert response.status_code == 422 and CONFIG["api_key"] not in response.text
    assert client.get(f"/api/papers/../../anything").status_code != 200


def test_api_settings_saved_encrypted_and_restored(client):
    initial = client.get("/api/config").json()
    assert initial["config"] is None
    assert initial["path"] == str(storage.DATA_DIR / "api-settings.json")
    config = dict(CONFIG, base_url="https://example.com/v1/", model=" test-model ", api_key=" secret-test-key ")
    response = client.post("/api/config", json={"config":config})
    assert response.status_code == 200 and response.json()["ok"]
    saved_file = storage.api_settings_path().read_text(encoding="utf-8")
    assert CONFIG["api_key"] not in saved_file
    assert "api_key" not in json.loads(saved_file)
    assert json.loads(saved_file)["api_key_encrypted"]
    assert storage.load_api_settings() == CONFIG
    assert client.get("/api/config").json()["config"] == CONFIG
    denied = client.get("/api/config", headers={"X-PaperReader-Token":"wrong"})
    assert denied.status_code == 403 and CONFIG["api_key"] not in denied.text
    rejected = client.post("/api/config", json={"config":dict(CONFIG, base_url="invalid")})
    assert rejected.status_code == 422 and CONFIG["api_key"] not in rejected.text
    assert storage.load_api_settings() == CONFIG


def test_failed_api_settings_save_keeps_previous_file(client, monkeypatch):
    assert client.post("/api/config", json={"config":CONFIG}).status_code == 200
    previous = storage.api_settings_path().read_bytes()
    def failure(*args):
        raise OSError("secret-test-key")
    monkeypatch.setattr(storage.os, "replace", failure)
    response = client.post("/api/config", json={"config":dict(CONFIG, model="other-model")})
    assert response.status_code == 500 and CONFIG["api_key"] not in response.text
    assert storage.api_settings_path().read_bytes() == previous
    assert not list(storage.DATA_DIR.glob("api-settings-*.tmp"))


@pytest.mark.parametrize("effort", ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_saved_and_restored(client, effort):
    config = dict(CONFIG, reasoning_effort=effort)
    assert client.post("/api/config", json={"config": config}).status_code == 200
    assert client.get("/api/config").json()["config"] == config
    assert storage.load_api_settings() == config
    saved = storage.api_settings_path().read_text(encoding="utf-8")
    assert json.loads(saved)["reasoning_effort"] == effort
    assert CONFIG["api_key"] not in saved


@pytest.mark.parametrize("effort", ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_autosave_preserves_api_credentials(client, effort):
    storage.save_api_settings(CONFIG)
    previous = json.loads(storage.api_settings_path().read_text(encoding="utf-8"))
    response = client.patch("/api/config/reasoning-effort", json={"reasoning_effort": effort})
    assert response.status_code == 200 and response.json()["ok"]
    current = json.loads(storage.api_settings_path().read_text(encoding="utf-8"))
    assert current == dict(previous, reasoning_effort=effort)
    restored = client.get("/api/config").json()
    assert restored["reasoning_effort"] == effort
    assert restored["config"] == dict(CONFIG, reasoning_effort=effort)


@pytest.mark.parametrize("effort", ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_saved_before_api_configuration(client, effort):
    assert client.patch("/api/config/reasoning-effort", json={"reasoning_effort": effort}).status_code == 200
    restored = client.get("/api/config").json()
    assert restored["config"] is None and "error" not in restored
    assert restored["reasoning_effort"] == effort
    assert storage.load_api_settings() is None
    assert storage.load_reasoning_effort() == effort


@pytest.mark.parametrize("effort", ["ultra", "", 123, None])
def test_invalid_reasoning_autosave_keeps_previous_value(client, effort):
    storage.save_reasoning_effort("high")
    previous = storage.api_settings_path().read_bytes()
    response = client.patch("/api/config/reasoning-effort", json={"reasoning_effort": effort})
    assert response.status_code == 422
    assert storage.api_settings_path().read_bytes() == previous


def test_reasoning_autosave_requires_session_token(client):
    response = client.patch("/api/config/reasoning-effort", json={"reasoning_effort": "high"}, headers={"X-PaperReader-Token": "wrong"})
    assert response.status_code == 403
    assert not storage.api_settings_path().exists()


def test_failed_reasoning_autosave_keeps_previous_file(client, monkeypatch):
    storage.save_reasoning_effort("high")
    previous = storage.api_settings_path().read_bytes()
    def failure(*args):
        raise OSError("secret-test-key")
    monkeypatch.setattr(storage.os, "replace", failure)
    response = client.patch("/api/config/reasoning-effort", json={"reasoning_effort": "max"})
    assert response.status_code == 500 and CONFIG["api_key"] not in response.text
    assert storage.api_settings_path().read_bytes() == previous
    assert not list(storage.DATA_DIR.glob("api-settings-*.tmp"))


def test_legacy_api_settings_use_service_default(client):
    storage.save_api_settings(dict(CONFIG, reasoning_effort="high"))
    path = storage.api_settings_path()
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("reasoning_effort")
    legacy["version"] = 1
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    assert storage.load_api_settings() == CONFIG
    assert client.get("/api/config").json()["config"] == CONFIG
    assert path.read_bytes() == before


@pytest.mark.parametrize("effort", ["ultra", "", 123, None])
def test_invalid_reasoning_effort_is_rejected_without_key_leak(client, effort):
    response = client.post("/api/config", json={"config": dict(CONFIG, reasoning_effort=effort)})
    assert response.status_code == 422 and CONFIG["api_key"] not in response.text
    assert client.get("/api/config").json()["config"] is None


@pytest.mark.parametrize("effort", [None, "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_provider_receives_selected_reasoning_effort(effort):
    import asyncio
    calls = []
    config = dict(CONFIG)
    if effort is None:
        config.pop("reasoning_effort")
    else:
        config["reasoning_effort"] = effort
    stream = 'data: {"choices":[{"delta":{"content":"回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    real_client = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if effort is None or effort == "default":
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == effort
        return httpx.Response(200, text=stream)

    async def collect():
        with patch.object(llm.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            return "".join([delta async for delta in llm.completion(llm.APIConfig(**config), [{"role": "user", "content": "hello"}])])

    assert asyncio.run(collect()) == "回答"
    assert len(calls) == 1


def test_reasoning_effort_reaches_translation_keywords_and_assistant(client, paper):
    calls = []
    real_client = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["reasoning_effort"] == "high"
        delta = {"choices": [{"delta": {"content": "attention transformer 中文回答[第1页]"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(delta) + "\n\ndata: [DONE]\n\n")

    config = dict(CONFIG, reasoning_effort="high")
    with patch.object(llm.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
        assert events(client.post(f"/api/papers/{paper['id']}/translate", json={"config": config, "pages": [1]}))[-1][0] == "done"
        assert events(client.post(f"/api/papers/{paper['id']}/chat", json={"config": config, "question": "这篇论文的贡献是什么？", "history": []}))[-1][0] == "done"
    assert len(calls) == 3
    assert "学术论文译者" in calls[0]["messages"][0]["content"]
    assert "Convert the research question" in calls[1]["messages"][0]["content"]
    assert "学术阅读助手" in calls[2]["messages"][0]["content"]


@pytest.mark.parametrize("status", [400, 422])
def test_rejected_reasoning_effort_reports_safe_hint_without_retry(status):
    import asyncio
    calls = []
    real_client = httpx.AsyncClient

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, text="unsupported reasoning_effort secret-test-key")

    async def collect():
        with patch.object(llm.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            return [delta async for delta in llm.completion(llm.APIConfig(**dict(CONFIG, reasoning_effort="high")), [{"role": "user", "content": "hello"}])]

    with pytest.raises(llm.ProviderError) as exc:
        asyncio.run(collect())
    assert "思考强度" in str(exc.value) and "服务默认" in str(exc.value)
    assert CONFIG["api_key"] not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("content", ["broken secret-test-key", '{"base_url":"https://example.com/v1","model":"test-model","api_key_encrypted":"secret-test-key"}'])
def test_unreadable_settings_do_not_block_library_or_expose_key(client, content):
    storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage.api_settings_path().write_text(content, encoding="utf-8")
    response = client.get("/api/config")
    assert response.status_code == 200 and response.json()["config"] is None
    assert response.json()["error"] and CONFIG["api_key"] not in response.text
    assert client.get("/api/papers").status_code == 200
    assert client.post("/api/config", json={"config":CONFIG}).status_code == 200
    assert client.get("/api/config").json()["config"] == CONFIG


def test_provider_stream_errors_and_secret_safe_messages():
    import asyncio
    real_client = httpx.AsyncClient
    async def collect(status, stream):
        def respond(request):
            assert request.headers["Authorization"] == "Bearer secret-test-key"
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(status, text=stream)
        with patch.object(llm.httpx,"AsyncClient",side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond),**kwargs)):
            return "".join([delta async for delta in llm.completion(llm.APIConfig(**CONFIG),[{"role":"user","content":"hello"}])])
    good = 'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    assert asyncio.run(collect(200,good)) == "你好"
    for status, stream in [(401,"secret-test-key"),(429,"error"),(200,good.split('data: {"choices":[{"delta":{},')[0]),(200,good.replace('"stop"','"length"'))]:
        with pytest.raises(llm.ProviderError) as exc:
            asyncio.run(collect(status,stream))
        assert CONFIG["api_key"] not in str(exc.value)


def test_retrieval_long_paper_finds_relevant_middle():
    pages = [("Unrelated background geology rocks. " * 200) for _ in range(20)]
    pages[10] = "Quantum entanglement experiment coherence measurement." * 40
    sources = retrieve(pages,"quantum entanglement coherence",max_chars=8000)
    assert 11 in {s["page"] for s in sources}
    assert sum(len(s["text"]) for s in sources) <= 8000
