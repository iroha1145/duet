"""Durable, version-bound MCP permissions using the retained native bool-map wire."""
import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager


class PermissionConflict(ValueError):
    pass


@dataclass(frozen=True)
class PermissionDecision:
    kind: str
    fingerprint: tuple


class ConnectorPermissions:
    def __init__(self, root):
        self.path = root / "connector-permissions.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Invalid connector permissions database")
        with self._db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS epochs (id TEXT PRIMARY KEY, value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS permissions (version TEXT PRIMARY KEY, enabled INTEGER NOT NULL, approved INTEGER NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS verified_tools (id TEXT NOT NULL, epoch TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(id,version));"
                             "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL);")
            db.execute("INSERT OR IGNORE INTO state VALUES(1, ?)", (secrets.token_hex(16),))
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def epoch(self, ident):
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO epochs VALUES(?, ?)", (ident, secrets.token_hex(16)))
            return db.execute("SELECT value FROM epochs WHERE id=?", (ident,)).fetchone()[0]

    def invalidate(self, ident):
        with self._db() as db:
            db.execute("DELETE FROM permissions WHERE version IN (SELECT version FROM verified_tools WHERE id=?)", (ident,))
            db.execute("DELETE FROM verified_tools WHERE id=?", (ident,))
            db.execute("INSERT OR REPLACE INTO epochs VALUES(?, ?)", (ident, secrets.token_hex(16)))
            db.execute("UPDATE state SET revision=? WHERE id=1", (secrets.token_hex(16),))

    def version(self, ident, definition):
        raw = json.dumps([self.epoch(ident), definition], sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def record_verified(self, ident, versions):
        """Replace only UI identity cache after real discovery; never enables execution."""
        versions = set(versions)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute("SELECT value FROM epochs WHERE id=?", (ident,)).fetchone()[0]
            old = {row[0] for row in db.execute("SELECT version FROM verified_tools WHERE id=?", (ident,))}
            for version in old - versions:
                db.execute("DELETE FROM permissions WHERE version=?", (version,))
            db.execute("DELETE FROM verified_tools WHERE id=?", (ident,))
            db.executemany("INSERT INTO verified_tools VALUES(?,?,?)", ((ident, epoch, version) for version in versions))
            if old != versions:
                db.execute("UPDATE state SET revision=? WHERE id=1", (secrets.token_hex(16),))

    def verified_versions(self, installed):
        installed = set(installed)
        with self._db() as db:
            rows = db.execute("SELECT t.id,t.version FROM verified_tools t JOIN epochs e ON t.id=e.id AND t.epoch=e.value")
            return {version for ident, version in rows if ident in installed}

    @staticmethod
    def keys(version):
        return "duet_mcp_enabled:" + version, "duet_mcp_approved:" + version

    def values(self, versions):
        with self._db() as db:
            rows = dict((v, (bool(e), bool(a))) for v, e, a in db.execute("SELECT * FROM permissions"))
        result = {}
        for version in versions:
            enabled, approved = rows.get(version, (True, False))
            enabled_key, approved_key = self.keys(version)
            result.update({enabled_key: enabled, approved_key: approved})
        return result

    def decision(self, version):
        with self._db() as db:
            row = db.execute("SELECT enabled,approved FROM permissions WHERE version=?", (version,)).fetchone()
            revision = db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]
        enabled, approved = row if row is not None else (True, False)
        kind = "blocked" if not enabled else "allow" if approved else "ask"
        return PermissionDecision(kind, (version, bool(enabled), bool(approved), revision))

    def etag(self):
        with self._db() as db:
            return '"' + db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0] + '"'

    def update(self, flags, versions, *, if_match=None):
        # Native sends only changed boolean leaves, without a revision. Those
        # preferences use transactional last-commit-wins within the exact version.
        # Clients that retain an ETag may additionally request compare-and-swap.
        if not isinstance(flags, dict) or len(flags) > 2049 or any(type(v) is not bool for v in flags.values()):
            raise ValueError("Invalid MCP permissions")
        versions = set(versions)
        valid = {key for version in versions for key in self.keys(version)}
        if any(key not in valid for key in flags):
            raise PermissionConflict("MCP tool definitions changed; refresh connections")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]
            if if_match is not None and if_match != '"' + revision + '"':
                raise PermissionConflict("MCP permissions changed; refresh account settings")
            for version in versions:
                enabled_key, approved_key = self.keys(version)
                if enabled_key not in flags and approved_key not in flags:
                    continue
                row = db.execute("SELECT enabled,approved FROM permissions WHERE version=?", (version,)).fetchone() or (True, False)
                enabled, approved = flags.get(enabled_key, bool(row[0])), flags.get(approved_key, bool(row[1]))
                db.execute("INSERT OR REPLACE INTO permissions VALUES(?,?,?)", (version, enabled, approved))
            db.execute("UPDATE state SET revision=? WHERE id=1", (secrets.token_hex(16),))
        return self.values(versions)
