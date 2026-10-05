"""Validate the exact source allowlist used for public distribution."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("paperreader", "static", "assets", "scripts", "tests", "installer", ".github")
SOURCE_FILES = ("launcher.py", "requirements.txt", "requirements-dev.txt", "README.md",
                "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "PaperReader.spec", ".gitignore")
PRIVATE = re.compile(
    r"\b[A-Za-z]:[\\/]|/(?:Users|home)/[A-Za-z0-9_.-]+|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}|\bAKIA[A-Z0-9]{16}\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def public_files():
    files = [ROOT / name for name in SOURCE_FILES]
    for directory in SOURCE_DIRS:
        files.extend(file for file in (ROOT / directory).rglob("*")
                     if file.is_file() and "__pycache__" not in file.parts)
    return sorted(files)


def check(files):
    problems = []
    for file in files:
        name = file.relative_to(ROOT).as_posix()
        if file.suffix.lower() in {".pdf", ".db", ".sqlite", ".exe", ".zip", ".pyc"} or file.name in {"api-settings.json", ".env"}:
            problems.append(name)
            continue
        data = file.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if file.suffix.lower() not in {".ico", ".woff", ".woff2", ".ttf"}:
                problems.append(name)
            continue
        if PRIVATE.search(text):
            problems.append(name)
    if problems:
        # Report filenames only: never echo a matched credential or private path.
        raise SystemExit("Privacy check failed: " + ", ".join(problems))


def manifest(files):
    return [{"path": file.relative_to(ROOT).as_posix(), "bytes": file.stat().st_size,
             "sha": hashlib.sha1(b"blob " + str(file.stat().st_size).encode() + b"\0" + file.read_bytes()).hexdigest()}
            for file in files]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    files = public_files()
    check(files)
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest(files)), encoding="utf-8")
    print(json.dumps({"ok": True, "files": len(files), "bytes": sum(f.stat().st_size for f in files)}))


if __name__ == "__main__":
    main()
