"""Build a self-contained x64 Windows installer from the pinned upstream files."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "src" / "video_localizer"
LOCKFILE = HERE / "windows-dependencies.json"
SETUP_NAME = "YoungTiger-VideoLocalizer-Setup-x64.exe"


def digest(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def fetch(cache, spec):
    target = cache / spec["file"]
    if target.is_file() and digest(target) == spec["sha256"]:
        print(f"[cache] {target.name} (SHA-256 verified)", flush=True)
        return target
    urls = [spec["url"], *spec.get("mirrors", [])]
    last_error = None
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as file:
        partial = Path(file.name)
    try:
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            for url in urls:
                host = urllib.parse.urlsplit(url).netloc
                print(f"[download {attempt + 1}/3] {target.name} from {host}", flush=True)
                request = urllib.request.Request(url, headers={"User-Agent": "YoungTiger-VideoLocalizer-Build/1.0",
                                                               "Accept": "application/octet-stream"})
                try:
                    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                        shutil.copyfileobj(response, output)
                    if digest(partial) != spec["sha256"]:
                        raise ValueError("download does not match the pinned SHA-256")
                except (OSError, http.client.HTTPException, ValueError) as error:
                    last_error = error
                    print(f"[failed] {target.name} from {host}: {error}", flush=True)
                    continue
                partial.replace(target)
                print(f"[verified] {target.name}", flush=True)
                return target
        raise RuntimeError(f"Could not download the pinned {target.name} from {len(urls)} source(s)") from last_error
    finally:
        partial.unlink(missing_ok=True)


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)


def materialize(payload, files):
    runtime = payload / "runtime"
    unpack(files["python"], runtime)
    packages = runtime / "site-packages"
    for library in ("numpy", "opencv", "pillow"):
        unpack(files[library], packages)
    # Embeddable CPython ignores system and user site-packages. Keep the app
    # and its wheels on this private path without reading host .pth hooks.
    (runtime / "python313._pth").write_text("python313.zip\n.\nsite-packages\n../app\n", encoding="ascii")
    code = payload / "app" / "video_localizer"
    code.mkdir(parents=True)
    for path in sorted(SOURCE.iterdir()):
        if path.is_file() and path.suffix in {".py", ".ps1"}:
            shutil.copy2(path, code / path.name)
    if not (code / "__main__.py").is_file():
        raise ValueError("Missing video_localizer CLI entry point.")
    binary = payload / "bin"
    binary.mkdir()
    notices = payload / "licenses"
    notices.mkdir()
    shutil.copy2(HERE.parent / "LICENSE.txt", notices / "YoungTiger-LICENSE.txt")
    with zipfile.ZipFile(files["ffmpeg"]) as source:
        prefix = "ffmpeg-9.0.2-essentials_build/"
        for member, target in {
            "bin/ffmpeg.exe": binary / "ffmpeg.exe",
            "bin/ffprobe.exe": binary / "ffprobe.exe",
            "LICENSE": notices / "FFmpeg-GPLv3.txt",
            "README.txt": notices / "FFmpeg-build.txt",
        }.items():
            with source.open(prefix + member) as data, target.open("wb") as output:
                shutil.copyfileobj(data, output)
    (payload / "fonts").mkdir()
    shutil.copy2(files["font"], payload / "fonts" / files["font"].name)
    shutil.copy2(files["font_license"], notices / "Noto-OFL.txt")
    with zipfile.ZipFile(files["nsis"]) as nsis:
        (notices / "NSIS-COPYING.txt").write_bytes(nsis.read("nsis-3.11/COPYING"))
    shutil.copy2(HERE / "third-party-notices.txt", notices / "README.txt")
    shutil.copy2(LOCKFILE, notices / LOCKFILE.name)
    version = re.search(r'__version__\s*=\s*"([^"]+)"', (code / "__init__.py").read_text()).group(1)
    (payload / "video-localizer-release.txt").write_text(version + "\n", encoding="ascii")
    subprocess.run([str(runtime / "python.exe"), "-X", "utf8", "-B", "-c",
                    "import cv2, numpy, PIL, video_localizer; from PIL import ImageFont; "
                    "ImageFont.truetype(r'fonts/NotoSansCJKkr-Regular.otf', 24)"], cwd=payload, check=True)
    return version


def install_payload(cache, scratch, output):
    specs = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    files = {key: fetch(cache, spec) for key, spec in specs.items()}
    payload = scratch / "payload"
    payload.mkdir()
    version = materialize(payload, files)
    nsis_root = scratch / "compiler"
    unpack(files["nsis"], nsis_root)
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    launcher = scratch / "video-localizer.exe"
    subprocess.run([str(csc), "/nologo", "/platform:x64", "/optimize+", "/target:exe",
                    "/out:" + str(launcher), str(HERE / "Launcher.cs")], check=True)
    cmd_shim = scratch / "video-localizer.cmd"
    cmd_shim.write_bytes(b'@echo off\r\n"%~dp0video-localizer.exe" %*\r\nexit /b %errorlevel%\r\n')
    inventory = sorted(payload.rglob("*"))
    remove = scratch / "uninstall-files.nsh"
    with remove.open("w", encoding="utf-8-sig") as lines:
        for path in reversed(inventory):
            rel = str(path.relative_to(payload)).replace("$", "$$")
            command = "RMDir" if path.is_dir() else "Delete"
            lines.write(f'    {command} "$4\\{rel}"\n')
    signature = hashlib.sha256(LOCKFILE.read_bytes())
    for path in [HERE.parent / "LICENSE.txt", HERE.parent / "README.md", HERE / "Launcher.cs",
                 HERE / "installer.nsi", HERE / "third-party-notices.txt", Path(__file__),
                 *sorted((payload / "app" / "video_localizer").iterdir())]:
        signature.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    installer = scratch / SETUP_NAME
    # NSIS reads UTF-8 input with a BOM for translated UI labels.
    template = scratch / "installer.nsi"
    template.write_text((HERE / "installer.nsi").read_text(encoding="utf-8"), encoding="utf-8-sig")
    options = {
        "OUTPUT": installer, "REVISION": signature.hexdigest()[:32], "PAYLOAD": payload,
        "LAUNCHER": launcher, "LAUNCHER_ID": digest(HERE / "Launcher.cs")[:32],
        "CMD_SHIM": cmd_shim, "UNINSTALL_FILES": remove, "VERSION": version,
        "PRODUCT_README": HERE.parent / "README.md",
    }
    subprocess.run([str(nsis_root / "nsis-3.11" / "makensis.exe"), "/V2",
                    *[f"/D{k}={v}" for k, v in options.items()], str(template)], check=True)
    result = output / SETUP_NAME
    # Copy before replacing so the previous release stays available on errors.
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        shutil.copyfile(installer, temp)
        checksum = digest(temp)
        temp.replace(result)
    finally:
        temp.unlink(missing_ok=True)
    result.with_suffix(".exe.sha256").write_text(checksum + "  " + result.name + "\n", encoding="ascii")
    print(f"{result}  {result.stat().st_size:,} bytes  SHA-256 {checksum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path, help="download and temporary build directory")
    parser.add_argument("--output-dir", required=True, type=Path, help="directory for the setup EXE and SHA-256")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Use 64-bit Windows with .NET Framework 4.x and Python 3.11+ to build.")
    cache = args.cache_dir.resolve()
    output = args.output_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="win-build-", dir=cache) as scratch:
        install_payload(cache, Path(scratch), output)


if __name__ == "__main__":
    main()
