"""One bounded policy for native uploads, extracted model input and downloads."""
import re
from pathlib import PurePosixPath
from urllib.parse import quote

FILE_LIMIT = 20 * 1024 * 1024
IMAGE_LIMIT = 6 * 1024 * 1024
TOTAL_LIMIT = 20 * 1024 * 1024
FILE_COUNT = 10
MODEL_TEXT_BYTES = 256 * 1024
MODEL_TEXT_CHARS = 200_000
PDF_PAGE_LIMIT = 200
EXTRACTOR_VERSION = 1
IMAGE_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}
DOCUMENT_TYPES = {".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
TEXT_SUFFIXES = {".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".css", ".log", ".yaml", ".yml", ".sql", ".sh", ".ipynb", ".ics"}
TEXT_TYPES = {"application/json", "application/xml", "application/javascript", "application/yaml",
    "application/x-yaml", "application/x-sh", "application/x-ipynb+json"}
# Native refused_kinds is extension based; never advertise support for legacy
# binary Office formats, executable macros, archives, media or unhandled images.
REFUSED_SUFFIXES = {".doc", ".xls", ".ppt", ".docm", ".xlsm", ".pptm", ".rtf", ".odt", ".epub", ".pages",
    ".numbers", ".key", ".zip", ".7z", ".rar", ".tar", ".gz", ".mp3", ".wav", ".m4a", ".mp4", ".mov",
    ".avi", ".flv", ".mpeg", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".avif", ".svg", ".exe", ".dll"}


class AttachmentError(ValueError):
    def __init__(self, message, status=400, code="invalid_attachment"):
        super().__init__(message)
        self.status, self.code = status, code


def attachment_rules():
    # Optional kind defaults to OTHER (b2s/c2s). Do not emit the non-wire "other".
    return {"refused_kinds": [{"extensions": sorted(suffix[1:] for suffix in REFUSED_SUFFIXES)}],
            "max_files_per_message": FILE_COUNT, "max_file_bytes": FILE_LIMIT}


def classify(name, mime):
    suffix = PurePosixPath(name).suffix.lower()
    mime = mime.split(";", 1)[0].strip().lower()
    if mime in IMAGE_TYPES:
        return "image", mime
    if suffix in REFUSED_SUFFIXES:
        raise AttachmentError("This file format is not supported; use PDF, DOCX, XLSX, PPTX or UTF-8 text", 415,
                              "unsupported_attachment_type")
    if suffix in DOCUMENT_TYPES:
        expected = DOCUMENT_TYPES[suffix]
        if mime not in {expected, "application/octet-stream", "application/zip"}:
            raise AttachmentError("Document filename and MIME type do not match", 415, "attachment_type_mismatch")
        return suffix[1:], expected
    if mime.startswith("text/") or mime in TEXT_TYPES or mime == "application/octet-stream" and suffix in TEXT_SUFFIXES:
        return "text", "text/plain" if mime == "application/octet-stream" else mime
    raise AttachmentError("Only PNG, JPEG, GIF, WebP, PDF, DOCX, XLSX, PPTX and UTF-8 text are supported", 415,
                          "unsupported_attachment_type")


def disposition(name, inline=False):
    # RFC 5987 avoids header injection and preserves non-ASCII Android save names.
    return ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(PurePosixPath(name).name, safe="")


def byte_range(value, size):
    """A single HTTP byte range; malformed/multiple/unsatisfiable ranges are 416."""
    if value is None:
        return 0, size, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or not any(match.groups()) or size == 0:
        raise AttachmentError("Requested byte range is not satisfiable", 416, "invalid_range")
    left, right = match.groups()
    if len(left) > 20 or len(right) > 20:
        raise AttachmentError("Requested byte range is not satisfiable", 416, "invalid_range")
    if left:
        start, end = int(left), int(right) + 1 if right else size
        if start >= size or end <= start:
            raise AttachmentError("Requested byte range is not satisfiable", 416, "invalid_range")
        return start, min(end, size), True
    suffix = int(right)
    if suffix <= 0:
        raise AttachmentError("Requested byte range is not satisfiable", 416, "invalid_range")
    return max(0, size - suffix), size, True


def ranged_response(request, data, headers, revision):
    from aiohttp import web
    headers = {**headers, "Accept-Ranges": "bytes", "ETag": '"' + revision + '"'}
    value = request.headers.get("Range")
    # Only the exact strong content revision authorizes a partial response.
    if request.headers.get("If-Range") not in (None, headers["ETag"]):
        value = None
    try:
        start, end, partial = byte_range(value, len(data))
    except AttachmentError:
        return web.Response(status=416, headers={**headers, "Content-Range": "bytes */" + str(len(data))})
    if partial:
        headers["Content-Range"] = "bytes " + str(start) + "-" + str(end - 1) + "/" + str(len(data))
    return web.Response(body=data[start:end], status=206 if partial else 200, headers=headers)
