"""Bounded, non-executing PDF/OOXML extraction in a disposable child process.

Nothing here renders HTML, opens URLs, evaluates formulas, loads Office, or
extracts ZIP members onto disk. Unsupported/limited input retains original
bytes in the caller and is never silently converted into partial model input.
"""
import hashlib
import io
import json
import logging
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import zipfile

try:
    from .attachment_policy import (AttachmentError, EXTRACTOR_VERSION, FILE_LIMIT, MODEL_TEXT_BYTES,
                                    MODEL_TEXT_CHARS, PDF_PAGE_LIMIT, classify)
except ImportError:
    if __name__ != "__main__":
        raise
    from attachment_policy import (AttachmentError, EXTRACTOR_VERSION, FILE_LIMIT, MODEL_TEXT_BYTES,
                                   MODEL_TEXT_CHARS, PDF_PAGE_LIMIT, classify)

TIMEOUT = 10
MEMORY_LIMIT = 512 * 1024 * 1024
ZIP_MEMBERS = 1024
ZIP_TOTAL = 32 * 1024 * 1024
ZIP_PART = 8 * 1024 * 1024
ZIP_RATIO = 200
_workers = threading.BoundedSemaphore(2)


def _result(status, kind, **kwargs):
    return {"status": status, "format": kind, "extractor_version": EXTRACTOR_VERSION,
            "max_text_chars": MODEL_TEXT_CHARS, "max_text_bytes": MODEL_TEXT_BYTES, **kwargs}


class _Text:
    def __init__(self):
        self.parts, self.chars, self.bytes = [], 0, 0

    def add(self, value):
        if not value:
            return
        self.chars += len(value)
        self.bytes += len(value.encode("utf-8"))
        if self.chars > MODEL_TEXT_CHARS or self.bytes > MODEL_TEXT_BYTES:
            raise AttachmentError("Extracted document text exceeds the model attachment budget; original file is downloadable", 413,
                                  "attachment_text_limit")
        self.parts.append(value)

    def get(self):
        return "".join(self.parts)


def _limits():
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                          "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        bounds = Extended()
        bounds.BasicLimitInformation.LimitFlags = 0x100 | 0x2  # PROCESS_MEMORY | PROCESS_TIME
        bounds.BasicLimitInformation.PerProcessUserTimeLimit = 6 * 10_000_000
        bounds.ProcessMemoryLimit = MEMORY_LIMIT
        if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) \
                or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise AttachmentError("Document extraction resource limits are unavailable", 503, "extractor_unavailable")
        return job  # Keep the handle open for the lifetime of this worker.
    raise AttachmentError("Document extraction is unsupported on this platform", 503, "extractor_unavailable")


def inspect_document(data, name, mime, *, preview=False, validate_office=False):
    """Return bounded extraction metadata; over-budget originals stay downloadable."""
    kind, canonical_mime = classify(name, mime)
    if not data or len(data) > FILE_LIMIT:
        raise AttachmentError("Attachment must contain 1..20 MiB of data", 413, "attachment_size_limit")
    if not _workers.acquire(blocking=False):
        raise AttachmentError("Document extraction is busy; retry the upload", 429, "extractor_busy")
    try:
        paths = [str(Path(__file__).parent), *[str(Path(path).resolve()) for path in sys.path if path]]
        bootstrap = "import json,runpy,sys;sys.path[:0]=json.loads(sys.argv[1]);runpy.run_path(sys.argv[2],run_name='__main__')"
        command = [sys.executable, "-I", "-c", bootstrap, json.dumps(paths), str(Path(__file__).resolve())]
        header = json.dumps({"name": name, "mime": canonical_mime, "preview": preview, "validate_office": validate_office}).encode() + b"\n"
        try:
            run = subprocess.run(command, input=header + data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=TIMEOUT, check=False, creationflags=0x08000000 if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return _result("unavailable", kind, code="extraction_timeout", reason="Document extraction exceeded its time budget",
                           source_sha256=hashlib.sha256(data).hexdigest())
        if run.returncode != 0 or len(run.stdout) > MODEL_TEXT_BYTES * 6 + 8192:
            return _result("unavailable", kind, code="extraction_resource_limit", reason="Document extraction exceeded its resource budget",
                           source_sha256=hashlib.sha256(data).hexdigest())
        try:
            result = json.loads(run.stdout)
        except (ValueError, UnicodeError):
            raise AttachmentError("Document extractor did not return a valid result", 503, "extractor_unavailable") from None
        if result.get("error"):
            raise AttachmentError(result["error"], result["http_status"], result["code"])
        result["source_sha256"] = hashlib.sha256(data).hexdigest()
        return result
    finally:
        _workers.release()


def _pdf(data, preview):
    from pypdf import PdfReader, overwrite_configuration
    overwrite_configuration(maximum_declared_stream_length=FILE_LIMIT,
        zlib_maximum_output_length=ZIP_PART, lzw_maximum_output_length=ZIP_PART,
        run_length_maximum_output_length=ZIP_PART, jbig2_maximum_output_length=ZIP_PART,
        brotli_maximum_output_length=ZIP_PART, array_based_stream_maximum_output_length=ZIP_PART,
        stream_decoding_work_maximum_length=ZIP_TOTAL, image_maximum_buffer_size=ZIP_PART,
        page_tree_maximum_depth=64, page_tree_maximum_entries=2000, page_tree_maximum_page_entries=20000,
        array_maximum_element_count=20000, dictionary_maximum_element_count=20000,
        xform_maximum_invocations_per_extraction=1000, jbig2dec_binary=None)
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-65536:]:
        raise AttachmentError("PDF header or end marker is missing", 415, "invalid_pdf")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True, xref_stream_maximum_entries=100000)
        if reader.is_encrypted:
            raise AttachmentError("Encrypted PDF is not supported; upload an unencrypted copy", 422, "encrypted_pdf")
        pages = len(reader.pages)
        if pages < 1:
            raise AttachmentError("PDF contains no pages", 422, "invalid_pdf")
        if pages > PDF_PAGE_LIMIT:
            return _result("too_large", "pdf", page_count=pages, code="pdf_page_limit", reason="PDF exceeds 200 pages")
        text = _Text()
        decoded_total = 0
        for index, page in enumerate(reader.pages, 1):
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise AttachmentError("PDF contains an invalid page size", 422, "invalid_pdf")
            content_stream = page.get_contents()
            if content_stream is not None:
                decoded_total += len(content_stream.get_data())
                if decoded_total > ZIP_TOTAL:
                    return _result("too_large", "pdf", page_count=pages, code="pdf_content_limit",
                                   reason="PDF decoded page content exceeds 32 MiB")
            if not preview:
                text.add("\n[Page " + str(index) + "]\n")
                content = page.extract_text() or ""
                text.add(content)
        return _result("ready" if preview or any(part.strip() and not part.startswith("\n[Page ") for part in text.parts) else "no_text",
                       "pdf", page_count=pages, text=text.get() if not preview else "",
                       warnings=["PDF text extraction does not perform OCR or interpret image-only content"])
    except AttachmentError:
        raise
    except MemoryError:
        return _result("unavailable", "pdf", code="extraction_resource_limit", reason="PDF extraction exceeded its memory budget")
    except Exception:
        raise AttachmentError("PDF is damaged or uses unsupported PDF structures", 422, "invalid_pdf") from None


class _Package:
    def __init__(self, data):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        members = self.archive.infolist()
        if len(members) > ZIP_MEMBERS:
            raise AttachmentError("Office package has too many members", 413, "office_package_limit")
        self.names, total = set(), 0
        for member in members:
            # ZipInfo.filename normalizes backslashes on Windows and truncates
            # NULs; inspect the original central-directory name before that.
            name = member.orig_filename
            if (not name or name.startswith("/") or "\\" in name or ":" in name or "\0" in name
                    or ".." in PurePosixPath(name).parts or name in self.names or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise AttachmentError("Office package contains an unsafe member path", 415, "unsafe_office_package")
            self.names.add(name)
            total += member.file_size
            if member.flag_bits & 1:
                raise AttachmentError("Encrypted Office packages are not supported", 422, "encrypted_office")
            if (member.file_size > ZIP_PART or total > ZIP_TOTAL
                    or member.file_size > max(1, member.compress_size) * ZIP_RATIO):
                raise AttachmentError("Office package exceeds decompression limits", 413, "office_package_limit")
            if "vbaproject" in name.lower() or name.lower().endswith((".exe", ".dll")):
                raise AttachmentError("Macro-enabled or executable Office content is not supported", 415, "office_active_content")
        if "[Content_Types].xml" not in self.names:
            raise AttachmentError("Not a valid Office Open XML package", 415, "invalid_office_package")

    def xml(self, name):
        from defusedxml import ElementTree
        if name not in self.names:
            raise AttachmentError("Office package is missing required document parts", 422, "invalid_office_package")
        with self.archive.open(name) as source:
            data = source.read(ZIP_PART + 1)
        if len(data) > ZIP_PART:
            raise AttachmentError("Office XML part exceeds decompression limits", 413, "office_package_limit")
        return ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)

    def close(self):
        self.archive.close()


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _paragraph(node, namespace):
    return "".join((child.text or "") if child.tag == namespace + "t" else "\t" if child.tag == namespace + "tab"
                   else "\n" if child.tag in {namespace + "br", namespace + "cr"} else "" for child in node.iter())


def _relationships(package, filename):
    relationships = {}
    source = PurePosixPath(filename)
    rels = str(source.parent / "_rels" / (source.name + ".rels"))
    if rels not in package.names:
        return relationships
    for item in package.xml(rels):
        if item.get("TargetMode") == "External":
            continue
        target = item.get("Target", "")
        if "\\" in target or ":" in target or "\0" in target:
            raise AttachmentError("Office package contains an invalid relationship", 422, "invalid_office_package")
        target = posixpath.normpath(target[1:] if target.startswith("/") else str(source.parent / target))
        if target.startswith("../") or target not in package.names:
            raise AttachmentError("Office package relationship points outside its package", 422, "invalid_office_package")
        relationships[item.get("Id")] = target
    return relationships


def _office(data, kind):
    package = None
    try:
        package = _Package(data)
        text = _Text()
        warnings = ["Text extraction omits embedded images and formatting; external links, formulas and macros are never executed"]
        if kind == "docx":
            body = package.xml("word/document.xml").find(W + "body")
            if body is None:
                raise AttachmentError("Word document has no body", 422, "invalid_office_package")
            for paragraph in body.iter(W + "p"):
                text.add(_paragraph(paragraph, W) + "\n")
            for name in sorted(package.names):
                if re.fullmatch(r"word/(?:header|footer|footnotes|endnotes)\d*\.xml", name):
                    for paragraph in package.xml(name).iter(W + "p"):
                        text.add(_paragraph(paragraph, W) + "\n")
        elif kind == "pptx":
            presentation = package.xml("ppt/presentation.xml")
            relationships = _relationships(package, "ppt/presentation.xml")
            p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
            slides = [relationships.get(slide.get(REL)) for slide in presentation.iter(p + "sldId")]
            if not slides:
                raise AttachmentError("Presentation contains no slides", 422, "invalid_office_package")
            for index, name in enumerate(slides, 1):
                if name is None:
                    raise AttachmentError("Presentation slide relationship is invalid", 422, "invalid_office_package")
                text.add("\n[Slide " + str(index) + "]\n")
                for paragraph in package.xml(name).iter(A + "p"):
                    text.add(_paragraph(paragraph, A) + "\n")
                for notes in _relationships(package, name).values():
                    if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", notes):
                        text.add("[Speaker notes]\n")
                        for paragraph in package.xml(notes).iter(A + "p"):
                            text.add(_paragraph(paragraph, A) + "\n")
        else:
            shared = []
            if "xl/sharedStrings.xml" in package.names:
                for item in package.xml("xl/sharedStrings.xml").iter(S + "si"):
                    shared.append("".join(node.text or "" for node in item.iter(S + "t")))
            relationships = _relationships(package, "xl/workbook.xml")
            for sheet in package.xml("xl/workbook.xml").iter(S + "sheet"):
                target = relationships.get(sheet.get(REL))
                if target not in package.names:
                    raise AttachmentError("Workbook sheet relationship is invalid", 422, "invalid_office_package")
                text.add("\n[Sheet " + sheet.get("name", "") + "]\n")
                for row in package.xml(target).iter(S + "row"):
                    values = []
                    for cell in row.findall(S + "c"):
                        value, formula = cell.find(S + "v"), cell.find(S + "f")
                        content = value.text or "" if value is not None else ""
                        if cell.get("t") == "s":
                            if not content.isdigit() or int(content) >= len(shared):
                                raise AttachmentError("Workbook shared string reference is invalid", 422, "invalid_office_package")
                            content = shared[int(content)]
                        elif cell.get("t") == "inlineStr":
                            content = "".join(node.text or "" for node in cell.iter(S + "t"))
                        if formula is not None:
                            content = "[formula, not executed: " + (formula.text or "") + "; cached value: " + content + "]"
                        values.append(cell.get("r", "") + ": " + content)
                    text.add("\t".join(values) + "\n")
        value = text.get()
        return _result("ready" if value.strip() else "no_text", kind, text=value, warnings=warnings)
    except AttachmentError:
        raise
    except ImportError:
        raise AttachmentError("The pinned Office parser dependency is unavailable", 503, "extractor_unavailable") from None
    except MemoryError:
        return _result("unavailable", kind, code="extraction_resource_limit", reason="Office extraction exceeded its memory budget")
    except Exception:
        raise AttachmentError("Office document is damaged, encrypted, or contains unsafe XML", 422, "invalid_office_package") from None
    finally:
        if package is not None:
            package.close()


def _extract(data, name, mime, preview):
    kind, _ = classify(name, mime)
    try:
        if kind == "pdf":
            return _pdf(data, preview)
        if kind in {"docx", "xlsx", "pptx"}:
            return _office(data, kind)
        if kind != "text":
            raise AttachmentError("Images use the image decoder", 415, "unsupported_attachment_type")
        try:
            value = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AttachmentError("Text attachment must be UTF-8", 415, "invalid_text_encoding") from None
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise AttachmentError("Binary content is not a text attachment", 415, "invalid_text_encoding")
        text = _Text()
        text.add(value)
        return _result("ready", kind, text=text.get(), warnings=[])
    except AttachmentError as error:
        if error.code in {"attachment_text_limit", "office_package_limit"}:
            return _result("too_large", kind, code=error.code, reason=str(error))
        raise


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        _job = _limits()
        header = sys.stdin.buffer.readline(16384)
        if not header.endswith(b"\n"):
            raise AttachmentError("Invalid extractor request")
        request = json.loads(header)
        raw = sys.stdin.buffer.read(FILE_LIMIT + 1)
        if not raw or len(raw) > FILE_LIMIT:
            raise AttachmentError("Invalid extractor input size", 413, "attachment_size_limit")
        if request.get("validate_office"):
            # Optional Make-only gate; ordinary attachment extraction retains
            # its original behavior and does not require the Office writers.
            from bridge.make_validate import validate_package
            validate_package(raw, request["name"])
        result = _extract(raw, request["name"], request["mime"], request["preview"])
        if request.get("validate_office"):
            result["make_validation_version"] = 1
    except AttachmentError as error:
        result = {"error": str(error), "http_status": error.status, "code": error.code}
    except ImportError:
        result = {"error": "The pinned document parser dependency is unavailable", "http_status": 503, "code": "extractor_unavailable"}
    except (MemoryError, OSError):
        result = _result("unavailable", "document", code="extraction_resource_limit", reason="Document extraction exceeded its resource budget")
    except Exception:
        result = {"error": "Document extraction could not be completed", "http_status": 422, "code": "invalid_document"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode("ascii"))
