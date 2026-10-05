"""Package an existing verified desktop build with docs, source and checksums."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperreader import __version__


def package_release(distribution=None):
    release = ROOT / "release"
    distribution = (Path(distribution) if distribution else ROOT / "dist" / "PaperReader").resolve()
    if not (distribution / "PaperReader.exe").exists():
        raise RuntimeError("Run scripts/build.py before packaging a release.")
    # Stale UI files would silently ship a different product than the source.
    for source in (ROOT / "static").rglob("*"):
        if source.is_file():
            packaged = distribution / "_internal" / "static" / source.relative_to(ROOT / "static")
            if not packaged.is_file() or source.read_bytes() != packaged.read_bytes():
                raise RuntimeError(f"Rebuild required: packaged UI differs from {source.name}")
    release.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "README.md", distribution / "使用说明.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", distribution)
    validation = release / "VALIDATION.md"
    if validation.exists():
        shutil.copy2(validation, distribution / "验证记录.md")
    licenses = distribution / "_internal" / "licenses"
    for info in (ROOT / ".deps").glob("*.dist-info"):
        for file in info.rglob("*"):
            if file.is_file() and (any(term in file.name.lower() for term in ("license", "copying", "notice")) or "licenses" in file.parts):
                path = licenses / info.name / file.relative_to(info)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, path)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        licenses.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    shutil.make_archive(str(release / f"PaperReader-{__version__}-Windows-x64-portable"), "zip", distribution.parent, distribution.name)
    with zipfile.ZipFile(release / f"PaperReader-{__version__}-source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in ["paperreader", "static", "assets", "scripts", "tests", "installer", ".github"]:
            for file in (ROOT / item).rglob("*"):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, file.relative_to(ROOT))
        for name in ["launcher.py", "requirements.txt", "requirements-dev.txt", "README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "PaperReader.spec", ".gitignore"]:
            archive.write(ROOT / name, name)
        if validation.exists():
            archive.write(validation, "VALIDATION.md")
    compiler = ROOT / ".tools" / "inno" / "ISCC.exe"
    if not compiler.exists():
        installed = shutil.which("ISCC")
        compiler = Path(installed) if installed else Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe"
    if compiler.exists():
        subprocess.run([str(compiler), "/Qp", f"/DAppVersion={__version__}", f"/DDistributionDir={distribution}", str(ROOT / "installer" / "PaperReader.iss")], check=True)
    else:
        print("Portable build complete. Install Inno Setup or place it in .tools/inno to create the setup EXE.")
    manifest = []
    for file in sorted(release.iterdir()):
        if file.suffix in {".exe", ".zip"} and file.name.startswith(f"PaperReader-{__version__}-"):
            manifest.append({"file":file.name, "bytes":file.stat().st_size, "sha256":hashlib.file_digest(file.open("rb"),"sha256").hexdigest()})
    (release / "SHA256SUMS.txt").write_text("\n".join(f"{item['sha256']}  {item['file']}" for item in manifest) + "\n",encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    package_release()
