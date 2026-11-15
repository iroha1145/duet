"""Explicit local Git registrations and isolated committed task snapshots.

No clone, network transport, credential import, source worktree mutation or
source Git configuration is used. The native repo wire is a display adapter,
not evidence of a GitHub account or installation.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time

from aiohttp import web

from .aibo import AiboError, canonical_session, ident
from .auth import AUTH_MANAGER, AUTH_PRINCIPAL
from .rest import ACCOUNT_ID, ORGANIZATION_ID
from .rest_home import response

REPOSITORIES = web.AppKey("duet_repositories", object)
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
SESSION = re.compile(r"session_[A-Za-z0-9-]{1,120}\Z")
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 20000


def regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise ValueError("Git source contains a nonregular file")
    return path


def contained(root, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Expected a relative POSIX path")
    parts = PurePosixPath(relative).parts
    if (not parts or relative != PurePosixPath(relative).as_posix() or relative.startswith("/")
            or any(p in (".", "..") or ":" in p for p in parts)):
        raise ValueError("Invalid registered path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Registered paths cannot traverse links")
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("Registered path escapes allowed root")
    return path


def source_bytes(root, relative, limit):
    """Linux descriptor traversal rejects links even if a source path is raced.

    Windows fixtures also validate every component; Linux is the production
    host and uses openat/O_NOFOLLOW for the entire registered source chain.
    """
    path = contained(root, relative)
    if os.name != "posix":
        regular(path)
        with path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Repository source is not a regular file")
            data = source.read(limit + 1)
    else:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(file_descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Repository source is not a regular file")
                data = source.read(limit + 1)
        finally:
            os.close(descriptor)
    if len(data) > limit:
        raise ValueError("Repository source exceeds limit")
    return data


class Repositories:
    def __init__(self, home, aibo, workspace, engine, data_dir):
        self.home, self.aibo, self.workspace, self.engine = home, aibo, workspace, engine
        self.config_path = Path(data_dir) / "repositories.json"
        self.directory = Path(data_dir).resolve() / "repository-tasks"
        self.lock = asyncio.Lock()
        self.git = shutil.which("git")
        self.root, self.entries = None, {}
        if self.config_path.exists():
            config = json.loads(regular(self.config_path).read_text(encoding="utf-8"))
            if set(config) != {"version", "allowed_root", "repositories"} or config["version"] != 1:
                raise ValueError("Invalid repositories configuration")
            root = Path(config["allowed_root"])
            if not root.is_absolute() or root.is_symlink() or not root.is_dir():
                raise ValueError("allowed_root must be an existing absolute directory")
            self.root = root.resolve(strict=True)
            if not isinstance(config["repositories"], list) or len(config["repositories"]) > 100:
                raise ValueError("At most 100 explicitly registered repositories")
            for entry in config["repositories"]:
                if (not isinstance(entry, dict) or set(entry) != {"name", "path", "revision"}
                        or not isinstance(entry["name"], str) or not NAME.fullmatch(entry["name"])
                        or entry["name"] in self.entries):
                    raise ValueError("Invalid repository registration")
                revision = entry["revision"]
                if not isinstance(revision, str) or not (re.fullmatch(r"[a-f0-9]{40}", revision)
                        or re.fullmatch(r"refs/heads/[A-Za-z0-9_][A-Za-z0-9_./-]{0,180}", revision)):
                    raise ValueError("Register a full commit or explicit local branch ref")
                if ".." in revision or "//" in revision or revision.endswith(("/", ".", ".lock")):
                    raise ValueError("Invalid registered revision")
                source = contained(self.root, entry["path"])
                gitdir = contained(self.root, entry["path"] + "/.git")
                if not gitdir.is_dir():
                    raise ValueError("Only standalone local Git worktrees are supported")
                self.entries[entry["name"]] = dict(entry)
        if self.directory.is_symlink():
            raise ValueError("Repository task directory cannot be a link")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Preparation directories are uncommitted transaction debris only.
        for path in self.directory.glob(".preparing-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        for path in self.directory.iterdir():
            if SESSION.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                session = self.aibo.state["sessions"].get(path.name, {})
                if session.get("repository", {}).get("workspace") != path.name:
                    shutil.rmtree(path)
        recovered = False
        for result in self.aibo.state["starts"].values():
            session = self.aibo.state["sessions"].get(result.get("session_id"), {})
            if session.get("repository_start_state") == "prepared":
                session["repository_start_state"] = "refused"
                session["phase"] = "error"
                result.update(outcome="repository_refused", session_possibly_started=False)
                recovered = True
        if recovered:
            self.aibo.save()

    def ready(self):
        return bool(self.git and self.root and self.workspace.connected()
                    and self.engine is not None and getattr(self.aibo.backend, "service", None) is self.engine)

    def authorize(self, request):
        principal = request.get(AUTH_PRINCIPAL)
        if principal is None:
            raise AiboError("unauthorized", "Account authentication required", 401)
        if principal.account_id != ACCOUNT_ID or principal.organization_id != ORGANIZATION_ID:
            raise AiboError("forbidden", "Repository belongs to another account", 403)
        manager = request.app.get(AUTH_MANAGER) if hasattr(request, "app") else None
        if manager is not None:
            owner = manager.public_user()
            if owner is None or principal.username != owner["username"]:
                raise AiboError("forbidden", "Repository account changed", 403)
        if request.match_info.get("organization", ORGANIZATION_ID) != ORGANIZATION_ID:
            raise AiboError("not_found", "Unknown organization", 404)
        if not request.get("duet_authorized", lambda: True)():
            raise AiboError("unauthorized", "Login expired", 401)
        return principal

    async def availability(self, request):
        self.authorize(request)
        return {"registered_local_repositories": self.ready(), "source": "registered_local_git",
                "environment_id": self.workspace.state["environment_id"] if self.ready() else None}

    async def listing(self, request):
        self.authorize(request)
        rows = []
        if self.ready() and not request.query.get("cursor"):
            for name, entry in sorted(self.entries.items()):
                owner, repo = name.split("/")
                rows.append({"repo": {"name": repo, "owner": {"login": owner},
                    "default_branch": entry["revision"].removeprefix("refs/heads/"), "visibility": "private",
                    "sourceUrl": "duet-local://registered/" + name}})
        return {"repos": rows, "sources": [], "source_warnings": [], "is_complete": True, "next_cursor": None}

    def _git(self, gitdir, *args, deadline=None):
        # Explicit safe Git directory contains no copied config, hooks, info,
        # alternates, replace refs, credentials or attributes. No source cwd.
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_LFS_SKIP_SMUDGE="1")
        timeout = 30 if deadline is None else min(30, deadline - time.monotonic())
        if timeout <= 0:
            raise ValueError("Repository preparation timed out")
        result = subprocess.run([self.git, "--git-dir=" + str(gitdir), "-c", "core.hooksPath=" + os.devnull,
            "-c", "protocol.allow=never", *args], cwd=gitdir.parent, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode:
            raise ValueError("Registered Git revision is unavailable or invalid")
        if len(result.stdout) > MAX_BYTES:
            raise ValueError("Repository output exceeds limit")
        return result.stdout

    def prepare(self, name, sid):
        deadline = time.monotonic() + 120
        entry = self.entries[name]
        source = contained(self.root, entry["path"])
        original = contained(self.root, entry["path"] + "/.git")
        destination = self.directory / sid
        if destination.exists() or destination.is_symlink():
            raise ValueError("Repository task workspace already exists")
        staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=self.directory))
        try:
            gitdir = staging / "git"
            (gitdir / "objects").mkdir(parents=True)
            (gitdir / "refs" / "heads").mkdir(parents=True)
            (gitdir / "HEAD").write_text("ref: refs/heads/unused\n", encoding="ascii")
            budget, count = 0, 0
            # No links or alternates, even if they currently point inside root.
            objects = contained(self.root, entry["path"] + "/.git/objects")
            for directory, dirs, files in os.walk(objects, followlinks=False):
                base = Path(directory)
                for child in dirs:
                    p = base / child
                    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                        raise ValueError("Git objects cannot traverse links")
                for filename in files:
                    if time.monotonic() >= deadline:
                        raise ValueError("Repository preparation timed out")
                    path = regular(base / filename)
                    rel = path.relative_to(objects)
                    if rel.parts[0] == "info":
                        if filename in ("alternates", "http-alternates"):
                            raise ValueError("Git alternate object stores are unsupported")
                        continue
                    if not (re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{38}", rel.as_posix()) or
                            re.fullmatch(r"pack/pack-[0-9a-f]{40}\.(pack|idx)", rel.as_posix())):
                        continue
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError("Registered Git object store exceeds limit")
                    data = source_bytes(self.root, entry["path"] + "/.git/objects/" + rel.as_posix(), MAX_BYTES - budget)
                    budget += len(data)
                    target = gitdir / "objects" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o600)
            revision = entry["revision"]
            if revision.startswith("refs/heads/"):
                ref = original / revision
                if ref.exists():
                    commit = source_bytes(self.root, entry["path"] + "/.git/" + revision, 256).decode("ascii").strip()
                else:
                    packed = source_bytes(self.root, entry["path"] + "/.git/packed-refs", 1024 * 1024).decode("ascii")
                    commit = next((line.split(" ")[0] for line in packed.splitlines()
                                   if line.endswith(" " + revision)), "")
            else:
                commit = revision
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                raise ValueError("Invalid registered commit")
            if self._git(gitdir, "cat-file", "-t", commit, deadline=deadline).strip() != b"commit":
                raise ValueError("Registered revision is not a commit")
            tree = self._git(gitdir, "ls-tree", "-rz", "--full-tree", commit, deadline=deadline)
            root = staging / "workspace"
            root.mkdir(mode=0o700)
            size, count = 0, 0
            for record in tree.split(b"\0"):
                if not record:
                    continue
                meta, raw = record.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split(" ")
                relative = raw.decode("utf-8", "strict")
                parts = PurePosixPath(relative).parts
                if (mode not in ("100644", "100755") or kind != "blob" or not parts or
                        relative != PurePosixPath(relative).as_posix() or relative.startswith("/") or "\\" in relative or any(p in (".", "..") or
                        p.lower() == ".git" or ":" in p or p.endswith((".", " ")) for p in parts)):
                    raise ValueError("Unsupported symlink, submodule or unsafe repository path")
                count += 1
                blob_size = int(self._git(gitdir, "cat-file", "-s", oid, deadline=deadline))
                size += blob_size
                if size > MAX_BYTES or count > MAX_FILES:
                    raise ValueError("Repository snapshot exceeds limit")
                content = self._git(gitdir, "cat-file", "blob", oid, deadline=deadline)
                target = root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(content)
                target.chmod(0o700 if mode == "100755" else 0o600)
            # Keep an independent, detached Git checkout: normal approved git
            # commands must never discover a service/source repository above it.
            private_git = root / ".git"
            gitdir.rename(private_git)
            (private_git / "HEAD").write_text(commit + "\n", encoding="ascii")
            (private_git / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n"
                "autocrlf = false\nfilemode = " + ("true" if os.name == "posix" else "false") + "\n", encoding="ascii")
            self._git(private_git, "read-tree", commit, deadline=deadline)
            staging.rename(destination)
            return {"name": name, "revision": revision, "commit": commit,
                    "source": "registered_local_git", "snapshot": "committed_checkout", "workspace": sid,
                    "branch": revision.removeprefix("refs/heads/") if revision.startswith("refs/heads/") else None}
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def session_root(self, sid):
        session = self.aibo.state["sessions"].get(sid, {})
        repo = session.get("repository")
        if repo is None:
            return None
        if (not SESSION.fullmatch(sid or "") or repo.get("workspace") != sid or
                session.get("repository_account_id") != ACCOUNT_ID):
            raise ValueError("Repository workspace ownership is invalid")
        channel = self.home.channel(session.get("channel_id"))
        if not channel or channel.get("created_by_account_id") != ACCOUNT_ID or channel.get("archived_at") or channel.get("disabled_at"):
            raise ValueError("Repository channel is unavailable")
        return contained(self.directory, sid + "/workspace")

    async def start(self, request, body):
        from .workspace import MAX_SESSIONS
        self.authorize(request)
        if not self.ready():
            raise AiboError("unavailable", "Registered local repository execution is unavailable", 503)
        name, press = body.get("repository"), body.get("press_id")
        if not isinstance(name, str) or name not in self.entries:
            raise AiboError("invalid_argument", "Select an explicitly registered local repository")
        if body.get("environment_id") != self.workspace.state["environment_id"]:
            raise AiboError("invalid_argument", "Repository environment does not match")
        prompt = self.aibo._text(body.get("text"))
        fingerprint = hashlib.sha256(json.dumps([ACCOUNT_ID, name, body.get("environment_id"), prompt,
            body.get("session_id")], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        async with self.lock:
            # A request may have waited behind preparation while its device was
            # logged out. Replays and capacity responses are authenticated too.
            self.authorize(request)
            if press in self.aibo.state["starts"]:
                result = self.aibo.state["starts"][press]
                session = self.aibo.state["sessions"].get(result.get("session_id"), {})
                if session.get("repository_start_fingerprint") != fingerprint:
                    raise AiboError("conflict", "press_id was already used for another request", 409)
                self.session_root(session["id"])
                return result
            if self.workspace.active_count() >= MAX_SESSIONS:
                return {"outcome": "full", "session_possibly_started": False,
                    "live_sessions": self.workspace.active_count(), "max_sessions": MAX_SESSIONS}
            project = self.home.state.get("project")
            if not project:
                raise AiboError("not_found", "Create a local project first", 404)
            cid = project["channel_id"]
            channel = self.home.channel(cid)
            if not channel or channel.get("created_by_account_id") != ACCOUNT_ID or channel.get("archived_at") or channel.get("disabled_at"):
                raise AiboError("forbidden", "Repository channel is unavailable", 403)
            sid = body.get("session_id") or ident("session")
            if not isinstance(sid, str) or not SESSION.fullmatch(canonical_session(sid)):
                raise AiboError("invalid_argument", "Invalid repository session ID")
            sid = canonical_session(sid)
            if sid in self.aibo.state["sessions"]:
                raise AiboError("conflict", "Session already exists", 409)
            job = asyncio.create_task(asyncio.to_thread(self.prepare, name, sid))
            created, started, reserved = None, False, False
            try:
                metadata = await asyncio.shield(job)
                self.authorize(request)
                if press in self.aibo.state["starts"]:
                    raise AiboError("conflict", "press_id was accepted while preparing repository", 409)
                if self.workspace.active_count() >= MAX_SESSIONS:
                    raise AiboError("resource_exhausted", "Worker capacity changed while preparing repository", 409)
                current = self.home.channel(cid)
                if (current is not channel or current.get("created_by_account_id") != ACCOUNT_ID
                        or current.get("archived_at") or current.get("disabled_at")):
                    raise AiboError("conflict", "Channel changed while preparing repository", 409)
                session = self.aibo._new_session(cid, prompt, sid)
                created = session
                session.update(repository=metadata, repository_account_id=ACCOUNT_ID,
                    repository_start_fingerprint=fingerprint, repository_start_state="prepared", task_id=sid, task_channel_id=cid)
                result = {"outcome": "starting", "session_possibly_started": False, "session_id": sid,
                    "computer": self.workspace.state["name"], "label": name, "cloud": True,
                    "environment_id": self.workspace.state["environment_id"]}
                self.aibo.state["starts"][press] = result
                reserved = True
                self.aibo.save()
                session["repository_start_state"] = "accepted"
                result.update(outcome="started", session_possibly_started=True)
                # _begin checkpoints nonce + messages before it schedules work.
                self.aibo._begin(session, prompt, "start:" + press)
                started = True
                return result
            except BaseException as error:
                await asyncio.gather(job, return_exceptions=True)
                if not started and reserved:
                    created["repository_start_state"] = "refused"
                    result.update(outcome="repository_refused", session_possibly_started=False)
                    try:
                        if created["phase"] == "running":
                            self.aibo._finish(created, "error")
                        else:
                            created["phase"] = "error"
                            self.aibo.save()
                    except OSError:
                        # The durable prepared reservation prevents replay after
                        # restart; in-memory refusal prevents replay in this run.
                        pass
                elif not started:
                    if created is not None and self.aibo.state["sessions"].get(sid) is created:
                        self.aibo.state["sessions"].pop(sid)
                        self.aibo.save()
                    path = self.directory / sid
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                if isinstance(error, (ValueError, OSError, subprocess.SubprocessError)):
                    raise AiboError("invalid_repository", "Registered repository snapshot could not be prepared", 409) from error
                raise


def register_repositories(app, home, aibo, workspace, engine, data_dir):
    service = Repositories(home, aibo, workspace, engine, data_dir)
    app[REPOSITORIES] = service
    aibo.repositories = service
    if engine is not None:
        engine.repository_workspace = service.session_root
    for prefix in ("", "/api"):
        for suffix, handler in (("/v1/code/aibo/repositories:availability", service.availability),
                ("/organizations/{organization}/code/repos/all", service.listing)):
            async def handle(request, function=handler):
                try:
                    return response(await function(request))
                except AiboError as error:
                    return response({"error": {"code": error.code, "message": error.message}}, error.status)
            app.router.add_get(prefix + suffix, handle)
    return service
