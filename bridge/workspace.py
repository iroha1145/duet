"""Original Aibo workspace/environment schemas over the connected local engine.

There is no remote forwarding, invented computer, or separate file sandbox.
The only filesystem root is engine.tools.workspace. Local HTTP extensions are
under /duet/workspace; inspected Android routes retain their original schemas.
"""
import base64
import asyncio
import errno
import hashlib
import html
import json
import os
import re
import socket
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from aiohttp import web
from duet_server.tools import workspace_path

from .aibo import AiboError, message_time
from .rest import ACCOUNT_ID, ORGANIZATION_ID
from .rest_home import object_body, response, utc_now
from .attachment_policy import (AttachmentError, FILE_LIMIT, disposition, ranged_response)
from .attachment_extract import inspect_document
from .make_formats import (FORMATS, TYPE_IDS, available as format_available, family as make_family,
                           preview as office_preview, resolve_type, starter as office_starter, wire_type)
from .make_render import render_office
from .make_web import WEB_FORMATS, starter as web_starter

MAX_FILE = FILE_LIMIT
MAX_TEXT = 2 * 1024 * 1024
MAX_FILES = 1000
MAX_SESSIONS = 32
MIMES = {
    ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown",
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "text/javascript", ".json": "application/json", ".csv": "text/csv",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".xml": "application/xml",
    ".py": "text/plain", ".sql": "text/plain", ".log": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
PAGE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; media-src 'self'; connect-src 'none'; "
            "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; sandbox allow-scripts")


class WorkspaceError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def iso_time(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_link(path):
    return path.is_symlink() or (getattr(path, "is_junction", lambda: False)())


def file_kind(name):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if MIMES.get(suffix, "").startswith("image/"):
        return "image"
    if suffix == ".pdf":
        return "pdf"
    return "text" if MIMES.get(suffix, "").startswith("text/") else "file"


class Workspace:
    @staticmethod
    def make_type(record):
        if make_family(record["path"]) == "page" and record.get("make_type") in WEB_FORMATS:
            return record["make_type"]
        return make_family(record["path"])

    @staticmethod
    def is_make_frame(record):
        kind = make_family(record["path"])
        preview = record.get("office_preview", {})
        return kind == "page" or (kind in FORMATS and preview.get("make_validation_version") == 1 and preview.get("status") in {"ready", "no_text"}
                                  and (not record.get("content_revision") or preview.get("content_revision") == record["content_revision"]))

    def __init__(self, home, aibo, engine, data_dir, tasks=None):
        self.home, self.aibo, self.engine = home, aibo, engine
        self.tasks = tasks
        self.path = Path(data_dir) / "workspace.json"
        self.output_directory = Path(data_dir) / "workspace-outputs"
        self.state = {"version": 1, "environment_id": "env_" + str(uuid4()),
                      "name": os.environ.get("DUET_COMPUTER_NAME", socket.gethostname()),
                      "created_at": utc_now(), "files": {}, "favorites": [], "creates": {},
                      "presence": {}, "read_markers": {}}
        self.state["private_files"] = {}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("version") != 1:
                raise ValueError("Unsupported workspace metadata version")
            migrated = False
            for key in ("presence", "read_markers", "private_files"):
                if key not in self.state:
                    self.state[key] = {}
                    migrated = True
            if migrated:
                self.save()
        else:
            configured = os.environ.get("DUET_ENVIRONMENT_ID")
            if configured:
                if not re.fullmatch(r"env_[A-Za-z0-9_-]{1,120}", configured):
                    raise ValueError("Invalid DUET_ENVIRONMENT_ID")
                self.state["environment_id"] = configured
            self.save()
        self.root = None
        if engine is not None:
            root = Path(engine.tools.workspace).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("Engine workspace is not a directory")
            self.root = root
        self.ensure_publication_order()
        self.ensure_gallery_deletions()

    def page_lineage(self, record, origin):
        """Actual producer identity plus exact workspace path; never basename."""
        session = self.aibo.state["sessions"].get(origin.get("session_id"), {})
        tid, cid = origin.get("task_id"), origin.get("task_channel_id")
        if self.tasks and tid and cid and session.get("task_id", session.get("trigger_id")) == tid:
            seen, hops = set(), 0
            while (cid, tid) not in seen:
                seen.add((cid, tid))
                task = self.tasks.state["channels"].get(cid, {}).get(tid)
                data = (task or {}).get("data", {})
                target, moved = data.get("movedToRoomId"), data.get("movedToTaskId")
                if not data.get("retired") or not target or not moved:
                    return ("task", cid, tid, record["path"]), hops
                cid, tid, hops = target, moved, hops + 1
        return ("session", origin["session_id"], record["path"]), 0

    def ensure_publication_order(self):
        """One-time deterministic migration; new successful publishes append."""
        records = [record for record in self.state["files"].values()
                   if record.get("content_revision") and make_family(record["path"]) is not None]
        numbered = [origin["publication_seq"] for record in records for origin in record.get("provenance", [])
                    if "publication_seq" in origin]
        if any(type(value) is not int or value < 1 for value in numbered):
            raise ValueError("Invalid page publication order")
        counter = max([self.state.get("publication_counter", 0), *numbered])
        missing = [(record, origin) for record in records for origin in record.get("provenance", [])
                   if "publication_seq" not in origin]
        def prior_order(item):
            record, origin = item
            session = self.aibo.state["sessions"].get(origin["session_id"], {})
            position = next((index for index, message in enumerate(session.get("messages", []))
                             if message["id"] == origin["message_id"]), -1)
            handle = origin.get("attached_output_id", "")
            ordinal = int(handle.rsplit(".", 1)[1]) if re.fullmatch(r"cmsg_[A-Za-z0-9_-]+\.[0-9]+", handle) else -1
            group, hops = self.page_lineage(record, origin)
            return (group, -hops, session.get("created_at", ""), origin["session_id"], position, ordinal,
                    origin.get("created_at", record["created_at"]), record["id"], origin["tool_call_id"])
        for record, origin in sorted(missing, key=prior_order):
            counter += 1
            origin["publication_seq"] = counter
        if missing or self.state.get("publication_counter") != counter:
            self.state["publication_counter"] = counter
            self.save()

    def page_groups(self, record):
        if not record.get("content_revision"):
            return {("file", record["path"])}
        return {self.page_lineage(record, origin)[0] for origin in self.valid_provenance(record)}

    def resolve_page_lineage(self, lineage):
        """A saved task identity can follow moves without its old producer."""
        group = tuple(lineage)
        if group[0] != "task" or self.tasks is None:
            return group
        _, cid, tid, path = group
        seen = set()
        while (cid, tid) not in seen:
            seen.add((cid, tid))
            task = self.tasks.state["channels"].get(cid, {}).get(tid)
            data = (task or {}).get("data", {})
            target, moved = data.get("movedToRoomId"), data.get("movedToTaskId")
            if not data.get("retired") or not target or not moved:
                return "task", cid, tid, path
            cid, tid = target, moved
        return group

    def ensure_gallery_deletions(self):
        """Only proven legacy deletions acquire a persistent lineage identity."""
        previous = self.state.get("gallery_deletions", [])
        retained = []
        for entry in previous:
            if not isinstance(entry, dict) or not isinstance(entry.get("origin"), dict):
                continue
            origin, path, cutoff = entry["origin"], entry.get("path"), entry.get("through_seq")
            if (not isinstance(path, str) or type(cutoff) is not int or not 0 < cutoff <= self.state["publication_counter"]
                    or origin.get("publication_seq") != cutoff):
                continue
            lineage = entry.get("lineage")
            artifact_id = entry.get("artifact_id")
            if (isinstance(artifact_id, str) and re.fullmatch(r"[0-9a-f-]{36}", artifact_id)
                    and isinstance(lineage, list) and all(isinstance(value, str) and value for value in lineage)):
                if ((len(lineage) == 4 and lineage[0] == "task" and lineage[2] == origin.get("task_id"))
                        or (len(lineage) == 3 and lineage[0] == "session" and lineage[1] == origin.get("session_id"))):
                    if lineage[-1] == path:
                        retained.append(entry)
                continue
            # Old metadata stored only an origin. Do not turn a fabricated
            # origin/path into authority over another producer's live pages.
            session = self.aibo.state["sessions"].get(origin.get("session_id"))
            if not session or session["channel_id"] != origin.get("channel_id"):
                continue
            if origin.get("task_id") and (session.get("task_id", session.get("trigger_id")) != origin["task_id"]
                                          or origin.get("task_channel_id") != session["channel_id"]):
                continue
            message = next((message for message in session["messages"] if message["id"] == origin.get("message_id")
                            and message.get("author_session_id") == session["id"]
                            and message.get("thread_root_id") == origin.get("root_id")), None)
            if message is None or origin.get("publication_seq") != cutoff:
                continue
            output = next((output for output in message.get("attached_outputs", [])
                           if output.get("id") == origin.get("attached_output_id")
                           and output.get("provenance_reply_id") == message["id"]
                           and output.get("kind") == "frame" and output.get("path") == path
                           and isinstance(output.get("frame_slug"), str)
                           and re.fullmatch(r"[0-9a-f-]{36}", output["frame_slug"])
                           and output.get("href") == "https://claude.ai/code/artifact/" + output["frame_slug"]), None)
            if output is None or output["frame_slug"] in self.state["files"] or (self.output_directory / (output["frame_slug"] + ".bin")).exists():
                continue
            retained.append({**entry, "lineage": list(self.page_lineage({"path": path}, origin)[0]),
                             "artifact_id": output["frame_slug"]})
        if retained != previous:
            self.state["gallery_deletions"] = retained
            self.save()

    def page_favorite(self, record):
        groups = self.page_groups(record)
        return record["id"] in self.state["favorites"] or any(
            groups & self.page_groups(self.state["files"][ident])
            for ident in self.state["favorites"] if ident in self.state["files"])

    def current_pages(self, published):
        newest = {}
        deleted = {}
        self.ensure_gallery_deletions()
        for entry in self.state.get("gallery_deletions", []):
            group = self.resolve_page_lineage(entry["lineage"])
            deleted[group] = max(deleted.get(group, 0), entry["through_seq"])
        for record in published:
            for origin in self.valid_provenance(record):
                group = self.page_lineage(record, origin)[0]
                rank = (origin.get("publication_seq", 0), record["id"])
                if rank[0] <= deleted.get(group, -1):
                    continue
                if group not in newest or rank > newest[group][0]:
                    newest[group] = rank, record
        return {record["id"]: record for rank, record in newest.values()}.values()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".json.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(self.state, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.path)

    def connected(self):
        return self.engine is not None and self.root is not None and self.root.is_dir()

    def require_engine(self):
        if not self.connected():
            raise WorkspaceError("unavailable", "Local execution engine is not connected", 503)

    def require_channel(self, channel_id):
        if not self.home.channel(channel_id):
            raise WorkspaceError("not_found", "Unknown local channel", 404)

    def parts(self, name):
        if (not isinstance(name, str) or not name or len(name) > 1024
                or name.startswith("/") or "\\" in name or ":" in name
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in name)):
            raise WorkspaceError("invalid_argument", "A relative workspace path is required")
        parts = name.split("/")
        if any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255 for part in parts):
            raise WorkspaceError("invalid_argument", "Invalid workspace path")
        return parts

    @contextmanager
    def parent(self, name):
        """Linux walks directory descriptors without following links, including races.

        Windows fallback rejects symlinks/junctions at every existing component;
        production is Linux and uses openat/O_NOFOLLOW for each component.
        """
        self.require_engine()
        parts = self.parts(name)
        descriptor = None
        try:
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                yield descriptor, parts[-1]
            else:
                target = self.root
                for part in parts:
                    target = target / part
                    if is_link(target):
                        raise WorkspaceError("forbidden", "Linked workspace paths are not served", 403)
                if not target.resolve().is_relative_to(self.root):
                    raise WorkspaceError("forbidden", "Path is outside workspace", 403)
                yield None, target
        except FileNotFoundError:
            raise WorkspaceError("not_found", "Workspace file or directory not found", 404)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
                raise WorkspaceError("forbidden", "Linked or inaccessible workspace path", 403)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def mime(self, name):
        self.parts(name)
        mime = MIMES.get(PurePosixPath(name).suffix.lower())
        if mime is None:
            raise WorkspaceError("unsupported_media_type", "This file type is not served", 415)
        return mime

    def metadata(self, name):
        self.mime(name)
        with self.parent(name) as (parent, leaf):
            descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceError("forbidden", "Only regular files are served", 403)
                if metadata.st_size > MAX_FILE:
                    raise WorkspaceError("too_large", "Workspace file exceeds 20 MiB", 413)
                return metadata
            finally:
                os.close(descriptor)

    def read(self, name):
        self.require_public_file(name)
        return self.read_bytes(name)

    def require_public_file(self, name):
        if self.private_key(name) in self.state["private_files"]:
            raise WorkspaceError("not_found", "No public workspace file exists at this path", 404)

    def private_key(self, name):
        self.parts(name)
        return name.casefold() if os.name == "nt" else name

    def read_bytes(self, name, *, served_type=True):
        """Internal bytes; callers must separately authorize private promotion."""
        self.parts(name)
        mime = self.mime(name) if served_type else "application/octet-stream"
        with self.parent(name) as (parent, leaf):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(leaf, flags, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceError("forbidden", "Only regular workspace files are served", 403)
                if metadata.st_size > MAX_FILE:
                    raise WorkspaceError("too_large", "Workspace file exceeds 20 MiB", 413)
                content = source.read(MAX_FILE + 1)
                if len(content) > MAX_FILE:
                    raise WorkspaceError("too_large", "Workspace file exceeds 20 MiB", 413)
        return content, mime, metadata

    def write(self, name, content, expected=None, create=False):
        self.mime(name)
        if len(content) > MAX_TEXT:
            raise WorkspaceError("too_large", "Saved content exceeds 2 MiB", 413)
        with self.parent(name) as (parent, leaf):
            try:
                existing = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise WorkspaceError("forbidden", "Cannot overwrite a linked or non-regular file", 403)
            if create and existing is not None:
                raise WorkspaceError("conflict", "Workspace file already exists", 409)
            if expected is not None:
                current = hashlib.sha256(self.read_bytes(name)[0]).hexdigest() if existing else None
                if expected.strip('"') != current:
                    raise WorkspaceError("conflict", "File changed since it was read", 409)
            temporary = ".duet-save-" + uuid4().hex
            temp_leaf = temporary if parent is not None else Path(leaf).parent / temporary
            descriptor = os.open(temp_leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_leaf, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temp_leaf, dir_fd=parent)
                except FileNotFoundError:
                    pass
        key = self.private_key(name)
        if key in self.state["private_files"]:
            del self.state["private_files"][key]
            self.save()  # An actual explicit HTTP write, not merely a new turn.

    def delete(self, name):
        self.mime(name)
        with self.parent(name) as (parent, leaf):
            metadata = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceError("forbidden", "Only regular workspace files can be deleted", 403)
            os.unlink(leaf, dir_fd=parent)
        key = self.private_key(name)
        if key in self.state["private_files"]:
            del self.state["private_files"][key]
            self.save()

    def write_visibility(self, session_id, tool_id, name, *, private, completed=False, expected_text=None, aborted=False, reservation=None):
        """Seal before private writes; only a successful ordinary write promotes.

        A path stays private across changes/restart until an explicit normal
        write or publication. Changing bytes or ending the routine cannot
        silently make a draft discoverable. Immutable published snapshots keep
        their previous public bytes when the live path is privately overwritten.
        """
        self.require_engine()
        if not isinstance(name, str):
            raise WorkspaceError("invalid_argument", "A workspace path is required")
        relative = workspace_path(self.root, name).relative_to(self.root).as_posix()
        key = self.private_key(relative)
        if not private and (not completed or key not in self.state["private_files"]):
            return
        session = self.aibo.state["sessions"].get(session_id)
        if session is None or session["phase"] != "running" or not isinstance(tool_id, str):
            raise WorkspaceError("conflict", "A real active write operation is required", 409)
        if self.aibo.is_private_run(session) != private or self.engine.native_tools.get(session_id, {}).get("id") != tool_id:
            raise WorkspaceError("conflict", "Workspace write owner changed", 409)
        self.require_channel(session["channel_id"])
        def digest():
            try:
                return hashlib.sha256(self.read_bytes(relative, served_type=False)[0]).hexdigest()
            except WorkspaceError as error:
                if error.code == "not_found":
                    return None
                raise
        if aborted:
            # Restore only a proven unchanged file and our own reservation.
            # A partial write or external change remains sealed after failure.
            if (reservation is not None and self.state["private_files"].get(key) is reservation["marker"]
                    and digest() == reservation["before_sha256"]):
                if reservation["previous"] is None:
                    self.state["private_files"].pop(key, None)
                else:
                    self.state["private_files"][key] = reservation["previous"]
                self.save()
            return
        if private:
            previous = self.state["private_files"].get(key)
            before = digest()
            marker = {"path": relative, "session_id": session_id, "message_id": session["user_id"],
                "tool_call_id": tool_id, "marked_at": utc_now()}
            self.state["private_files"][key] = marker
            try:
                self.save()  # Must succeed before the underlying write executes.
            except BaseException:
                if previous is None:
                    self.state["private_files"].pop(key, None)
                else:
                    self.state["private_files"][key] = previous
                raise
            return {"marker": marker, "previous": previous, "before_sha256": before}
        elif completed:
            if not isinstance(expected_text, str) or digest() != hashlib.sha256(expected_text.encode("utf-8")).hexdigest():
                raise WorkspaceError("conflict", "File changed before its explicit write was confirmed", 409)
            del self.state["private_files"][key]
            self.save()

    def index(self):
        self.require_engine()
        files = []
        capped = False
        directories = 0
        for directory, folders, names in os.walk(self.root, followlinks=False):
            directories += 1
            if directories > MAX_FILES:
                capped = True
                break
            folders[:] = sorted(name for name in folders if not name.startswith(".")
                                and not is_link(Path(directory) / name))
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                if self.private_key(relative) in self.state["private_files"]:
                    continue
                if name.startswith(".") or path.suffix.lower() not in MIMES:
                    continue
                try:
                    metadata = self.metadata(relative)
                except WorkspaceError:
                    continue
                ident = str(uuid5(NAMESPACE_URL, "duet-workspace:" + relative))
                old = self.state["files"].get(ident, {})
                record = {**old, "id": ident, "path": relative,
                          "created_at": old.get("created_at", iso_time(metadata.st_ctime)),
                          "updated_at": iso_time(metadata.st_mtime), "size": metadata.st_size}
                if old.get("updated_at") != record["updated_at"] or old.get("size") != record["size"]:
                    record.pop("office_preview", None)
                self.state["files"][ident] = record
                files.append(record)
                if len(files) >= MAX_FILES:
                    capped = True
                    break
            if capped:
                break
        self.save()
        return files, capped

    def artifact(self, ident):
        if not isinstance(ident, str):
            raise WorkspaceError("not_found", "Unknown local artifact", 404)
        record = self.state["files"].get(ident)
        if record is None:
            self.index()
            record = self.state["files"].get(ident)
        if record is None:
            raise WorkspaceError("not_found", "Unknown local artifact", 404)
        self.artifact_content(record)
        return record

    def artifact_content(self, record):
        if not record.get("content_revision"):
            return self.read(record["path"])
        ident = record["id"]
        if not re.fullmatch(r"[0-9a-f-]{36}", ident) or is_link(self.output_directory):
            raise WorkspaceError("forbidden", "Invalid output snapshot", 403)
        path = self.output_directory / (ident + ".bin")
        if is_link(path):
            raise WorkspaceError("forbidden", "Linked output snapshots are not served", 403)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE:
                    raise WorkspaceError("forbidden", "Invalid output snapshot", 403)
                content = source.read(MAX_FILE + 1)
        except FileNotFoundError:
            raise WorkspaceError("not_found", "Output snapshot no longer exists", 404)
        if len(content) > MAX_FILE or hashlib.sha256(content).hexdigest() != record["content_revision"]:
            raise WorkspaceError("conflict", "Output snapshot integrity check failed", 409)
        return content, self.mime(record["path"]), metadata

    def valid_provenance(self, record):
        valid = []
        for origin in record.get("provenance", []):
            session = self.aibo.state["sessions"].get(origin.get("session_id"))
            if not session or session["channel_id"] != origin.get("channel_id"):
                continue
            message = next((m for m in session["messages"] if m["id"] == origin.get("message_id")
                            and m.get("thread_root_id") == origin.get("root_id") and m.get("author_session_id") == session["id"]), None)
            allowed = {record["id"]}  # Legacy UUID projection during upgrade.
            handle = origin.get("attached_output_id")
            if isinstance(handle, str) and re.fullmatch(re.escape(origin["message_id"]) + r"\.[0-9]+", handle):
                allowed.add(handle)
            if message and any(isinstance(output.get("id"), str) and output.get("id") in allowed
                               and output.get("provenance_reply_id") == origin["message_id"]
                               for output in message.get("attached_outputs", [])):
                valid.append(origin)
        return valid

    def task_for_origin(self, origin, session):
        if self.tasks is None or not origin.get("task_id") or session.get("task_id", session.get("trigger_id")) != origin["task_id"]:
            return None
        cid, tid = origin["task_channel_id"], origin["task_id"]
        visited = set()
        while (cid, tid) not in visited:
            visited.add((cid, tid))
            task = self.tasks.state["channels"].get(cid, {}).get(tid)
            if task is None:
                return None
            if not task["data"].get("retired"):
                return cid, tid, task
            next_cid, next_tid = task["data"].get("movedToRoomId"), task["data"].get("movedToTaskId")
            if not next_cid or not next_tid:
                return None
            cid, tid = next_cid, next_tid
        return None

    def output_belongs_to_channel(self, record, channel_id):
        for origin in self.valid_provenance(record):
            if origin["channel_id"] == channel_id:
                return True
            session = self.aibo.state["sessions"][origin["session_id"]]
            found = self.task_for_origin(origin, session)
            # threadPages is the HTML presentation list, not an authorization
            # list. Valid publication provenance follows the task's move chain
            # equally for attached notes, PDFs, and HTML pages.
            if found and found[0] == channel_id:
                return True
        return False

    def output_handle(self, record, origin):
        """Persist a native attached-output handle, distinct from artifact UUID."""
        session = self.aibo.state["sessions"].get(origin["session_id"])
        message = next((item for item in (session or {}).get("messages", [])
                        if item["id"] == origin["message_id"]
                        and item.get("author_session_id") == origin["session_id"]
                        and item.get("thread_root_id") == origin["root_id"]), None)
        if message is None or not re.fullmatch(r"cmsg_[A-Za-z0-9_-]{1,120}", message["id"]):
            raise WorkspaceError("not_found", "Output reply no longer exists", 404)
        prefix = message["id"] + "."
        assigned = origin.get("attached_output_id")
        if assigned is not None and not re.fullmatch(re.escape(prefix) + r"[0-9]+", assigned):
            raise WorkspaceError("conflict", "Invalid persisted output handle", 409)
        occupied = set()
        for candidate in self.state["files"].values():
            for prior in candidate.get("provenance", []):
                value = prior.get("attached_output_id")
                if prior is origin or prior.get("message_id") != message["id"] or not value:
                    continue
                if assigned == value:
                    raise WorkspaceError("conflict", "Output handle belongs to multiple publications", 409)
                if re.fullmatch(re.escape(prefix) + r"[0-9]+", value):
                    occupied.add(int(value[len(prefix):]))
        if assigned is not None:
            return assigned
        outputs = message.get("attached_outputs", [])
        occupied.update(int(item["id"][len(prefix):]) for item in outputs
                        if isinstance(item.get("id"), str) and re.fullmatch(re.escape(prefix) + r"[0-9]+", item["id"]))
        # Recover legacy UUID entries in their original reply order. Missing
        # entries append after all reserved handles; deleted slots never move.
        preferred = next((index for index, item in enumerate(outputs)
                          if item.get("id") == record["id"] and item.get("provenance_reply_id") == message["id"]), None)
        ordinal = preferred if preferred is not None and preferred not in occupied else max(occupied, default=-1) + 1
        origin["attached_output_id"] = prefix + str(ordinal)
        self.save()  # Persist the binding before publishing a reference to it.
        return origin["attached_output_id"]

    def output_wire(self, record, origin):
        name = record["path"]
        is_frame = self.is_make_frame(record)
        pdf = self.pdf_preview(record) if file_kind(name) == "pdf" else None
        output = {"id": self.output_handle(record, origin), "kind": "frame" if is_frame else "file", "filename": PurePosixPath(name).name,
                  "provenance_reply_id": origin["message_id"], "created_at": origin["created_at"],
                  "delivery_state": "delivered", "size_bytes": record["size"], "has_thumbnail": False,
                  "file_kind": file_kind(name), "has_preview": is_frame or bool(pdf and pdf.get("status") == "ready"), "path": name}
        if is_frame:
            output["type_slug"] = wire_type(self.make_type(record))
            output.update(href="https://claude.ai/code/artifact/" + record["id"], frame_slug=record["id"])
        return output

    def pdf_preview(self, record):
        """Projection/startup code reads cached metadata and never runs a parser."""
        metadata = record.get("pdf_preview")
        if metadata is not None and record.get("content_revision") and metadata.get("content_revision") == record["content_revision"]:
            return metadata
        return {"status": "not_checked", "reason": "PDF preview has not been validated"}

    async def inspect_pdf(self, content, name):
        try:
            metadata = await asyncio.to_thread(inspect_document, content, name, "application/pdf", preview=True)
            metadata.pop("text", None)
        except AttachmentError as error:
            if error.status in {429, 503}:
                raise WorkspaceError(error.code, str(error), error.status) from error
            metadata = {"status": "invalid", "code": error.code, "reason": str(error)}
        if metadata.get("status") == "unavailable":
            raise WorkspaceError(metadata.get("code", "pdf_preview_unavailable"),
                                 metadata.get("reason", "PDF validation is temporarily unavailable"), 503)
        return metadata

    @staticmethod
    def provenance_identity(record):
        return tuple(sorted(json.dumps({key: origin.get(key) for key in ("session_id", "channel_id", "root_id", "message_id",
                             "tool_call_id", "task_id", "task_channel_id", "attached_output_id")}, sort_keys=True)
                            for origin in record.get("provenance", [])))

    async def ensure_pdf_preview(self, record, recheck=None):
        metadata = self.pdf_preview(record)
        if metadata["status"] != "not_checked":
            return metadata
        content, _, _ = self.artifact_content(record)
        revision = hashlib.sha256(content).hexdigest()
        identity, name = self.provenance_identity(record), record["path"]
        metadata = await self.inspect_pdf(content, name)
        if recheck is not None:
            recheck()
        self.require_engine()
        if (self.state["files"].get(record["id"]) is not record or record["path"] != name
                or self.provenance_identity(record) != identity
                or hashlib.sha256(self.artifact_content(record)[0]).hexdigest() != revision):
            raise WorkspaceError("conflict", "Output owner or content changed during PDF validation", 409)
        metadata["content_revision"] = revision
        if record.get("content_revision"):
            record["pdf_preview"] = metadata
            self.save()
            # A migrated PDF becomes previewable through the existing root and
            # reply update stream, rather than waiting for a new conversation.
            for origin in self.valid_provenance(record):
                self.attach_output(record, origin)
        return metadata

    def publication_owner(self, session_id, tool_id):
        session = self.aibo.state["sessions"].get(session_id)
        if session is None:
            raise WorkspaceError("not_found", "Unknown output session", 404)
        if self.aibo.is_private_run(session):
            raise WorkspaceError("forbidden", "Private background drafts are not published as workspace outputs", 403)
        self.require_channel(session["channel_id"])
        assistant = next((m for m in reversed(session["messages"])
                          if m.get("author_session_id") == session_id and not m.get("server_notice") and not m.get("_complete")), None)
        if assistant is None or session["phase"] != "running":
            raise WorkspaceError("conflict", "Output needs an active assistant reply", 409)
        tools = getattr(self.engine, "native_tools", None)
        active_tool = tools.get(session_id, {}).get("id") if tools is not None else tool_id
        if active_tool != tool_id:
            raise WorkspaceError("conflict", "Output tool owner changed during validation", 409)
        return (session_id, session["channel_id"], assistant["id"], assistant["thread_root_id"], session.get("task_id"),
                session.get("trigger_id"), self.home.channel(session["channel_id"]).get("created_by_account_id"), active_tool)

    def publication_source_current(self, origin):
        session = self.aibo.state["sessions"].get(origin["session_id"])
        if not session or session["channel_id"] != origin["channel_id"] or not any(
                item["id"] == origin["message_id"] and item.get("author_session_id") == origin["session_id"]
                and item.get("thread_root_id") == origin["root_id"] for item in session["messages"]):
            raise WorkspaceError("conflict", "Output publication owner no longer exists", 409)
        self.require_channel(origin["channel_id"])

    async def publish_output(self, session_id, tool_id, name, title=None, *, expected_revision=None, recheck=None):
        self.require_engine()
        self.parts(name)
        if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 256:
            raise WorkspaceError("invalid_argument", "An actual tool call ID is required")
        session = self.aibo.state["sessions"].get(session_id)
        if session is None:
            raise WorkspaceError("not_found", "Unknown output session", 404)
        if self.aibo.is_private_run(session):
            raise WorkspaceError("forbidden", "Private background drafts are not published as workspace outputs", 403)
        self.require_channel(session["channel_id"])
        assistant = next((m for m in reversed(session["messages"])
                          if m.get("author_session_id") == session_id and not m.get("server_notice") and not m.get("_complete")), None)
        # Retries use the original immutable revision and its message. A reused
        # tool ID in another turn must never attach that output to the new turn.
        for record in self.state["files"].values():
            for origin in record.get("provenance", []):
                if origin["session_id"] == session_id and origin["tool_call_id"] == tool_id:
                    if (record["path"] != name or (assistant and assistant["id"] != origin["message_id"])
                            or (expected_revision and record.get("content_revision") != expected_revision)):
                        raise WorkspaceError("conflict", "Tool call was already used for another output", 409)
                    if file_kind(name) == "pdf":
                        def current():
                            if recheck is not None:
                                recheck()
                            self.publication_source_current(origin)
                        await self.ensure_pdf_preview(record, current)
                    if recheck is not None:
                        recheck()
                    self.publication_source_current(origin)
                    self.artifact_content(record)
                    self.attach_output(record, origin)
                    return {"published": True, "artifact": self.output_wire(record, origin)}
        if assistant is None or session["phase"] != "running":
            raise WorkspaceError("conflict", "Output needs an active assistant reply", 409)
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200):
            raise WorkspaceError("invalid_argument", "Output title must be 1..200 characters")
        content, _, _ = self.read_bytes(name)
        revision = hashlib.sha256(content).hexdigest()
        if expected_revision is not None and revision != expected_revision:
            raise WorkspaceError("conflict", "File changed before its output was registered", 409)
        office = None
        if make_family(name) in FORMATS:
            owner = self.publication_owner(session_id, tool_id)
            office = await asyncio.to_thread(self.office_metadata, {"path": name}, content)
            if recheck is not None:
                recheck()
            self.require_engine()
            if (self.publication_owner(session_id, tool_id) != owner
                    or self.aibo.state["sessions"].get(session_id) is not session
                    or hashlib.sha256(self.read_bytes(name)[0]).hexdigest() != revision):
                raise WorkspaceError("conflict", "File or publication owner changed during Office validation", 409)
        pdf = None
        if file_kind(name) == "pdf":
            owner = self.publication_owner(session_id, tool_id)
            pdf = await self.inspect_pdf(content, name)
            if recheck is not None:
                recheck()
            self.require_engine()
            if (self.publication_owner(session_id, tool_id) != owner
                    or self.aibo.state["sessions"].get(session_id) is not session
                    or hashlib.sha256(self.read_bytes(name)[0]).hexdigest() != revision):
                raise WorkspaceError("conflict", "File or publication owner changed during PDF validation", 409)
            pdf["content_revision"] = revision
        now = utc_now()
        origin = {"session_id": session_id, "channel_id": session["channel_id"], "root_id": assistant["thread_root_id"],
                  "message_id": assistant["id"], "tool_call_id": tool_id, "created_at": now}
        found = self.tasks.record_for_session(session_id) if self.tasks else None
        if found:
            origin.update(task_id=found[1], task_channel_id=found[0])
            request = found[2].get("make_request", {})
            requested_type = resolve_type(request.get("artifact_type_id"))
            if not requested_type and request.get("page") in self.state["files"]:
                requested_type = self.make_type(self.state["files"][request["page"]])
            if make_family(name) == "page" and requested_type in WEB_FORMATS:
                origin["make_type"] = requested_type
        identity = "duet-output:" + name + ":" + revision
        # An explicit HTML family is immutable artifact metadata. Identical
        # bytes reused by another family must not rewrite historical type.
        # Keep the established UUID rule for ordinary pages and other files.
        if origin.get("make_type"):
            identity += ":make-family:" + origin["make_type"]
        ident = str(uuid5(NAMESPACE_URL, identity))
        old = self.state["files"].get(ident, {})
        if recheck is not None:
            recheck()
        self.output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_link(self.output_directory):
            raise WorkspaceError("forbidden", "Linked output store is not allowed", 403)
        snapshot = self.output_directory / (ident + ".bin")
        if not snapshot.exists():
            temporary = self.output_directory / (".output-" + uuid4().hex)
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, snapshot)
            finally:
                temporary.unlink(missing_ok=True)
        record = {**old, "id": ident, "path": name, "created_at": old.get("created_at", now), "updated_at": now,
                  "size": len(content), "content_revision": revision, "provenance": [*old.get("provenance", []), origin]}
        if origin.get("make_type"):
            record["make_type"] = origin["make_type"]
        if office is not None:
            record["office_preview"] = office
        if pdf is not None:
            record["pdf_preview"] = pdf
        if title:
            record["title"] = title.strip()
        self.artifact_content(record)
        if make_family(name) is not None:
            previous_counter = self.state["publication_counter"]
            self.state["publication_counter"] = previous_counter + 1
            origin["publication_seq"] = self.state["publication_counter"]
            self.state["files"][ident] = record
            try:
                self.save()
            except BaseException:
                self.state["publication_counter"] = previous_counter
                if old:
                    self.state["files"][ident] = old
                else:
                    self.state["files"].pop(ident, None)
                raise
        else:
            self.state["files"][ident] = record
            self.save()
        self.attach_output(record, origin)
        key = self.private_key(name)
        if key in self.state["private_files"]:
            del self.state["private_files"][key]
            self.save()  # Only this real successful publication promotes bytes.
        return {"published": True, "artifact": self.output_wire(record, origin)}

    def attach_output(self, record, origin):
        session = self.aibo.state["sessions"].get(origin["session_id"])
        if session is None or session["channel_id"] != origin["channel_id"]:
            raise WorkspaceError("not_found", "Output session no longer exists", 404)
        message = next((m for m in session["messages"] if m["id"] == origin["message_id"]
                        and m.get("thread_root_id") == origin["root_id"] and m.get("author_session_id") == session["id"]), None)
        if message is None:
            raise WorkspaceError("not_found", "Output reply no longer exists", 404)
        # ChannelMessageWire.attached_outputs on a thread root is the native
        # resource projection: e.K4 only feeds root messages (id == root_id)
        # through vds.d0 into j6v.h/i. Keep the real reply's provenance while
        # also exposing its actual output on that same conversation's root.
        root = next((item for item in session["messages"] if item["id"] == origin["root_id"]
                     and item.get("thread_root_id") == origin["root_id"]), None)
        changed_messages = []
        wire = self.output_wire(record, origin)
        for target in (message, root):
            if target is None or target in changed_messages:
                continue
            outputs = target.setdefault("attached_outputs", [])
            existing = next((index for index, item in enumerate(outputs)
                             if item.get("id") == wire["id"] or
                             (item.get("id") == record["id"] and item.get("provenance_reply_id") == origin["message_id"])), None)
            if existing is None:
                outputs.append(wire.copy())
                changed_messages.append(target)
            elif outputs[existing] != wire:
                outputs[existing] = wire.copy()
                changed_messages.append(target)
        found = self.tasks.record_for_session(session["id"]) if self.tasks else None
        if found is None and self.tasks and origin.get("task_id"):
            # Recover a publication interrupted between metadata and task save,
            # even if the task subsequently moved. Follow actual move records.
            found = self.task_for_origin(origin, session)
        if found and self.is_make_frame(record):
            pages = found[2]["data"].setdefault("threadPages", [])
            if not any(item.get("artifactId") == record["id"] for item in pages):
                pages.append({"artifactId": record["id"]})
                found[2]["updated_at"] = utc_now()
                self.tasks.save()
        if changed_messages:
            for changed in changed_messages:
                if changed.get("_complete") or changed.get("_published"):
                    changed["edited_at"] = message_time(utc_now(), changed.get("edited_at", changed["created_at"]))
                    self.aibo._channel_event({**session, "root_id": changed["thread_root_id"]},
                                             "channel_message_updated", self.aibo.message_wire(changed))
            self.aibo.save()

    def reconcile_outputs(self):
        for record in self.state["files"].values():
            if not record.get("content_revision"):
                continue
            try:
                self.artifact_content(record)
            except WorkspaceError:
                continue
            for origin in record.get("provenance", []):
                try:
                    self.attach_output(record, origin)
                except WorkspaceError:
                    # A deliberately deleted conversation/file has no source to
                    # restore; never manufacture a replacement session/message.
                    continue

    async def written_output(self, session_id, tool_id, name, *, expected_text=None):
        try:
            revision = hashlib.sha256(expected_text.encode("utf-8")).hexdigest() if expected_text is not None else None
            return await self.publish_output(session_id, tool_id, name, expected_revision=revision)
        except WorkspaceError as error:
            # A successful write stays successful even when its format cannot
            # be previewed or the output metadata cannot be registered.
            return {"published": False, "reason": error.code}
        except OSError:
            return {"published": False, "reason": "output_registration_failed"}

    def active_count(self):
        return sum(item.get("phase") == "running" for item in self.aibo.state["sessions"].values())

    async def folders(self, request):
        await object_body(request)
        computers = []
        if self.connected():
            computers = [{"name": self.state["name"], "computer_key": self.state["environment_id"],
                          "folders": [{"environment_id": self.state["environment_id"], "label": "workspace",
                                       "live_sessions": self.active_count(), "max_sessions": MAX_SESSIONS,
                                       "can_pick": self.active_count() < MAX_SESSIONS}]}]
        result = {"state": "ok" if computers else "no_computers", "mode": "manual", "computers": computers}
        repositories = getattr(self.aibo, "repositories", None)
        if repositories is not None and repositories.ready():
            result["cloud"] = {"environment_id": self.state["environment_id"]}
        return result

    async def computers(self, request):
        await object_body(request)
        items = []
        if self.connected():
            items = [{"computer_key": self.state["environment_id"], "alias_keys": [], "kind": "yours",
                      "name": self.state["name"], "own_name": self.state["name"], "machine_name": socket.gethostname(),
                      "reach": "on", "counts": {"folders": 1},
                      "access": {"folders": [{"name": "workspace", "on": True}]}, "can": ["rename", "start_here"]}]
        return {"state": "ok" if items else "none", "checked_at": utc_now(),
                "headline": {"up": len(items), "of": len(items)}, "computers": items}

    async def rename(self, request):
        self.require_engine()
        body = await object_body(request)
        keys, name = body.get("keys"), body.get("name")
        if not isinstance(keys, list) or keys != [self.state["environment_id"]]:
            return {"state": "gone"}
        if (not isinstance(name, str) or not name.strip() or len(name) > 200
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
            return {"state": "name_refused"}
        self.state["name"] = name.strip()
        self.save()
        return {"state": "ok", "name": self.state["name"]}

    def environment_resource(self):
        return {"kind": "bridge", "environment_id": self.state["environment_id"], "name": self.state["name"],
                "created_at": self.state["created_at"], "state": "active", "owner_account_uuid": ACCOUNT_ID,
                "bridge_info": {"machine_name": socket.gethostname(), "machine_id": self.state["environment_id"],
                                "directory": "workspace", "online": True, "max_sessions": MAX_SESSIONS, "runner": "duet"}}

    async def environment_list(self, request):
        self.organization(request)
        environments = [self.environment_resource()] if self.connected() and not request.query.get("after_id") else []
        return {"environments": environments, "has_more": False,
                "first_id": environments[0]["environment_id"] if environments else None,
                "last_id": environments[-1]["environment_id"] if environments else None}

    async def network_environments(self, request):
        # The engine exposes its own execution policy; no remote CCR controls are offered.
        return {"environments": [], "full_access_offered": False, "level_descriptions": [],
                "network_apply_note": "本地执行环境的网络权限由服务器配置管理。"}

    def organization(self, request):
        organization = request.match_info.get("organization") or request.query.get("org")
        if organization is not None and organization != ORGANIZATION_ID:
            raise WorkspaceError("not_found", "Unknown local organization", 404)

    async def desktop(self, request):
        if not self.connected():
            return {"connected": False, "ready": False, "reason": "Local engine is not connected", "view_url": None}
        capabilities = dict(await self.engine.desktop_capabilities())
        capabilities.update(connected=True, environment_id=self.state["environment_id"],
                            view_url="/desktop/view" if capabilities.get("ready") else None)
        return capabilities

    async def files(self, request):
        channel = request.match_info["channel_id"]
        self.require_channel(channel)
        body = await object_body(request)
        files, capped = self.index()
        query, kind = body.get("q", ""), body.get("kind")
        if query is None:
            query = ""
        if not isinstance(query, str) or len(query) > 512:
            raise WorkspaceError("invalid_argument", "Invalid file query")
        items = [{"path": item["path"], "name": PurePosixPath(item["path"]).name,
                  "kind": file_kind(item["path"]), "size_bytes": item["size"],
                  "modified_at": item["updated_at"], "has_thumbnail": False, "channel_id": channel}
                 for item in files if query.casefold() in item["path"].casefold()
                 and (not kind or file_kind(item["path"]) == kind)]
        limit = body.get("limit")
        limit = 100 if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= 500:
            raise WorkspaceError("invalid_argument", "limit must be 1..500")
        cursor = body.get("cursor")
        if cursor:
            position = next((i for i, item in enumerate(items) if item["path"] == cursor), None)
            if position is None:
                raise WorkspaceError("invalid_argument", "Unknown file cursor")
            items = items[position + 1:]
        selected = items[:limit]
        return {"files": selected, "next_cursor": selected[-1]["path"] if len(items) > limit else None,
                "truncated": capped, "channels_searched": 1, "channels_skipped": 0}

    async def file_content(self, name, record=None, *, range_offset=None, range_length=None, rendition=None, recheck=None):
        content, mime, _ = self.artifact_content(record) if record else self.read(name)
        result = {"filename": PurePosixPath(name).name, "kind": file_kind(name), "size_bytes": len(content),
                  "mime_type": mime, "content_revision": hashlib.sha256(content).hexdigest()}
        if mime == "application/pdf":
            if record:
                pdf = await self.ensure_pdf_preview(record, recheck)
            else:
                pdf = await self.inspect_pdf(content, name)
                if recheck is not None:
                    recheck()
                if hashlib.sha256(self.read(name)[0]).hexdigest() != result["content_revision"]:
                    raise WorkspaceError("conflict", "File changed during PDF validation", 409)
            if pdf.get("status") == "ready":
                result["page_count"] = pdf["page_count"]
            elif rendition is not None:
                return {**result, "type": "unsupported", "reason": pdf.get("reason", "PDF preview is unavailable")}
        if range_offset is not None or range_length is not None:
            offset = 0 if range_offset is None else range_offset
            if type(offset) is not int or offset < 0 or (range_length is not None and (type(range_length) is not int or range_length <= 0)):
                raise WorkspaceError("invalid_argument", "range_offset must be nonnegative and range_length positive integers")
            length = len(content) - offset if range_length is None else range_length
            if offset > len(content) or length > MAX_FILE:
                raise WorkspaceError("invalid_range", "Requested file range is not satisfiable", 416)
            chunk = content[offset:offset + length]
            # Explicit ranges contain raw bytes; splitting a UTF-8 code point
            # must not corrupt a text preview or silently round its boundaries.
            return {**result, "type": "download", "data": base64.b64encode(chunk).decode("ascii"),
                    "size_bytes": len(chunk), "total_size_bytes": len(content), "range_offset": offset,
                    "range_length": len(chunk)}
        if len(content) <= MAX_TEXT and (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
            try:
                result.update(type="text", content=content.decode("utf-8"))
                return result
            except UnicodeDecodeError:
                pass
        result.update(type="download", data=base64.b64encode(content).decode("ascii"))
        return result

    async def open_file(self, request):
        channel_id = request.match_info["channel_id"]
        self.require_channel(channel_id)
        owner = self.home.channel(channel_id).get("created_by_account_id")
        def recheck():
            self.require_channel(channel_id)
            self.request_current(request)
            if self.home.channel(channel_id).get("created_by_account_id") != owner:
                raise WorkspaceError("forbidden", "Channel owner changed during file read", 403)
        body = await object_body(request)
        result = await self.file_content(body.get("path"), range_offset=body.get("range_offset"), range_length=body.get("range_length"), recheck=recheck)
        recheck()
        return result

    @staticmethod
    def request_current(request):
        authorized = request.get("duet_authorized")
        if authorized is not None and not authorized():
            raise WorkspaceError("unauthorized", "Login expired during file validation", 401)

    async def attached(self, request):
        channel_id = request.match_info["channel_id"]
        self.require_channel(channel_id)
        handle = request.match_info["artifact_id"]
        if re.fullmatch(r"cmsg_[A-Za-z0-9_-]{1,120}\.[0-9]+", handle):
            records = [record for record in self.state["files"].values()
                       if any(origin.get("attached_output_id") == handle for origin in self.valid_provenance(record))]
            if len(records) != 1:
                raise WorkspaceError("not_found", "Unknown attached output", 404)
            record = records[0]
        else:
            # Existing local consumers used artifact UUIDs here. Keep those
            # authenticated reads working without treating UUIDs as native IDs.
            record = self.artifact(handle)
        if not self.output_belongs_to_channel(record, channel_id):
            raise WorkspaceError("not_found", "Output does not belong to this channel", 404)
        rendition = request.query.get("rendition")
        if rendition not in (None, "display", "thumbnail"):
            raise WorkspaceError("invalid_argument", "Unknown file rendition")
        if rendition == "thumbnail":
            # No invented raster thumbnail is returned for a PDF/file output.
            return {"type": "unsupported", "reason": "This output has no thumbnail"}
        def recheck():
            self.require_channel(channel_id)
            self.request_current(request)
            if not self.output_belongs_to_channel(record, channel_id):
                raise WorkspaceError("not_found", "Output no longer belongs to this channel", 404)
        result = await self.file_content(record["path"], record, rendition=rendition, recheck=recheck)
        recheck()
        return result

    async def page_source(self, request):
        self.require_channel(request.match_info["channel_id"])
        body = await object_body(request)
        page = body.get("page")
        if isinstance(page, str) and page.startswith(("http://", "https://")):
            # Parse only a known artifact URL. Never fetch it or trust its host.
            match = re.fullmatch(r"/(?:code/(?:artifact|frame)|artifact|duet/workspace/pages)/([A-Za-z0-9_-]+)", urlsplit(page).path)
            if not match:
                raise WorkspaceError("invalid_argument", "Unknown local page URL")
            page = match[1]
        record = self.artifact(page)
        if record.get("provenance") and not self.output_belongs_to_channel(record, request.match_info["channel_id"]):
            raise WorkspaceError("not_found", "Page does not belong to this channel", 404)
        content, mime, _ = self.artifact_content(record)
        if make_family(record["path"]) in FORMATS:
            content = await self.office_render(record, content)
            self.request_current(request)
            self.require_channel(request.match_info["channel_id"])
            if record.get("provenance") and not self.output_belongs_to_channel(record, request.match_info["channel_id"]):
                raise WorkspaceError("not_found", "Output no longer belongs to this channel", 404)
        elif mime != "text/html":
            raise WorkspaceError("unsupported_media_type", "This artifact is not a Make frame", 415)
        if len(content) > MAX_TEXT:
            raise WorkspaceError("too_large", "HTML source exceeds 2 MiB", 413)
        return {"source": base64.b64encode(content).decode("ascii"), "content_type": "text/html"}

    async def artifacts(self, request):
        self.organization(request)
        files, capped = self.index() if self.connected() else ([], False)
        published = [record for record in self.state["files"].values()
                     if record.get("content_revision") and make_family(record["path"]) is not None]
        published_paths = {record["path"] for record in published} | {entry["path"] for entry in self.state.get("gallery_deletions", [])}
        query = request.query.get("q", "").casefold()
        ids = request.query.get("ids", "").split(",")
        current = [record for record in files if record["path"] not in published_paths] + list(self.current_pages(published))
        files = files + published if ids != [""] else current
        files = sorted(files, key=lambda record: (record["updated_at"],
                       max((origin.get("publication_seq", 0) for origin in record.get("provenance", [])), default=0), record["id"]), reverse=True)
        relation = request.query.get("rel", "mine")
        items = [{"kind": "page", "id": item["id"], "rel": "mine", "updated_at": item["updated_at"],
                  "created_at": item["created_at"], "title": item.get("title") or PurePosixPath(item["path"]).stem,
                  "type_slug": wire_type(self.make_type(item)), "source": "local", "owner_account": ACCOUNT_ID,
                  "description": item["path"], "thumbnail_pending": False}
                 for item in files if self.is_make_frame(item)
                 and query in item["path"].casefold() and (ids == [""] or item["id"] in ids)
                 and (relation != "favorite" or self.page_favorite(item))]
        try:
            limit = int(request.query.get("limit", 100))
        except ValueError:
            raise WorkspaceError("invalid_argument", "Invalid artifact limit")
        if not 1 <= limit <= 500:
            raise WorkspaceError("invalid_argument", "limit must be 1..500")
        token = request.query.get("page_token")
        if token:
            position = next((i for i, item in enumerate(items) if item["id"] == token), None)
            if position is None:
                raise WorkspaceError("invalid_argument", "Unknown artifact cursor")
            items = items[position + 1:]
        return {"artifacts": items[:limit], "next_page_token": items[limit - 1]["id"] if len(items) > limit else None,
                "hosted_total": sum(self.is_make_frame(item) for item in current), "hosted_total_capped": capped}

    async def frame_boot(self, request):
        self.organization(request)
        self.artifact(request.match_info["artifact_id"])
        # Prevent the optimized source loader from constructing a vendor CDN URL.
        # Native pages/source or the local viewer endpoint provides the real HTML.
        return {"contentVia": "frame"}

    async def artifact_origins(self, request):
        self.organization(request)
        record = self.artifact(request.match_info["artifact_id"])
        ids = sorted({origin["session_id"] for origin in self.valid_provenance(record)})
        return {"origins": [{"kind": "cowork", "id": sid} for sid in ids]}

    async def frame_delete(self, request):
        self.organization(request)
        record = self.artifact(request.match_info["artifact_id"])
        if record.get("content_revision"):
            snapshot = self.output_directory / (record["id"] + ".bin")
            staged = self.output_directory / (".delete-" + uuid4().hex)
            previous_markers = self.state.get("gallery_deletions", [])
            favorites = self.state["favorites"]
            markers = [{"path": record["path"], "origin": dict(origin), "through_seq": origin.get("publication_seq", 0),
                        "lineage": list(self.page_lineage(record, origin)[0]), "artifact_id": record["id"]}
                       for origin in self.valid_provenance(record)]
            # No await between staging, metadata commit and removal. An I/O
            # failure restores this exact snapshot and its gallery visibility.
            os.replace(snapshot, staged)
            committed = False
            try:
                del self.state["files"][record["id"]]
                self.state["gallery_deletions"] = [*previous_markers, *markers]
                self.state["favorites"] = [item for item in favorites if item != record["id"]]
                self.save()
                committed = True
                staged.unlink()
            except BaseException:
                self.state["files"][record["id"]] = record
                self.state["gallery_deletions"] = previous_markers
                self.state["favorites"] = favorites
                os.replace(staged, snapshot)
                if committed:
                    self.save()
                raise
            return None
        else:
            self.delete(record["path"])
        self.state["favorites"] = [item for item in self.state["favorites"] if item != record["id"]]
        self.save()
        return None

    async def favorite(self, request):
        self.organization(request)
        record = self.artifact(request.match_info["artifact_id"])
        favorites = self.state["favorites"]
        if request.method == "POST" and record["id"] not in favorites:
            favorites.append(record["id"])
        elif request.method == "DELETE" and record["id"] in favorites:
            favorites.remove(record["id"])
        if request.method == "DELETE":
            groups = self.page_groups(record)
            self.state["favorites"] = [ident for ident in favorites if ident not in self.state["files"]
                                       or not (groups & self.page_groups(self.state["files"][ident]))]
        self.save()
        return None

    async def types(self, request):
        self.organization(request)
        items = [{"slug": TYPE_IDS["page"], "title": "本地页面", "icon_key": "globe", "show_in_create_menus": self.connected(), "listings": []}]
        items.extend({"slug": TYPE_IDS[kind], "title": value["title"], "icon_key": value["icon_key"],
                      "show_in_create_menus": self.connected(), "listings": []}
                     for kind, value in FORMATS.items() if format_available(kind))
        items.extend({"slug": TYPE_IDS[kind], "title": value["title"], "icon_key": value["icon_key"],
                      "show_in_create_menus": self.connected(), "listings": []} for kind, value in WEB_FORMATS.items())
        return {"types": items}

    def make_template(self, type_id):
        type_id = resolve_type(type_id)
        if not format_available(type_id):
            raise AiboError("not_implemented", "This local Make format is unavailable", 501)
        if type_id == "page":
            instructions = "Make the page the user requests as an actual self-contained UTF-8 HTML file in the private workspace. Use write_file with an .html filename, or publish_artifact after a tool actually generates the file, so it is attached to this task. "
        elif type_id in WEB_FORMATS:
            instructions = (WEB_FORMATS[type_id]["prompt"] + " Save a real self-contained UTF-8 .html file with write_file, or publish_artifact after exec generates it. "
                            "Use the existing native HTML frame viewer; include all styles and scripts locally, with no vendor CDN or invented hosted template. "
                            "The viewer cannot fetch external data, use ambient microphone access, or persist state via browser storage. "
                            "Mark temporary in-view interactions clearly. Preserve requested state in the actual HTML when publishing a revision through Make. ")
        else:
            spec = FORMATS[type_id]
            instructions = ("Make the requested " + type_id + " as a real " + spec["extension"] + " Office file in the private workspace. "
                            "Use the installed " + spec["library"] + " Python library through exec to create or edit the actual file, then call publish_artifact with its workspace path. "
                            "Do not substitute HTML or renamed text for an Office package. The native viewer attempts isolated local visual rendering and clearly marks a text fallback when unavailable; the downloadable original retains the real Office package. "
                            "When editing, verify that the existing path SHA-256 matches the supplied content_revision before writing. If it differs or the path is missing, explain that this is an older snapshot and ask which revision to edit; do not silently replace newer work. "
                            "Open the existing original with that library and preserve existing content, formatting and formulas unless the user requests a change. ")
        return {"id": TYPE_IDS[type_id], "prompt": instructions +
                "Do not claim a deliverable exists until a tool result confirms it. Preserve the complete user request, including any supplied preset details. "
                "Use only actually available tools and data; do not invent calendar, email, connector access, scheduled routines or pinning. "
                "If the request needs unavailable data, explain the specific missing access or ask the user. Publish truthful progress with update_task_progress. "
                "When existing source includes a path, edit that same workspace path unless the user asks for a separate copy. "
                "Tool execution retains its normal user approval rules."}

    def office_metadata(self, record, content):
        revision = hashlib.sha256(content).hexdigest()
        cached = record.get("office_preview", {})
        if cached.get("content_revision") == revision and cached.get("make_validation_version") == 1:
            return cached
        try:
            metadata = inspect_document(content, record["path"], self.mime(record["path"]), validate_office=True)
        except AttachmentError as error:
            raise WorkspaceError(error.code, str(error), error.status) from None
        if metadata.get("status") not in {"ready", "no_text"}:
            raise WorkspaceError(metadata.get("code", "office_preview_unavailable"),
                                 metadata.get("reason", "Office text preview is unavailable"), 503)
        return {**metadata, "content_revision": revision}

    def office_source(self, record, content):
        metadata = self.office_metadata(record, content)
        visual = render_office(content, record["path"])
        result = office_preview(record.get("title") or PurePosixPath(record["path"]).stem, metadata,
                                "/duet/workspace/artifacts/" + quote(record["id"], safe="") + "/download", visual)
        if len(result) > MAX_TEXT:
            result = office_preview(record.get("title") or PurePosixPath(record["path"]).stem, metadata,
                                    "/duet/workspace/artifacts/" + quote(record["id"], safe="") + "/download",
                                    {"status": "unavailable", "reason": "Visual HTML exceeds the native viewer source budget"})
        return result

    async def office_render(self, record, content):
        revision, identity = hashlib.sha256(content).hexdigest(), self.provenance_identity(record)
        result = await asyncio.to_thread(self.office_source, record, content)
        self.require_engine()
        if (self.state["files"].get(record["id"]) is not record
                or self.provenance_identity(record) != identity
                or hashlib.sha256(self.artifact_content(record)[0]).hexdigest() != revision):
            raise WorkspaceError("conflict", "Output owner or content changed during Office preview", 409)
        return result

    def make_source(self, channel_id, page):
        self.require_channel(channel_id)
        try:
            record = self.artifact(page)
            content, _, _ = self.artifact_content(record)
            kind = make_family(record["path"])
            if kind in FORMATS:
                metadata = record.get("office_preview", {})
                if metadata.get("content_revision") != hashlib.sha256(content).hexdigest() or metadata.get("make_validation_version") != 1:
                    # Make preparation is synchronous. Never block the HTTP loop
                    # on parsing an unvalidated file; the worker can read its
                    # actual original through its normal approved exec tool.
                    metadata = {"text": "", "warnings": ["Read the actual original file with its Office library before editing; no validated text preview is cached."]}
                return {"artifact_id": record["id"], "title": record.get("title") or PurePosixPath(record["path"]).stem,
                        "path": record["path"], "artifact_type_id": wire_type(kind), "content_revision": hashlib.sha256(content).hexdigest(),
                        "source": metadata.get("text", ""), "source_is_text_preview": True, "warnings": metadata.get("warnings", [])}
            if kind != "page":
                raise WorkspaceError("unsupported_media_type", "Not a supported Make artifact", 415)
            if len(content) > 200000:
                raise WorkspaceError("too_large", "Existing HTML source exceeds the Make context limit", 413)
            source = content.decode("utf-8")
        except WorkspaceError as error:
            raise AiboError(error.code, error.message, error.status) from None
        except UnicodeDecodeError:
            raise AiboError("invalid_argument", "Existing HTML source must be UTF-8") from None
        # This single-account catalog is owned by the authenticated account.
        # Choosing an existing catalog page authorizes its private local source;
        # this never fetches a URL or accepts an arbitrary filesystem path.
        return {"artifact_id": record["id"], "title": record.get("title") or PurePosixPath(record["path"]).stem,
                "path": record["path"], "artifact_type_id": wire_type(self.make_type(record)), "content_revision": hashlib.sha256(content).hexdigest(), "source": source}

    async def create_page(self, request):
        self.organization(request)
        self.require_engine()
        kind = resolve_type(request.match_info["type_id"])
        if not format_available(kind):
            raise WorkspaceError("not_implemented", "This local Make format is unavailable", 501)
        body = await object_body(request)
        title = body.get("title")
        if (not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(char) < 32 for char in title)
                or any(0xD800 <= ord(char) <= 0xDFFF for char in title)):
            raise WorkspaceError("invalid_argument", "A page title is required")
        key = request.query.get("idempotency_key")
        if key is not None and (not key or len(key) > 256):
            raise WorkspaceError("invalid_argument", "Invalid idempotency key")
        if key and key in self.state["creates"]:
            ident = self.state["creates"][key]
            if self.make_type(self.artifact(ident)) != kind:
                raise WorkspaceError("conflict", "Idempotency key belongs to another Make format", 409)
            return {"slug": ident}
        name = kind + "-" + uuid4().hex + (FORMATS[kind]["extension"] if kind in FORMATS else ".html")
        if kind == "page":
            content = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + html.escape(title)
                       + "</title></head><body><h1>" + html.escape(title) + "</h1></body></html>").encode()
        elif kind in WEB_FORMATS:
            content = web_starter(kind, title)
        else:
            content = await asyncio.to_thread(office_starter, kind, title)
        office = await asyncio.to_thread(self.office_metadata, {"path": name}, content) if kind in FORMATS else None
        self.request_current(request)
        self.require_engine()
        if key and key in self.state["creates"]:
            ident = self.state["creates"][key]
            if self.make_type(self.artifact(ident)) != kind:
                raise WorkspaceError("conflict", "Idempotency key belongs to another Make format", 409)
            return {"slug": ident}
        self.write(name, content, create=True)
        metadata = self.metadata(name)
        ident = str(uuid5(NAMESPACE_URL, "duet-workspace:" + name))
        self.state["files"][ident] = {"id": ident, "path": name, "created_at": iso_time(metadata.st_ctime),
                                      "updated_at": iso_time(metadata.st_mtime), "size": metadata.st_size}
        if office is not None:
            self.state["files"][ident]["office_preview"] = office
        if kind in WEB_FORMATS:
            self.state["files"][ident]["make_type"] = kind
        self.save()
        if key:
            self.state["creates"][key] = ident
            self.save()
        return {"slug": ident}

    async def local_file(self, request):
        name = request.query.get("path")
        if request.method == "GET":
            result = await self.file_content(name, recheck=lambda: self.request_current(request))
            self.request_current(request)
            return result
        if request.method == "DELETE":
            self.delete(name)
            return {"deleted": True}
        body = await object_body(request)
        content = body.get("content")
        if not isinstance(content, str):
            raise WorkspaceError("invalid_argument", "UTF-8 content string required")
        if not (self.mime(name).startswith("text/") or self.mime(name) in {"application/json", "application/xml"}):
            raise WorkspaceError("unsupported_media_type", "This endpoint saves UTF-8 text files only", 415)
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError("invalid_argument", "Content is not valid UTF-8 text")
        self.request_current(request)
        self.write(name, encoded, request.headers.get("If-Match"))
        self.index()
        return await self.file_content(name, recheck=lambda: self.request_current(request))

    async def local_download(self, request):
        name = request.query.get("path")
        content, mime, _ = self.read(name)
        return ranged_response(request, content, {"Content-Type": mime, "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Disposition": disposition(name)}, hashlib.sha256(content).hexdigest())

    async def artifact_download(self, request):
        record = self.artifact(request.match_info["artifact_id"])
        content, mime, _ = self.artifact_content(record)
        return ranged_response(request, content, {"Content-Type": mime, "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Disposition": disposition(record["path"])}, hashlib.sha256(content).hexdigest())

    async def local_page(self, request):
        record = self.artifact(request.match_info["artifact_id"])
        if make_family(record["path"]) is None:
            raise WorkspaceError("unsupported_media_type", "Not a supported Make artifact", 415)
        if make_family(record["path"]) in FORMATS:
            content, _, _ = self.artifact_content(record)
            preview = await self.office_render(record, content)
            self.request_current(request)
            return web.Response(body=preview, headers={"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox allow-downloads", "Referrer-Policy": "no-referrer"})
        if record.get("content_revision"):
            content, mime, _ = self.artifact_content(record)
            return web.Response(body=content, headers={"Content-Type": mime, "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "Content-Security-Policy": PAGE_CSP, "Referrer-Policy": "no-referrer"})
        raise web.HTTPFound("/duet/workspace/view/" + quote(record["path"], safe="/"))

    def html_artifact(self, ident):
        record = self.artifact(ident)
        if make_family(record["path"]) is None:
            raise WorkspaceError("unsupported_media_type", "Not a supported Make artifact", 415)
        return record

    async def page_details(self, request):
        record = self.html_artifact(request.match_info["artifact_id"])
        return {"id": record["id"], "title": PurePosixPath(record["path"]).stem,
                "favorite": self.page_favorite(record), "published": False}

    async def page_text(self, request):
        record = self.html_artifact(request.match_info["artifact_id"])
        content, _, _ = self.artifact_content(record)
        if make_family(record["path"]) in FORMATS:
            content = await self.office_render(record, content)
            self.request_current(request)
        if len(content) > MAX_TEXT:
            raise WorkspaceError("too_large", "HTML source exceeds 2 MiB", 413)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("unsupported_media_type", "HTML source is not UTF-8 text", 415)
        return web.Response(body=content, headers={"Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox", "Referrer-Policy": "no-referrer"})

    async def local_view(self, request):
        content, mime, _ = self.read(request.match_info["path"])
        return web.Response(body=content, headers={"Content-Type": mime, "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": PAGE_CSP,
            "Referrer-Policy": "no-referrer"})

    async def skills(self, request):
        self.require_channel(request.match_info["channel_id"])
        plugins = getattr(self.engine, "plugins", None)
        if plugins is not None:
            return plugins.slash_skills(request.match_info["channel_id"])
        return {"skills": [], "account_skills_loaded": False, "skills_unavailable": True}

    def channel_messages(self, channel_id, thread_id=None):
        messages = [message for session in self.aibo.state["sessions"].values()
                    if session["channel_id"] == channel_id
                    for message in session["messages"] if (message.get("_complete") or message.get("_published"))
                    and (thread_id is None or message.get("thread_root_id") == thread_id)]
        return sorted(messages, key=lambda message: message["created_at"])

    def require_thread(self, channel_id, thread_id):
        if (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 256
                or not any(message.get("thread_root_id") == thread_id
                           for session in self.aibo.state["sessions"].values() if session["channel_id"] == channel_id
                           for message in session["messages"])):
            raise WorkspaceError("not_found", "Unknown thread in this local channel", 404)

    async def presence(self, request):
        channel_id = request.match_info["channel_id"]
        self.require_channel(channel_id)
        body = await object_body(request)
        client = body.get("client_id")
        if (not isinstance(client, str) or not client or len(client) > 256
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in client)):
            raise WorkspaceError("invalid_argument", "client_id up to 256 characters is required")
        for key in ("clear", "text_posts_only", "typing"):
            if body.get(key) is not None and type(body[key]) is not bool:
                raise WorkspaceError("invalid_argument", "Presence flags must be booleans")
        clear = body.get("clear", False)
        thread = body.get("thread_id")
        if thread is not None and not clear:
            self.require_thread(channel_id, thread)
        now = time.time()
        records = self.state["presence"].get(channel_id, {})
        records = {key: value for key, value in records.items() if value["expires_at"] > now}
        if clear:
            records.pop(client, None)
        else:
            if client not in records and len(records) >= 256:
                raise WorkspaceError("resource_exhausted", "Too many active local clients", 429)
            records[client] = {"client_id": client, "last_seen_at": utc_now(), "expires_at": now + 60,
                               "thread_id": thread, "text_posts_only": body.get("text_posts_only", False),
                               "typing": body.get("typing", False),
                               "typing_expires_at": now + 15 if body.get("typing") else now}
        self.state["presence"][channel_id] = records
        self.save()
        return {"refresh_after_seconds": 0 if clear else 20, "text_posts_only": False,
                "typing_refresh_after_seconds": 0 if clear else 5}

    async def mark_read(self, request):
        channel_id = request.match_info["channel_id"]
        self.require_channel(channel_id)
        body = await object_body(request) if request.can_read_body else {}
        thread = body.get("thread_id")
        if thread is not None:
            self.require_thread(channel_id, thread)
        if body.get("last_read_message_id") is not None and thread is None:
            raise WorkspaceError("invalid_argument", "A message read marker requires thread_id")
        messages = self.channel_messages(channel_id, thread)
        positions = {message["id"]: index for index, message in enumerate(messages)}
        last = body.get("last_read_message_id")
        if last is not None and (not isinstance(last, str) or last not in positions):
            raise WorkspaceError("not_found", "Read boundary is not a message in this thread", 404)
        last = last if last is not None else (messages[-1]["id"] if messages else None)
        key = thread or "__channel__"
        markers = self.state["read_markers"].get(channel_id, {})
        previous = markers.get(key, {}).get("last_read_message_id")
        if previous in positions and (last is None or positions[previous] > positions[last]):
            last = previous
        markers[key] = {"thread_id": thread, "last_read_message_id": last, "read_at": utc_now()}
        self.state["read_markers"][channel_id] = markers
        self.save()
        channel_messages = self.channel_messages(channel_id)
        if thread is None or (channel_messages and last == channel_messages[-1]["id"]):
            self.home.channel(channel_id)["has_unread"] = False
            self.home.save()
        # y1b0 delegates kotlin.Unit's object serializer; {} is a valid body.
        return {}

    async def mcp(self, request):
        self.organization(request)
        manager = getattr(self.engine, "connector_manager", None)
        frames = manager.bootstrap_frames() if manager is not None else [
            ("server_list", {"servers": [], "servers_may_be_partial": False}), ("completed", {})]
        body = "".join("event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n" for kind, data in frames)
        return web.Response(text=body,
                            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})


WORKSPACE = web.AppKey("duet_workspace", Workspace)


def register_workspace(app, home, aibo, engine=None, data_dir=None, tasks=None):
    workspace = Workspace(home, aibo, engine, data_dir or home.path.parent, tasks=tasks)
    app[WORKSPACE] = workspace
    if tasks is not None:
        tasks.make_source = workspace.make_source
        tasks.make_template = workspace.make_template
    workspace.reconcile_outputs()
    if engine is not None and hasattr(engine, "workspace_output"):
        engine.workspace_output = workspace.written_output
        engine.workspace_write_visibility = workspace.write_visibility
    if engine is not None and hasattr(engine, "extensions"):
        async def publish(arguments, session_id):
            if not isinstance(arguments, dict) or arguments.keys() - {"path", "title"}:
                raise ValueError("publish_artifact requires a workspace path and optional title")
            tool = engine.native_tools.get(session_id, {})
            try:
                return await workspace.publish_output(session_id, tool.get("id"), arguments.get("path"), arguments.get("title"),
                    recheck=lambda: engine.recheck_tool_dispatch(session_id))
            except WorkspaceError as error:
                raise ValueError(error.message) from None
        name = "publish_artifact"
        engine.extensions[name] = ({"name": name, "description": "Attach an existing file in this private workspace to your current reply as a deliverable. Use after a successful exec creates a file. Reads and snapshots the real file; does not execute code or share publicly. write_file already attaches supported files automatically.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "title": {"type": "string"}},
                           "required": ["path"], "additionalProperties": False}}, publish)
        if hasattr(engine, "task_tools"):
            engine.task_tools.add(name)
    if engine is not None:
        # Original bdc.b checks bui.Z (tasks_change_source) before it can read
        # the HTML source endpoint used by its native page viewer.
        home.capabilities = sorted(set(home.capabilities) | {"computers_list_v1", "tasks_change_source"})
    if workspace.connected():
        # z1s.E -> channel.d0 -> dxb.e exposes the existing Sessions start UI.
        home.capabilities = sorted(set(home.capabilities) | {"start_session_v1"})

    @web.middleware
    async def workspace_policy(request, handler):
        try:
            if request.method == "POST" and request.path in {"/v1/code/aibo/sessions:start", "/api/v1/code/aibo/sessions:start"}:
                body = await object_body(request)
                if not workspace.connected():
                    return response({"outcome": "offline", "session_possibly_started": False})
                if body.get("environment_id") != workspace.state["environment_id"]:
                    return response({"outcome": "not_known", "session_possibly_started": False})
                press = body.get("press_id")
                if not isinstance(press, str):
                    raise WorkspaceError("invalid_argument", "press_id required")
                if press not in aibo.state["starts"] and workspace.active_count() >= MAX_SESSIONS:
                    return response({"outcome": "full", "session_possibly_started": False,
                                     "live_sessions": workspace.active_count(), "max_sessions": MAX_SESSIONS})
                result = await aibo.start(request)
                if result.get("session_id"):
                    session = aibo.session(result["session_id"])
                    session["environment_id"] = workspace.state["environment_id"]
                    repository = session.get("repository")
                    result.update(environment_id=workspace.state["environment_id"], computer=workspace.state["name"],
                                  label=repository["name"] if repository else "workspace", cloud=bool(repository))
                    aibo.save()
                return response(result)
            return await handler(request)
        except (WorkspaceError, AiboError) as error:
            return response({"error": {"code": error.code, "message": error.message}}, error.status)
    app.middlewares.append(workspace_policy)

    def route(method, path, function):
        async def handle(request):
            result = await function(request)
            return result if isinstance(result, web.StreamResponse) else (web.Response(status=204) if result is None else response(result))
        app.router.add_route(method, path, handle)

    for prefix in ("", "/api"):
        route("POST", prefix + "/v1/code/aibo/sessions:start-folders", workspace.folders)
        route("POST", prefix + "/v1/code/aibo/computers:list", workspace.computers)
        route("POST", prefix + "/v1/code/aibo/computers:rename", workspace.rename)
        route("GET", prefix + "/v1/code/aibo/environments", workspace.network_environments)
        route("GET", prefix + "/v1/environment_providers/private/organizations/{organization}/environments", workspace.environment_list)
        route("POST", prefix + "/v1/code/aibo/channels/{channel_id}/files/list", workspace.files)
        route("POST", prefix + "/v1/code/channels/{channel_id}/files:open", workspace.open_file)
        route("GET", prefix + "/v1/code/channels/{channel_id}/attached_outputs/{artifact_id}", workspace.attached)
        route("POST", prefix + "/v1/code/aibo/channels/{channel_id}/pages/source", workspace.page_source)
        route("GET", prefix + "/v1/code/channels/{channel_id}/slash-skills", workspace.skills)
        route("POST", prefix + "/v1/code/channels/{channel_id}/client/presence", workspace.presence)
        route("POST", prefix + "/v1/code/channels/{channel_id}/mark_read", workspace.mark_read)
        route("GET", prefix + "/organizations/{organization}/mcp/v2/bootstrap", workspace.mcp)
        route("GET", prefix + "/frame/artifacts", workspace.artifacts)
        route("GET", prefix + "/frame/artifacts/{artifact_id}/origin", workspace.artifact_origins)
        route("GET", prefix + "/frame/types", workspace.types)
        route("POST", prefix + "/frame/types/{type_id}/create", workspace.create_page)
        route("GET", prefix + "/frame/{artifact_id}", workspace.frame_boot)
        route("DELETE", prefix + "/frame/{artifact_id}", workspace.frame_delete)
        route("POST", prefix + "/frame/favorite/{artifact_id}", workspace.favorite)
        route("DELETE", prefix + "/frame/favorite/{artifact_id}", workspace.favorite)
        async def payment_unavailable(request):
            return {"state": "unavailable"}
        route("GET", prefix + "/v1/code/aibo/shop-pay", payment_unavailable)
    route("GET", "/duet/workspace/desktop", workspace.desktop)
    for method in ("GET", "PUT", "DELETE"):
        route(method, "/duet/workspace/files", workspace.local_file)
    route("GET", "/duet/workspace/download", workspace.local_download)
    route("HEAD", "/duet/workspace/download", workspace.local_download)
    route("GET", "/duet/workspace/artifacts/{artifact_id}/download", workspace.artifact_download)
    route("HEAD", "/duet/workspace/artifacts/{artifact_id}/download", workspace.artifact_download)
    route("GET", "/duet/workspace/pages/{artifact_id}", workspace.local_page)
    route("GET", "/duet/workspace/pages/{artifact_id}/details", workspace.page_details)
    route("GET", "/duet/workspace/pages/{artifact_id}/source", workspace.page_text)
    route("GET", "/duet/workspace/view/{path:.*}", workspace.local_view)
    return workspace
