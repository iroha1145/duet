"""Single-account, multi-device authentication for the self-hosted service.

Integration: manager = AuthManager(data_dir); manager.register(app). The outer
middleware permits only manager.is_public_request(request), otherwise requires
``await manager.authenticate(request)`` and stores the returned Principal if it
needs it. These routes also authenticate themselves. No model task is touched.

Initialize once, outside HTTP, with initialize(username, password, display_name)
or ``python -m bridge.auth init --data-dir DIR --username USER`` (getpass prompt).
TLS termination and trusted reverse-proxy configuration belong to deployment.
"""
import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import tempfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from .rest import ACCOUNT_ID, ORGANIZATION_ID

ACCESS_TTL = 15 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
ROTATION_GRACE = 120
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 17, 8, 1
PUBLIC_ROUTES = frozenset({("POST", "/duet/auth/login"), ("POST", "/duet/auth/refresh"),
                           ("GET", "/duet/auth/status")})
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+\-]{0,127}\Z")
_ACCESS = re.compile(r"duet_a_[A-Za-z0-9_\-]{43}\Z")
_REFRESH = re.compile(r"duet_r_[A-Za-z0-9_\-]{43}\Z")
_ROTATION = re.compile(r"[A-Za-z0-9_\-]{43}\Z")


@dataclass(frozen=True)
class Principal:
    device_id: str
    username: str
    display_name: str
    account_id: str = ACCOUNT_ID
    organization_id: str = ORGANIZATION_ID
    access_token_hash: bytes = field(default=b"", repr=False)

    def user(self):
        return {"account_id": self.account_id, "organization_id": self.organization_id,
                "username": self.username, "display_name": self.display_name}


AUTH_PRINCIPAL = web.AppKey("duet_auth_principal", Principal)
AUTH_MANAGER = web.AppKey("duet_auth_manager", object)


class AuthError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _password_hash(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=32, maxmem=256 * 1024 * 1024)


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).digest()


def _label(value, limit):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _json_response(value=None, status=200, headers=None):
    policy = {"Cache-Control": "no-store", "Pragma": "no-cache", "Vary": "Authorization"}
    policy.update(headers or {})
    if status == 204:
        return web.Response(status=204, headers=policy)
    return web.json_response(value, status=status, headers=policy)


class AuthManager:
    def __init__(self, data_dir, *, clock=time.time):
        self.clock = clock
        directory = Path(data_dir) / "auth"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError("Authentication directory must not be a symlink")
        os.chmod(directory, 0o700)
        self.path = directory / "auth.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Authentication database must not be a symlink")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS account (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            username TEXT NOT NULL, display_name TEXT NOT NULL,
            password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
            scrypt_n INTEGER NOT NULL, scrypt_r INTEGER NOT NULL, scrypt_p INTEGER NOT NULL,
            created_at INTEGER NOT NULL
          );
          CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
            revoked_at INTEGER,
            access_hash BLOB UNIQUE, access_expires_at INTEGER,
            refresh_hash BLOB UNIQUE, refresh_expires_at INTEGER
          );
        """)
        try:
            self._initialize_rotation(directory)
        except BaseException:
            self.db.close()
            raise
        self._attempts = {}
        self._hash_slots = asyncio.Semaphore(2)
        self._dummy_salt = secrets.token_bytes(32)
        self._closed = False

    def _initialize_rotation(self, directory):
        # Serialize migration/key publication across processes. Only hashes and
        # timestamps are persisted in SQLite, never recoverable token responses.
        with self._transaction():
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)")}
            for name, kind in (("previous_refresh_hash", "BLOB"), ("rotation_id_hash", "BLOB"),
                               ("rotation_expires_at", "INTEGER"), ("rotation_issued_at", "INTEGER")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE devices ADD COLUMN {name} {kind}")
            key_path = directory / "rotation.key"
            if key_path.is_symlink():
                raise ValueError("Rotation key must not be a symlink")
            if not key_path.exists():
                if self.db.execute("SELECT 1 FROM devices WHERE previous_refresh_hash IS NOT NULL LIMIT 1").fetchone():
                    raise ValueError("Rotation key is missing; restore the authentication key backup")
                descriptor, temporary = tempfile.mkstemp(prefix=".rotation-", dir=directory)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(secrets.token_bytes(32))
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, key_path)
                    if os.name == "posix":
                        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            os.chmod(key_path, 0o600)
            self._rotation_key = key_path.read_bytes()
            if len(self._rotation_key) != 32:
                raise ValueError("Invalid rotation key")

    @staticmethod
    def is_public_path(path, method="GET"):
        return (method.upper(), path) in PUBLIC_ROUTES

    @classmethod
    def is_public_request(cls, request):
        return cls.is_public_path(request.path, request.method)

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def initialize(self, username, password, display_name="duet"):
        """Offline provisioning only. Existing accounts cannot be overwritten."""
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            raise ValueError("Username must be 1-128 ASCII letters/digits or ._@+-")
        if not isinstance(password, str) or not 12 <= len(password) or len(password.encode("utf-8")) > 1024:
            raise ValueError("Password must be at least 12 characters and at most 1024 UTF-8 bytes")
        if not _label(display_name, 80):
            raise ValueError("Display name must be 1-80 printable characters")
        if self.db.execute("SELECT 1 FROM account").fetchone():
            raise ValueError("Account is already initialized")
        salt = secrets.token_bytes(32)
        digest = _password_hash(password, salt)
        try:
            with self._transaction():
                self.db.execute("INSERT INTO account VALUES(1,?,?,?,?,?,?,?,?)",
                    (username.casefold(), display_name.strip(), salt, digest,
                     SCRYPT_N, SCRYPT_R, SCRYPT_P, int(self.clock())))
        except sqlite3.IntegrityError:
            raise ValueError("Account is already initialized") from None

    def _account(self):
        return self.db.execute("SELECT * FROM account WHERE singleton=1").fetchone()

    def _user(self, account):
        return Principal("", account["username"], account["display_name"]).user()

    def public_user(self):
        """Synchronous safe bootstrap profile, or None before provisioning."""
        account = self.db.execute("SELECT username,display_name FROM account WHERE singleton=1").fetchone()
        return self._user(account) if account is not None else None

    def _limit(self, request, operation):
        # Do not trust arbitrary X-Forwarded-For. A trusted proxy may set the
        # actual peer in middleware; absent that, clients share its peer bucket.
        now = self.clock()
        peer = request.remote or "unknown"
        window, global_limit, peer_window, peer_limit = (300, 30, 60, 5) if operation == "login" else (60, 120, 60, 30)
        for key, history in list(self._attempts.items()):
            while history and history[0] <= now - 300:
                history.popleft()
            if not history:
                self._attempts.pop(key, None)
        keys = [((operation, "global"), window, global_limit), ((operation, peer), peer_window, peer_limit)]
        for key, duration, count in keys:
            history = self._attempts.get(key, ())
            recent = [value for value in history if value > now - duration]
            if len(recent) >= count:
                retry = max(1, int(recent[0] + duration - now) + 1)
                raise AuthError(429, "rate_limited", "Too many authentication attempts; try again later", retry)
        for key, _, _ in keys:
            self._attempts.setdefault(key, deque()).append(now)

    async def _body(self, request):
        body = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > 16384:
                raise AuthError(400, "invalid_request", "Invalid authentication request")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            raise AuthError(400, "invalid_request", "Invalid authentication request") from None
        if not isinstance(value, dict):
            raise AuthError(400, "invalid_request", "Invalid authentication request")
        return value

    async def _hash_async(self, password, salt, n, r, p):
        await self._hash_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(_password_hash, password, salt, n, r, p))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._hash_slots.release()
            else:
                # HTTP cancellation must not release the memory budget while
                # an uncancellable scrypt worker is still running.
                def finished(result):
                    self._hash_slots.release()
                    if not result.cancelled():
                        result.exception()
                task.add_done_callback(finished)

    def _pair(self, device_id, account, now, rotation=None):
        def material(domain):
            if rotation is None:
                return secrets.token_urlsafe(32)
            # Fixed-size digests plus a JSON-framed device/domain prevent
            # ambiguous concatenation and separate the two token purposes.
            message = json.dumps(["duet-rotation-v1", domain, device_id], separators=(",", ":")).encode() + b"\0" + rotation[0] + rotation[1]
            return base64.urlsafe_b64encode(hmac.digest(self._rotation_key, message, "sha256")).rstrip(b"=").decode("ascii")
        access = "duet_a_" + material("access")
        refresh = "duet_r_" + material("refresh")
        response = {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                    "expires_in": ACCESS_TTL, "refresh_expires_in": REFRESH_TTL,
                    "device_id": device_id, "user": self._user(account)}
        columns = (_token_hash(access), now + ACCESS_TTL, _token_hash(refresh), now + REFRESH_TTL)
        return response, columns

    async def login(self, request):
        self._limit(request, "login")
        body = await self._body(request)
        username, password, name = body.get("username"), body.get("password"), body.get("device_name", "Device")
        if (not isinstance(username, str) or len(username) > 128 or not isinstance(password, str)
                or not 0 < len(password.encode("utf-8")) <= 1024 or not _label(name, 80)):
            raise AuthError(400, "invalid_request", "Invalid authentication request")
        account = self._account()
        salt = account["password_salt"] if account else self._dummy_salt
        n, r, p = (account["scrypt_n"], account["scrypt_r"], account["scrypt_p"]) if account else (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        candidate = await self._hash_async(password, salt, n, r, p)
        password_ok = hmac.compare_digest(candidate, account["password_hash"] if account else bytes(32))
        username_ok = hmac.compare_digest(username.casefold().encode("utf-8"), account["username"].encode("utf-8") if account else b"")
        if account is None or not (password_ok and username_ok):
            raise AuthError(401, "invalid_credentials", "Invalid username or password")
        now, device_id = int(self.clock()), str(uuid4())
        response, columns = self._pair(device_id, account, now)
        with self._transaction():
            self.db.execute("INSERT INTO devices(id,name,created_at,last_seen_at,revoked_at,access_hash,access_expires_at,refresh_hash,refresh_expires_at) VALUES(?,?,?,?,NULL,?,?,?,?)", (device_id, name.strip(), now, now, *columns))
        return _json_response(response)

    async def refresh(self, request):
        self._limit(request, "refresh")
        body = await self._body(request)
        token = body.get("refresh_token")
        if not isinstance(token, str) or not _REFRESH.fullmatch(token):
            raise AuthError(401, "invalid_token", "Invalid or expired refresh token")
        rotation_id = body.get("rotation_id")
        if "rotation_id" in body and (not isinstance(rotation_id, str) or not _ROTATION.fullmatch(rotation_id)):
            raise AuthError(400, "invalid_request", "Invalid rotation ID")
        rotation_hash = _token_hash(rotation_id) if rotation_id is not None else None
        digest, now = _token_hash(token), int(self.clock())
        with self._transaction():
            # BEGIN IMMEDIATE serializes validation + rotation across connections,
            # not just coroutines. A concurrent second use cannot match old hash.
            device = self.db.execute("SELECT * FROM devices WHERE refresh_hash=?", (digest,)).fetchone()
            account = self._account()
            if device is None and rotation_hash is not None:
                device = self.db.execute("SELECT * FROM devices WHERE previous_refresh_hash=? AND rotation_id_hash=?", (digest, rotation_hash)).fetchone()
                if (device is not None and account is not None and device["revoked_at"] is None
                        and device["rotation_expires_at"] > now and device["access_expires_at"] > now
                        and device["refresh_expires_at"] > now):
                    response, columns = self._pair(device["id"], account, device["rotation_issued_at"], (digest, rotation_hash))
                    if (hmac.compare_digest(columns[0], device["access_hash"])
                            and hmac.compare_digest(columns[2], device["refresh_hash"])):
                        response["expires_in"] = device["access_expires_at"] - now
                        response["refresh_expires_in"] = device["refresh_expires_at"] - now
                        return _json_response(response)
                raise AuthError(401, "invalid_token", "Invalid or expired refresh token")
            if device is None or account is None or device["revoked_at"] is not None or device["refresh_expires_at"] <= now:
                raise AuthError(401, "invalid_token", "Invalid or expired refresh token")
            response, columns = self._pair(device["id"], account, now, (digest, rotation_hash) if rotation_hash is not None else None)
            recovery = (digest, rotation_hash, now + ROTATION_GRACE, now) if rotation_hash is not None else (None, None, None, None)
            self.db.execute("UPDATE devices SET access_hash=?,access_expires_at=?,refresh_hash=?,refresh_expires_at=?,last_seen_at=?,previous_refresh_hash=?,rotation_id_hash=?,rotation_expires_at=?,rotation_issued_at=? WHERE id=?",
                            (*columns, now, *recovery, device["id"]))
        return _json_response(response)

    async def authenticate(self, request):
        """Return a Principal or None; never read credentials from URL/cookies."""
        header = request.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not _ACCESS.fullmatch(token):
            return None
        now = int(self.clock())
        row = self.db.execute("""SELECT d.id,a.username,a.display_name FROM devices d CROSS JOIN account a
            WHERE a.singleton=1 AND d.access_hash=? AND d.access_expires_at>? AND d.revoked_at IS NULL""",
            (_token_hash(token), now)).fetchone()
        if row is None:
            return None
        self.db.execute("UPDATE devices SET last_seen_at=? WHERE id=? AND last_seen_at<?", (now, row["id"], now - 60))
        principal = Principal(row["id"], row["username"], row["display_name"], access_token_hash=_token_hash(token))
        request[AUTH_PRINCIPAL] = principal
        return principal

    def session_valid(self, device_id, access_token_hash=None):
        """Recheck long-lived transports on the event-loop/owning DB thread.

        Pass both Principal.device_id and Principal.access_token_hash to enforce
        access expiry, refresh rotation, and device revocation. With only device
        ID this checks device/refresh lifetime (not the original access token).
        A false result closes transport only; callers must not cancel model work.
        """
        now = int(self.clock())
        if access_token_hash is not None:
            if not isinstance(access_token_hash, bytes) or len(access_token_hash) != 32:
                return False
            return self.db.execute("""SELECT 1 FROM devices WHERE id=? AND access_hash=?
                AND revoked_at IS NULL AND access_expires_at>?""", (device_id, access_token_hash, now)).fetchone() is not None
        return self.db.execute("SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL AND refresh_expires_at>?",
                               (device_id, now)).fetchone() is not None

    async def _require(self, request):
        principal = await self.authenticate(request)
        if principal is None:
            raise AuthError(401, "unauthorized", "Authentication required")
        return principal

    async def status(self, request):
        return _json_response({"initialized": self._account() is not None, "registration_enabled": False})

    async def me(self, request):
        principal = await self._require(request)
        return _json_response({"user": principal.user(), "device_id": principal.device_id})

    async def devices(self, request):
        principal = await self._require(request)
        rows = self.db.execute("SELECT id,name,created_at,last_seen_at,revoked_at,refresh_expires_at FROM devices ORDER BY created_at DESC,id").fetchall()
        now = int(self.clock())
        return _json_response({"devices": [{"id": r["id"], "device_name": r["name"], "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"], "revoked_at": r["revoked_at"], "current": r["id"] == principal.device_id,
            "active": r["revoked_at"] is None and r["refresh_expires_at"] is not None and r["refresh_expires_at"] > now} for r in rows]})

    def _revoke(self, device_id):
        with self._transaction():
            row = self.db.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise AuthError(404, "not_found", "Device not found")
            self.db.execute("""UPDATE devices SET revoked_at=COALESCE(revoked_at,?),access_hash=NULL,access_expires_at=NULL,
                refresh_hash=NULL,refresh_expires_at=NULL,previous_refresh_hash=NULL,rotation_id_hash=NULL,
                rotation_expires_at=NULL,rotation_issued_at=NULL WHERE id=?""", (int(self.clock()), device_id))

    async def logout(self, request):
        principal = await self._require(request)
        self._revoke(principal.device_id)
        return _json_response(status=204)

    async def revoke(self, request):
        await self._require(request)
        device_id = request.match_info["device_id"]
        if len(device_id) > 128:
            raise AuthError(404, "not_found", "Device not found")
        self._revoke(device_id)
        return _json_response(status=204)

    def _handler(self, method):
        async def wrapped(request):
            try:
                return await method(request)
            except AuthError as error:
                headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
                return _json_response({"error": {"code": error.code, "message": error.message}}, error.status, headers)
            except sqlite3.Error:
                return _json_response({"error": {"code": "unavailable", "message": "Authentication temporarily unavailable"}}, 503)
            except UnicodeError:
                return _json_response({"error": {"code": "invalid_request", "message": "Invalid authentication request"}}, 400)
        return wrapped

    def register(self, app):
        app[AUTH_MANAGER] = self
        routes = [("POST", "/duet/auth/login", self.login), ("POST", "/duet/auth/refresh", self.refresh),
                  ("GET", "/duet/auth/status", self.status), ("GET", "/duet/auth/me", self.me),
                  ("GET", "/duet/auth/devices", self.devices), ("POST", "/duet/auth/logout", self.logout),
                  ("DELETE", "/duet/auth/devices/{device_id}", self.revoke)]
        for verb, path, handler in routes:
            app.router.add_route(verb, path, self._handler(handler))
        app.on_cleanup.append(self.cleanup)
        return self

    def close(self):
        if not self._closed:
            self.db.close()
            self._closed = True

    async def cleanup(self, app):
        self.close()


def main():
    parser = argparse.ArgumentParser(description="Provision the single duet server account offline")
    parser.add_argument("command", choices=["init"])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="duet")
    args = parser.parse_args()
    password = getpass.getpass("Password (at least 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords did not match")
    manager = AuthManager(args.data_dir)
    try:
        manager.initialize(args.username, password, args.display_name)
    except ValueError as error:
        parser.error(str(error))
    finally:
        manager.close()
    print("Local account initialized.")


if __name__ == "__main__":
    main()
