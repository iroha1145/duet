"""Local Linux Office → PDF → page images, inside a strict bubblewrap sandbox.

Missing dependencies or unavailable isolation never run an unsandboxed Office
process. Each sandbox has private network/PID/user namespaces, minimal read-only
system mounts, bounded tmpfs, resource limits, and no user workspace/home mount.
Only its original input and a static macro-security profile are exposed.
"""
import base64
from collections import OrderedDict
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

from .attachment_extract import inspect_document
from .attachment_policy import AttachmentError, FILE_LIMIT

PAGE_LIMIT = 40
IMAGE_BYTES = 900_000
PDF_BYTES = 20 * 1024 * 1024
CONVERT_SECONDS = 20
RENDER_SECONDS = 20
CACHE_ENTRIES = 4
RENDER_VERSION = 1
_worker = threading.BoundedSemaphore(1)
_cache_lock = threading.Lock()
_cache = OrderedDict()

PROFILE = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisablePythonRuntime" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableOLEAutomation" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="SecureURL" oor:op="fuse"><value/></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
</oor:items>'''


def dependencies():
    if sys.platform != "linux":
        return None
    found = {key: shutil.which(key) for key in ("bwrap", "libreoffice", "pdftoppm", "prlimit")}
    return found if all(found.values()) else None


def sandbox_command(commands, input_path, profile_path, tools, max_bytes, timeout):
    """No ambient HOME, /run, /var, /home, credentials, or application mounts."""
    command = [tools["bwrap"], "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
               "--uid", "65534", "--gid", "65534", "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr"]
    for name in ("/bin", "/sbin", "/lib", "/lib64"):
        path = Path(name)
        if path.is_symlink():
            command += ["--symlink", os.readlink(path), name]
        elif path.exists():
            command += ["--ro-bind", name, name]
    for name in ("/etc/fonts", "/etc/libreoffice", "/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime"):
        if Path(name).exists():
            command += ["--ro-bind", name, name]
    command += ["--proc", "/proc", "--dev", "/dev", "--size", str(64 * 1024 * 1024), "--tmpfs", "/work",
                "--size", str(32 * 1024 * 1024), "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/home/preview",
                "--ro-bind", str(input_path), "/input" + input_path.suffix,
                "--ro-bind", str(profile_path), "/profile-seed", "--chdir", "/work",
                "--setenv", "HOME", "/home/preview", "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "LC_ALL", "C.UTF-8",
                "--setenv", "SAL_USE_VCLPLUGIN", "svp", "--setenv", "SAL_DISABLE_OPENCL", "1",
                tools["prlimit"], "--as=" + str(1024 * 1024 * 1024), "--cpu=" + str(max(1, int(timeout))),
                "--fsize=" + str(max_bytes), "--nofile=128", "--nproc=32", "--core=0", "--",
                "/bin/sh", "-eu", "-c", commands]
    return command


class RenderFailure(Exception):
    def __init__(self, reason, code="office_visual_unavailable"):
        self.reason, self.code = reason, code


def _run(command, output, timeout, max_bytes):
    # stdout is a regular bounded file, not an unbounded communicate() buffer.
    # Resource limits apply to the shell/converter and its cat-to-stdout writes.
    with output.open("wb") as sink:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.DEVNULL,
                                   close_fds=True, start_new_session=True, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            raise RenderFailure("Visual preview exceeded its time budget", "office_visual_timeout") from None
        if process.returncode:
            raise RenderFailure("The isolated Office renderer could not produce a visual preview; no unsandboxed fallback was used")
    if output.stat().st_size > max_bytes:
        raise RenderFailure("Visual preview exceeded its byte budget", "office_visual_limit")
    return output.read_bytes()


def render_office(content, name):
    """Return truthful visual status; callers recheck auth/provenance after await."""
    tools = dependencies()
    if tools is None:
        return {"status": "unavailable", "reason": "Visual preview requires Linux LibreOffice, Poppler and bubblewrap isolation"}
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in {".docx", ".pptx", ".xlsx"} or not content or len(content) > FILE_LIMIT:
        return {"status": "unavailable", "reason": "Unsupported Office visual input"}
    key = (hashlib.sha256(content).hexdigest(), suffix, RENDER_VERSION)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    if not _worker.acquire(blocking=False):
        return {"status": "unavailable", "reason": "Visual preview renderer is busy; reopen to retry"}
    try:
        with tempfile.TemporaryDirectory(prefix="duet-office-preview-") as temporary:
            stage = Path(temporary)
            original = stage / ("input" + suffix)
            original.write_bytes(content)
            profile = stage / "registrymodifications.xcu"
            profile.write_text(PROFILE, encoding="utf-8")
            # Fixed paths and fixed shell code; no user filename/title reaches
            # command text. The renderer cannot read another workspace file.
            convert = ("mkdir -p /work/profile/user; cp /profile-seed /work/profile/user/registrymodifications.xcu; "
                       "libreoffice -env:UserInstallation=file:///work/profile --headless --nologo --nodefault --nofirststartwizard "
                       "--convert-to pdf --outdir /work /input" + suffix + " >/dev/null 2>&1; cat /work/input.pdf")
            pdf = _run(sandbox_command(convert, original, profile, tools, PDF_BYTES, CONVERT_SECONDS), stage / "converted.pdf", CONVERT_SECONDS + 2, PDF_BYTES)
            checked = inspect_document(pdf, "preview.pdf", "application/pdf", preview=True)
            if checked.get("status") != "ready":
                raise RenderFailure("Converted PDF did not pass bounded validation", "office_visual_invalid_pdf")
            count = checked.get("page_count", 0)
            if not 1 <= count <= PAGE_LIMIT:
                raise RenderFailure("Visual preview exceeds the 40-page budget; the original file remains downloadable", "office_visual_limit")
            pdf_path = stage / "input.pdf"
            pdf_path.write_bytes(pdf)
            pages, total = [], 0
            deadline = time.monotonic() + RENDER_SECONDS
            scale, quality = (1440, 80) if count <= 4 else (1024, 75)
            for number in range(1, count + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RenderFailure("Page rendering exceeded its time budget", "office_visual_timeout")
                budget = IMAGE_BYTES - total
                if budget <= 0:
                    raise RenderFailure("Visual preview exceeded its total image budget", "office_visual_limit")
                render = ("pdftoppm -f " + str(number) + " -l " + str(number) + " -singlefile -scale-to " + str(scale)
                          + " -jpeg -jpegopt quality=" + str(quality) + " /input.pdf /work/page >/dev/null 2>&1; cat /work/page.jpg")
                image = _run(sandbox_command(render, pdf_path, profile, tools, budget, min(5, remaining)),
                             stage / "page.jpg", min(5, remaining) + .5, budget)
                try:
                    with Image.open(io.BytesIO(image)) as picture:
                        if picture.format != "JPEG" or not 1 <= picture.width <= 1600 or not 1 <= picture.height <= 1600:
                            raise ValueError("Unexpected page image")
                        picture.verify()
                except (OSError, ValueError):
                    raise RenderFailure("Page renderer returned an invalid image", "office_visual_invalid_image") from None
                total += len(image)
                pages.append(base64.b64encode(image).decode("ascii"))
            result = {"status": "ready", "pages": pages, "page_count": count, "image_bytes": total,
                      "engine": "LibreOffice / Poppler", "render_version": RENDER_VERSION}
            with _cache_lock:
                _cache[key] = result
                _cache.move_to_end(key)
                while len(_cache) > CACHE_ENTRIES:
                    _cache.popitem(last=False)
            return result
    except AttachmentError as error:
        return {"status": "unavailable", "reason": "Converted PDF validation failed: " + str(error), "code": error.code}
    except RenderFailure as error:
        return {"status": "unavailable", "reason": error.reason, "code": error.code}
    except OSError:
        return {"status": "unavailable", "reason": "The isolated local renderer is unavailable"}
    finally:
        _worker.release()
