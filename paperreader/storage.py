import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

from .credentials import protect_key, unprotect_key


DATA_DIR = Path(os.environ.get("PAPERREADER_DATA_DIR", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PaperReader"))
_settings_lock = threading.Lock()


def api_settings_path():
    return (DATA_DIR / "api-settings.json").resolve()


def _read_api_settings():
    path = api_settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    saved = json.loads(raw)
    if not isinstance(saved, dict):
        raise ValueError("Invalid API settings")
    return saved


def load_api_settings():
    saved = _read_api_settings()
    # The reasoning preference can be saved before an API is configured.
    if not any(key in saved for key in ("base_url", "model", "api_key_encrypted")):
        return None
    return {"base_url": saved["base_url"], "model": saved["model"], "api_key": unprotect_key(saved["api_key_encrypted"]), "reasoning_effort": saved.get("reasoning_effort", "default")}


def load_reasoning_effort():
    return _read_api_settings().get("reasoning_effort", "default")


def save_api_settings(config):
    saved = {"version": 2, "base_url": config["base_url"], "model": config["model"], "api_key_encrypted": protect_key(config["api_key"]), "reasoning_effort": config.get("reasoning_effort", "default")}
    with _settings_lock:
        _write_api_settings(saved)


def save_reasoning_effort(effort):
    with _settings_lock:
        saved = _read_api_settings()
        saved.update(version=2, reasoning_effort=effort)
        _write_api_settings(saved)


def _write_api_settings(saved):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR, prefix="api-settings-", suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            json.dump(saved, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, api_settings_path())
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "library.db", timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, title TEXT, source TEXT, created TEXT, pages TEXT, metadata TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS translations (paper_id TEXT, page INTEGER, content TEXT, PRIMARY KEY(paper_id,page))")
    db.execute("CREATE TABLE IF NOT EXISTS translation_locks (paper_id TEXT PRIMARY KEY)")
    if "profile" in {row["name"] for row in db.execute("PRAGMA table_info(translations)")}:
        # Serialize upgrades, and retain the old variants as a local backup.
        db.execute("BEGIN IMMEDIATE")
        if "profile" in {row["name"] for row in db.execute("PRAGMA table_info(translations)")}:
            db.execute("ALTER TABLE translations RENAME TO translations_by_profile")
            db.execute("CREATE TABLE translations (paper_id TEXT, page INTEGER, content TEXT, PRIMARY KEY(paper_id,page))")
            # Old writes used INSERT OR REPLACE: the largest rowid is the most
            # recently saved variant of a page, regardless of service/model.
            db.execute("INSERT INTO translations SELECT paper_id,page,content FROM translations_by_profile WHERE rowid IN (SELECT MAX(rowid) FROM translations_by_profile GROUP BY paper_id,page)")
        db.commit()
    return db


def save_paper(paper):
    with connect() as db:
        db.execute("INSERT INTO papers VALUES (?,?,?,?,?,?)", (paper["id"], paper["title"], paper["source"], paper["created"], json.dumps(paper["pages"], ensure_ascii=False), json.dumps(paper["metadata"], ensure_ascii=False)))


def load_paper(paper_id):
    with connect() as db:
        row = db.execute("""SELECT p.*,l.paper_id IS NOT NULL AS translation_locked
                            FROM papers AS p LEFT JOIN translation_locks AS l ON l.paper_id=p.id
                            WHERE p.id=?""", (paper_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["pages"] = json.loads(result["pages"])
    result["metadata"] = json.loads(result["metadata"])
    result["translation_locked"] = bool(result["translation_locked"])
    return result


def save_metadata(paper_id, metadata):
    with connect() as db:
        db.execute("UPDATE papers SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), paper_id))


def list_papers():
    with connect() as db:
        rows = db.execute("""
            SELECT p.id,p.title,p.source,p.created,p.pages,p.metadata,
                   COALESCE(t.translated_count,0) AS translated_count,
                   l.paper_id IS NOT NULL AS translation_locked
            FROM papers AS p
            LEFT JOIN (SELECT paper_id,COUNT(*) AS translated_count
                       FROM translations GROUP BY paper_id) AS t ON t.paper_id=p.id
            LEFT JOIN translation_locks AS l ON l.paper_id=p.id
            ORDER BY p.created DESC
        """).fetchall()
    return [{"id": r["id"], "title": r["title"], "source": r["source"], "created": r["created"], "page_count": len(json.loads(r["pages"])), "translated_count": r["translated_count"], "translation_locked": bool(r["translation_locked"]), "metadata": {k: v for k, v in json.loads(r["metadata"]).items() if k != "reading_layout"}} for r in rows]


def translation_locked(paper_id):
    with connect() as db:
        return db.execute("SELECT 1 FROM translation_locks WHERE paper_id=?", (paper_id,)).fetchone() is not None


def set_translation_lock(paper_id, locked):
    with connect() as db:
        if locked:
            db.execute("INSERT OR IGNORE INTO translation_locks SELECT id FROM papers WHERE id=?", (paper_id,))
        else:
            db.execute("DELETE FROM translation_locks WHERE paper_id=?", (paper_id,))


def translations(paper_id):
    with connect() as db:
        rows = db.execute("SELECT page,content FROM translations WHERE paper_id=? ORDER BY page", (paper_id,)).fetchall()
    return {str(r["page"]): r["content"] for r in rows}


def save_translation(paper_id, page, content):
    with connect() as db:
        # Do not resurrect translations if the paper was removed during a request.
        if db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
            db.execute("INSERT OR REPLACE INTO translations VALUES (?,?,?)", (paper_id, page, content))


def delete_paper(paper_id):
    with connect() as db:
        db.execute("DELETE FROM translation_locks WHERE paper_id=?", (paper_id,))
        db.execute("DELETE FROM translations WHERE paper_id=?", (paper_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='translations_by_profile'").fetchone():
            db.execute("DELETE FROM translations_by_profile WHERE paper_id=?", (paper_id,))
        db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    (DATA_DIR / f"{paper_id}.pdf").unlink(missing_ok=True)
