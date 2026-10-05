# Third-party components

PaperReader bundles the Python runtime and the following open-source components:

| Component | License | Source |
| --- | --- | --- |
| Python | PSF | https://www.python.org/ |
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| Starlette | BSD-3-Clause | https://github.com/encode/starlette |
| Uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
| HTTPX / HTTPCore | BSD-3-Clause | https://github.com/encode/httpx |
| PyMuPDF / MuPDF | AGPL-3.0 or commercial | https://github.com/pymupdf/PyMuPDF |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| pythonnet | MIT | https://github.com/pythonnet/pythonnet |
| Pydantic | MIT | https://github.com/pydantic/pydantic |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| KaTeX 0.19.0 | MIT | https://github.com/KaTeX/KaTeX |

KaTeX's renderer, stylesheet, fonts and MIT license are bundled under `static/vendor/katex` (`_internal/static/vendor/katex` in desktop builds). Formula rendering does not load a remote CDN.

Other dependency license files are included under `_internal/licenses` in the portable and installed applications. PyInstaller is used to build the executable (GPL with a bootloader exception); Inno Setup is used to create the installer and is not an application runtime dependency.

PyMuPDF's open-source distribution uses AGPL-3.0. Redistribution must comply with its license or use an appropriate commercial license. The application source and build instructions are included in this workspace and in the release source archive.
