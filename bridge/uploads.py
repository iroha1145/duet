"""Original Android multipart/MessageFile adapter backed by the real engine store."""
import base64
import asyncio
import hashlib
import io
import os
import re
import warnings
import threading
from uuid import UUID, uuid4

from aiohttp import web
from PIL import Image, UnidentifiedImageError

from .rest import ORGANIZATION_ID
from .rest_home import utc_now
from .attachment_policy import (AttachmentError, FILE_LIMIT, FILE_COUNT, IMAGE_LIMIT, TOTAL_LIMIT, MODEL_TEXT_BYTES,
    IMAGE_TYPES, TEXT_TYPES, TEXT_SUFFIXES, EXTRACTOR_VERSION, classify, disposition, ranged_response)
from .attachment_extract import inspect_document

TEXT_LIMIT = FILE_LIMIT  # Raw bytes; extracted model input has a separate explicit budget.
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
UploadError = AttachmentError
_upload_slots = threading.BoundedSemaphore(4)


def attachment_id(value):
    """Resolve native p1d0.q/k2d0.m IDs without trusting them as file paths.

    Android upload returns a UUID, then iz70 converts it to file_[namespace_]01
    plus a fixed-width 22-character base58 encoding of the UUID's 16 bytes.
    Namespace is an opaque routing label, never an authorization grant.
    """
    if not isinstance(value, str) or len(value) > 256:
        raise UploadError("Invalid attachment ID")
    if value.startswith("file_"):
        match = re.fullmatch(r"file_(?:[A-Za-z0-9-]+_)*01([1-9A-HJ-NP-Za-km-z]{22})", value)
        if not match:
            raise UploadError("Invalid attachment ID")
        number = 0
        for char in match[1]:
            number = number * 58 + BASE58.index(char)
        if number >= 1 << 128:
            raise UploadError("Invalid attachment ID")
        return str(UUID(int=number))
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise UploadError("Invalid attachment ID") from error
    if value not in (str(parsed), parsed.hex):
        raise UploadError("Invalid attachment ID")
    return str(parsed)


def safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 255 or value in (".", "..")
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or "/" in value or "\\" in value):
        raise UploadError("Invalid attachment filename")
    return value


def validate_bytes(data, name, mime):
    """Verify actual image bytes or extract a bounded, non-executing document."""
    safe_name(name)
    if not data:
        raise UploadError("Empty attachment")
    if mime in IMAGE_TYPES:
        if len(data) > IMAGE_LIMIT:
            raise UploadError("Image exceeds 6 MiB", 413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    if image.format != IMAGE_TYPES[mime] or width * height > 40_000_000 or max(width, height) > 20000:
                        raise UploadError("Image type or dimensions are invalid", 415)
                    image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise UploadError("Invalid image", 415) from error
        return {"width": width, "height": height}
    result = inspect_document(data, name, mime)
    text = result.pop("text", None)
    return {"extraction": result, **({"text": text} if text is not None else {})}


def attachment_wire(record):
    result = {"file_id": record["id"], "filename": record["name"], "mime_type": record["mime"], "size_bytes": record["size"]}
    if "width" in record:
        result.update(image_width=record["width"], image_height=record["height"])
    return result


def load_attachment(store, ident, organization_id=ORGANIZATION_ID):
    """Read authenticated original bytes and verify their persisted digest only.

    Downloads must remain independent of extraction caches and parser versions.
    SQLite access stays on the caller's event-loop thread.
    """
    ident = attachment_id(ident)
    record = store.get("attachments", ident)
    if not record or record.get("organization_id") != organization_id:
        raise UploadError("Unknown attachment", 404)
    root = (store.root / "attachments").resolve()
    path = root / ident
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        raise UploadError("Attachment content is unavailable", 404)
    with path.open("rb") as source:
        data = source.read(FILE_LIMIT + 1)
    if len(data) != record.get("size") or len(data) > FILE_LIMIT or hashlib.sha256(data).hexdigest() != record.get("sha256"):
        raise UploadError("Attachment content changed", 409)
    return record, data, {}


def cached_metadata(record):
    if record["mime"] in IMAGE_TYPES:
        return {key: record[key] for key in ("width", "height") if key in record}
    extraction = record.get("extraction")
    if (not isinstance(extraction, dict) or extraction.get("extractor_version") != EXTRACTOR_VERSION
            or extraction.get("source_sha256") != record["sha256"]):
        return None
    text = record.get("extracted_text")
    if text is not None and (not isinstance(text, str) or len(text.encode("utf-8")) > MODEL_TEXT_BYTES):
        raise UploadError("Stored extraction metadata is invalid", 409)
    if extraction.get("status") == "ready" and text is None:
        return None
    return {"extraction": extraction, **({"text": text} if text is not None else {})}


def record_identity(record):
    return {key: value for key, value in record.items() if key not in {"extraction", "extracted_text"}}


async def prepared_attachments(store, references, organization_id, recheck):
    if not isinstance(references, list) or len(references) > FILE_COUNT:
        raise UploadError("At most 10 attachment objects are allowed")
    if recheck is not None:
        recheck()
    pending, seen, size = [], set(), 0
    for ref in references:
        if not isinstance(ref, dict):
            raise UploadError("Attachment must be a ChannelMessageAttachment object")
        ident = ref.get("file_id")
        record, data, _ = load_attachment(store, ident, organization_id)
        if record["id"] in seen:
            raise UploadError("Duplicate attachment ID")
        seen.add(record["id"])
        wire = attachment_wire(record)
        # Keep the original native wire ID: its thumbnail projection decodes
        # file_...01base58 back to the upload UUID with p1d0.p.
        wire["file_id"] = ident
        for key in ("filename", "mime_type", "size_bytes", "image_width", "image_height"):
            value = ref.get(key)
            if value is not None and (type(value) is not type(wire.get(key)) or value != wire.get(key)):
                raise UploadError("Attachment metadata does not match uploaded content")
        size += record["size"]
        if size > TOTAL_LIMIT:
            raise UploadError("Attachments exceed 20 MiB in total", 413)
        metadata = cached_metadata(record)
        migrate = metadata is None
        if migrate:
            try:
                metadata = await asyncio.to_thread(validate_bytes, data, record["name"], record["mime"])
            finally:
                if recheck is not None:
                    recheck()
        pending.append((wire, record, metadata, migrate))
    # Every source is reread after the final parser await: an earlier attachment
    # can change while a later one is being parsed. No SQLite work runs off-loop.
    result, commits = [], []
    for wire, original, metadata, migrate in pending:
        current, data, _ = load_attachment(store, original["id"], organization_id)
        if record_identity(current) != record_identity(original):
            raise UploadError("Attachment record changed during extraction", 409)
        if current != original:
            latest = cached_metadata(current)
            if latest is None:
                raise UploadError("Attachment cache changed during extraction", 409)
            metadata, migrate = latest, False
        if migrate:
            updated = {**current, "extraction": metadata["extraction"]}
            updated.pop("extracted_text", None)
            if "text" in metadata:
                updated["extracted_text"] = metadata["text"]
            commits.append(updated)
            current = updated
        result.append((wire, current, data, metadata))
    if recheck is not None:
        recheck()
    for record in commits:
        store.put("attachments", record)
    text_bytes = 0
    for _, _, _, metadata in result:
        extraction = metadata.get("extraction")
        if extraction is not None:
            if extraction.get("status") != "ready" or "text" not in metadata:
                status = 413 if extraction.get("status") == "too_large" else 422
                reason = extraction.get("reason") or "This document has no extractable text; image-only PDFs require OCR"
                raise UploadError(reason + "; original file remains downloadable", status,
                                  extraction.get("code", "attachment_has_no_text"))
            text_bytes += len(metadata["text"].encode("utf-8"))
            if text_bytes > MODEL_TEXT_BYTES:
                raise UploadError("Combined extracted attachments exceed the model text budget; send fewer files", 413,
                                  "attachment_text_limit")
    return result


async def prepare_attachments(store, references, organization_id=ORGANIZATION_ID, *, recheck=None):
    return [wire for wire, _, _, _ in await prepared_attachments(store, references, organization_id, recheck)]


async def message_attachments(store, references, text, organization_id=ORGANIZATION_ID):
    records, images = [], []
    for _, record, data, metadata in await prepared_attachments(store, references, organization_id, None):
        records.append(record)
        if record["mime"] in IMAGE_TYPES:
            images.append({"mime": record["mime"], "data": base64.b64encode(data).decode("ascii")})
        else:
            text += "\n\n[Attachment: " + record["name"] + "]\n" + metadata["text"]
    return text, records, images


def register_uploads(app, engine=None, *, organization_id=ORGANIZATION_ID, public_base=None):
    """Register before catch-all. engine is the real EngineService, or None in fixture mode.

    Authentication is the host's outer middleware; no credentials are created here.
    public_base optionally fixes the externally reachable base URL for image assets.
    """
    async def upload(request):
        if engine is None:
            return web.json_response({"error": "Uploads require the real engine"}, status=501)
        if request.match_info["organization"] != organization_id:
            return web.json_response({"error": "Unknown organization"}, status=404)
        if not _upload_slots.acquire(blocking=False):
            return web.json_response({"error": "Too many uploads; retry shortly", "code": "uploads_busy"}, status=429)
        release_slot = True
        try:
            if request.content_type != "multipart/form-data":
                raise UploadError("multipart/form-data required", 415)
            reader = await request.multipart()
            part = await reader.next()
            if part is None or part.name != "file" or not part.filename:
                raise UploadError("Exactly one file part is required")
            name = safe_name(part.filename)
            mime = part.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
            _, mime = classify(name, mime)
            chunks, size = [], 0
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > (IMAGE_LIMIT if mime in IMAGE_TYPES else TEXT_LIMIT):
                    raise UploadError("Attachment size limit exceeded", 413)
                chunks.append(chunk)
                authorized = request.get("duet_authorized")
                if authorized is not None and not authorized():
                    raise UploadError("Login expired during upload", 401, "unauthorized")
            if await reader.next() is not None:
                raise UploadError("Exactly one file part is required")
            data = b"".join(chunks)
            chunks.clear()
            validation = asyncio.create_task(asyncio.to_thread(validate_bytes, data, name, mime))
            try:
                metadata = await asyncio.shield(validation)
            except asyncio.CancelledError:
                # A disconnected request must not release its memory/parser
                # admission slot while its bounded worker is still running.
                release_slot = False
                def finished(task):
                    if not task.cancelled():
                        task.exception()
                    _upload_slots.release()
                validation.add_done_callback(finished)
                raise
            authorized = request.get("duet_authorized")
            if authorized is not None and not authorized():
                raise UploadError("Login expired during upload", 401, "unauthorized")
            if request.transport is None or request.transport.is_closing():
                raise UploadError("Upload connection closed before completion", 499, "upload_cancelled")
            ident = str(uuid4())
            record = {"id": ident, "name": name, "mime": mime, "size": size,
                      "organization_id": organization_id, "created_at": utc_now(), "sha256": hashlib.sha256(data).hexdigest()}
            if "width" in metadata:
                record.update(width=metadata["width"], height=metadata["height"])
            if "extraction" in metadata:
                record["extraction"] = metadata["extraction"]
                if "text" in metadata:
                    record["extracted_text"] = metadata["text"]
            path = engine.store.root / "attachments" / ident
            try:
                with path.open("xb") as output:
                    os.chmod(path, 0o600)
                    output.write(data)
                engine.store.put("attachments", record)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            base = public_base.rstrip("/") if public_base else str(request.url.origin())
            url = base + "/api/organizations/" + organization_id + "/files/" + ident + "/contents"
            result = {"file_kind": "image" if mime in IMAGE_TYPES else "blob", "file_uuid": ident,
                      "file_name": name, "sanitized_name": name, "size_bytes": size, "created_at": record["created_at"]}
            if mime in IMAGE_TYPES:
                asset = {"image_width": metadata["width"], "image_height": metadata["height"]}
                image_base = base + "/api/" + organization_id + "/files/" + ident
                result.update(thumbnail_asset={**asset, "url": image_base + "/thumbnail"},
                              preview_asset={**asset, "url": image_base + "/preview"})
            else:
                result.update(download_source=url, text_extracted=metadata["extraction"]["status"] == "ready",
                              extraction=metadata["extraction"])
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except UploadError as error:
            return web.json_response({"error": str(error), "code": error.code}, status=error.status)
        except (ValueError, AssertionError):
            return web.json_response({"error": "Invalid multipart upload"}, status=400)
        finally:
            if release_slot:
                _upload_slots.release()

    async def content(request):
        if engine is None:
            return web.json_response({"error": "Uploads require the real engine"}, status=501)
        try:
            if request.match_info.get("organization", organization_id) != organization_id:
                raise UploadError("Unknown organization", 404)
            record, data, _ = load_attachment(engine.store, request.match_info["id"], organization_id)
            if request.match_info.get("asset") in ("thumbnail", "preview") and record["mime"] not in IMAGE_TYPES:
                raise UploadError("Image preview is only available for PNG, JPEG, GIF and WebP", 415)
            return ranged_response(request, data, {"Content-Type": record["mime"], "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": disposition(record["name"], record["mime"] in IMAGE_TYPES)}, record["sha256"])
        except UploadError as error:
            return web.json_response({"error": str(error)}, status=error.status)

    for path in ("/{organization}/upload", "/organizations/{organization}/upload"):
        for prefix in ("", "/api"):
            app.router.add_post(prefix + path, upload)
    app.router.add_get("/duet/uploads/{id}/content", content)
    # Exact paths built by the original ijd0.C0/D0/E0 and h6w.a helpers.
    app.router.add_get("/api/{organization}/files/{id}/{asset:thumbnail|preview}", content)
    app.router.add_get("/api/organizations/{organization}/files/{id}/contents", content)
