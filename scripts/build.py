"""Build a self-contained Windows executable and optional Inno Setup installer."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS = ROOT / ".deps"
if DEPS.is_dir():
    # Prevent optional packages from the developer's global environment from
    # inflating the desktop bundle or becoming accidental runtime dependencies.
    base = Path(sys.base_prefix)
    sys.path[:] = [str(ROOT), str(DEPS), str(base / f"python{sys.version_info.major}{sys.version_info.minor}.zip"), str(base / "DLLs"), str(base / "Lib"), str(base)]
    os.environ["PYTHONPATH"] = str(DEPS)
os.chdir(ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist", help="Build output directory; use a separate directory when an older build is running")
    args = parser.parse_args()
    dist_dir = args.dist_dir.resolve()
    from PyInstaller.__main__ import run
    run([
        str(ROOT / "launcher.py"), "--name=PaperReader", "--onedir", "--windowed", "--noconfirm", "--clean",
        "--specpath=" + str(ROOT / "build" / "spec"),
        "--distpath=" + str(dist_dir),
        "--add-data=" + str(ROOT / "static") + ";static", "--paths=" + str(DEPS),
        "--hidden-import=uvicorn.logging", "--hidden-import=uvicorn.loops.asyncio",
        "--hidden-import=uvicorn.protocols.http.h11_impl", "--hidden-import=uvicorn.lifespan.on",
        "--collect-all=webview", "--collect-all=pythonnet", "--collect-all=clr_loader",
        "--exclude-module=PyQt5", "--exclude-module=PyQt6", "--exclude-module=PySide2", "--exclude-module=PySide6",
        "--exclude-module=torch", "--exclude-module=scipy", "--exclude-module=matplotlib", "--exclude-module=IPython",
        "--exclude-module=numpy", "--exclude-module=pandas", "--exclude-module=zmq", "--exclude-module=pytest", "--exclude-module=playwright",
        "--icon=" + str(ROOT / "assets" / "icon.ico"),
    ])
    from scripts.package_release import package_release
    package_release(dist_dir / "PaperReader")


if __name__ == "__main__":
    main()
