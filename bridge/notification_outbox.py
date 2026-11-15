"""Durable, device-scoped push grants and delivery of committed source events."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from uuid import UUID, uuid4

from .aibo import AiboError
from .fcm import FCMConfigStore, FCMError, FCMTransport, PACKAGE
from .rest import ACCOUNT_ID, ORGANIZATION_ID


MAX_DEVICES = 64
MAX_EVENTS = 1024
MAX_DELIVERIES = 4096
MAX_RECEIPTS = MAX_EVENTS * MAX_DEVICES
MAX_SOURCE_CHANNELS = 4096
MAX_ATTEMPTS = 8
CHALLENGE_TTL = 300
TERMINAL = ("device_received", "cancelled", "expired", "failed")
PENDING = ("queued", "retry", "sending", "provider_accepted")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _date(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).digest()


def _epoch(value, default):
    try:
        return min(default, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default


def installation_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise FCMError("push_installation_id_required")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise FCMError("invalid_push_installation_id") from error
    if str(parsed) != value.lower() or parsed.version != 4:
        raise FCMError("invalid_push_installation_id")
    return str(parsed)


class PushStore:
    def __init__(self, root, *, clock=time.time):
        self.clock = clock
        self.path = root / "outbox.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Push database must not be a link")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Push database must be private")
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS subscriptions(
            device TEXT PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, generation TEXT NOT NULL,
            config_generation TEXT NOT NULL, installation TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE, permission INTEGER NOT NULL, status TEXT NOT NULL,
            form_factor TEXT NOT NULL, timezone TEXT NOT NULL, refreshed REAL NOT NULL,
            received REAL, challenge_hash BLOB, challenge_expires REAL);
          CREATE TABLE IF NOT EXISTS events(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, collapse_key TEXT NOT NULL,
            body TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(
            event TEXT NOT NULL, device TEXT NOT NULL, generation TEXT NOT NULL,
            config_generation TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL, provider_accepted REAL, received REAL, error TEXT,
            PRIMARY KEY(event,device));
          CREATE TABLE IF NOT EXISTS cursors(channel TEXT PRIMARY KEY, event INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts(
            event TEXT NOT NULL, device TEXT NOT NULL, generation TEXT NOT NULL,
            config_generation TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL,
            PRIMARY KEY(event,device));
          CREATE TABLE IF NOT EXISTS push_metadata(id INTEGER PRIMARY KEY CHECK(id=1), source_gaps INTEGER NOT NULL);
          INSERT OR IGNORE INTO push_metadata VALUES(1,0);
          CREATE INDEX IF NOT EXISTS pending_push_due ON deliveries(state,due);
        """)
        # A send interrupted by a server crash has an uncertain outcome. Retrying
        # the SAME event ID is safe because the client must deduplicate it.
        self.db.execute("UPDATE deliveries SET state='retry',due=? WHERE state='sending'", (self.clock() + 1,))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def subscription(self, device):
        return self.db.execute("SELECT * FROM subscriptions WHERE device=?", (device,)).fetchone()

    def _cancel_device(self, device):
        self.db.execute("UPDATE deliveries SET state='cancelled',error='subscription_changed' WHERE device=? AND state IN ('queued','retry','sending','provider_accepted')", (device,))

    def revoke(self, device):
        with self.transaction():
            self._cancel_device(device)
            self.db.execute("DELETE FROM subscriptions WHERE device=?", (device,))

    def revoke_sent_subscription(self, sent):
        # Google can reply after token/install/config replacement. Its rejection
        # applies only to the exact grant used for that request, not whichever
        # subscription happens to occupy the same login device now.
        with self.transaction():
            deleted = self.db.execute(
                "DELETE FROM subscriptions WHERE device=? AND generation=? AND config_generation=? AND token=?",
                (sent["device"], sent["generation"], sent["config_generation"], sent["token"]))
            if deleted.rowcount:
                self._cancel_device(sent["device"])

    def revoke_all(self):
        with self.transaction():
            self.db.execute("UPDATE deliveries SET state='cancelled',error='subscription_changed' WHERE state IN ('queued','retry','sending','provider_accepted')")
            self.db.execute("DELETE FROM subscriptions")

    def native(self, row):
        return {"channel_type": "FCM", "client_app_name": PACKAGE, "client_platform": "android",
                "device_id": row["device"], "last_successful_delivery_time": _date(row["received"]) if row["received"] is not None else None,
                "last_token_refresh_time": _date(row["refreshed"]), "registration_token": row["token"],
                "status": row["status"], "uuid": row["uuid"]}

    def register(self, principal, body, installation, config, *, prove=False):
        now = self.clock()
        with self.transaction():
            previous = self.subscription(principal.device_id)
            other = self.db.execute("SELECT device FROM subscriptions WHERE token=? AND device<>?", (body["registration_token"], principal.device_id)).fetchone()
            if other is not None:
                raise FCMError("push_token_already_bound")
            if previous is None and self.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] >= MAX_DEVICES:
                raise FCMError("push_device_limit")
            permission = body["os_push_permission_granted"] is True
            same = previous is not None and all((previous["config_generation"] == config.generation,
                previous["installation"] == installation, previous["token"] == body["registration_token"],
                bool(previous["permission"]) == permission))
            if same and ((previous["status"] == "ACTIVE" and not prove) or not permission
                         or (previous["status"] == "INACTIVE" and previous["challenge_expires"] is not None
                             and previous["challenge_expires"] > now)):
                self.db.execute("UPDATE subscriptions SET form_factor=?,timezone=?,refreshed=? WHERE device=?",
                    (body["form_factor"], body["timezone"], now, principal.device_id))
                return self.subscription(principal.device_id)
            self._cancel_device(principal.device_id)
            uid = previous["uuid"] if previous is not None else str(uuid4())
            generation, nonce = secrets.token_hex(16), secrets.token_urlsafe(32)
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?,?,?,?,'INACTIVE',?,?,?,NULL,?,?)",
                (principal.device_id, uid, generation, config.generation, installation, body["registration_token"], permission,
                 body["form_factor"], body["timezone"], now, _digest(nonce) if permission else None,
                 now + CHALLENGE_TTL if permission else None))
            if permission:
                event = {"id": "challenge-" + secrets.token_hex(16), "kind": "challenge", "collapse_key": "registration",
                         "created": now, "expires": now + CHALLENGE_TTL, "nonce": nonce}
                self._insert_event(event, [self.subscription(principal.device_id)])
            return self.subscription(principal.device_id)

    def _insert_event(self, event, subscriptions):
        if self.db.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
            return False
        self._make_room(len(subscriptions))
        if (self.db.execute("SELECT count(*) FROM events").fetchone()[0] >= MAX_EVENTS
                or self.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] + len(subscriptions) > MAX_DELIVERIES):
            raise FCMError("push_outbox_full", retryable=True)
        body = _json(event)
        if len(body.encode()) > 8192:
            raise FCMError("invalid_push_event")
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event["id"], event["kind"], event["collapse_key"], body, event["created"], event["expires"]))
        for row in subscriptions:
            if event["kind"] != "challenge":
                self.db.execute("UPDATE deliveries SET state='cancelled',error='superseded' WHERE device=? AND event IN (SELECT id FROM events WHERE collapse_key=?) AND state IN ('queued','retry')",
                                (row["device"], event["collapse_key"]))
            self.db.execute("INSERT INTO deliveries(event,device,generation,config_generation,state,due) VALUES(?,?,?,?,'queued',?)",
                            (event["id"], row["device"], row["generation"], row["config_generation"], self.clock()))
        return True

    def _make_room(self, deliveries):
        # Called inside the insertion transaction. Compact only terminal rows;
        # pending deliveries and unconsumed source cursors are never discarded.
        self.db.execute("DELETE FROM receipts WHERE expires<=?", (self.clock(),))
        count = self.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        excess = max(0, count + deliveries - MAX_DELIVERIES)
        event_excess = max(0, self.db.execute("SELECT count(*) FROM events").fetchone()[0] + 1 - MAX_EVENTS)
        if excess or event_excess:
            rows = self.db.execute("""SELECT d.*,e.kind,e.expires FROM deliveries d
                JOIN events e ON e.id=d.event
                WHERE d.state IN ('device_received','cancelled','expired','failed')
                ORDER BY CASE WHEN d.state='device_received' THEN 1 ELSE 0 END,e.created""").fetchall()
            receipt_count = self.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
            for row in rows:
                if excess <= 0 and event_excess <= 0:
                    break
                if row["state"] == "device_received" and row["expires"] > self.clock():
                    if receipt_count >= MAX_RECEIPTS:
                        continue
                    inserted = self.db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)",
                        (row["event"], row["device"], row["generation"], row["config_generation"], row["kind"], row["expires"]))
                    receipt_count += inserted.rowcount
                self.db.execute("DELETE FROM deliveries WHERE event=? AND device=?", (row["event"], row["device"]))
                excess -= 1
                if event_excess:
                    deleted = self.db.execute("DELETE FROM events WHERE id=? AND NOT EXISTS (SELECT 1 FROM deliveries WHERE event=?)",
                        (row["event"], row["event"]))
                    event_excess -= deleted.rowcount
        if event_excess:
            # Keep ACKed rows until delivery pressure compacts them. Only events
            # with no remaining delivery rows are safe to remove here.
            self.db.execute("""DELETE FROM events WHERE id IN (
                SELECT e.id FROM events e WHERE NOT EXISTS
                (SELECT 1 FROM deliveries d WHERE d.event=e.id)
                ORDER BY e.created LIMIT ?)""", (event_excess,))

    def enqueue(self, event, subscriptions, *, cursor=None):
        with self.transaction():
            inserted = self._insert_event(event, subscriptions) if event is not None else False
            if cursor is not None:
                channel, record_id = cursor
                if (self.db.execute("SELECT 1 FROM cursors WHERE channel=?", (channel,)).fetchone() is None
                        and self.db.execute("SELECT count(*) FROM cursors").fetchone()[0] >= MAX_SOURCE_CHANNELS):
                    raise FCMError("push_source_channel_limit")
                self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET event=MAX(event,excluded.event)", (channel, record_id))
        return inserted

    def cursors(self):
        return dict(self.db.execute("SELECT channel,event FROM cursors"))

    def flush_cursors(self, cursors):
        if not cursors:
            return
        with self.transaction():
            for channel, record_id in cursors.items():
                if (self.db.execute("SELECT 1 FROM cursors WHERE channel=?", (channel,)).fetchone() is None
                        and self.db.execute("SELECT count(*) FROM cursors").fetchone()[0] >= MAX_SOURCE_CHANNELS):
                    raise FCMError("push_source_channel_limit")
                self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET event=MAX(event,excluded.event)", (channel, record_id))

    def reconcile(self, replacements):
        # Explicit fail-safe retention policy: cancel old queued state for the
        # affected channel, skip only the unavailable prefix, and replay retained
        # immutable records subject to current-state checks. Never read a dirty
        # Aibo ledger to invent missing notification history.
        with self.transaction():
            for channel, after in replacements.items():
                if (self.db.execute("SELECT 1 FROM cursors WHERE channel=?", (channel,)).fetchone() is None
                        and self.db.execute("SELECT count(*) FROM cursors").fetchone()[0] >= MAX_SOURCE_CHANNELS):
                    raise FCMError("push_source_channel_limit")
                for row in self.db.execute("SELECT id,body FROM events").fetchall():
                    if json.loads(row["body"]).get("channel_id") == channel:
                        self.db.execute("UPDATE deliveries SET state='cancelled',error='source_history_gap' WHERE event=? AND state IN ('queued','retry','sending','provider_accepted')", (row["id"],))
                self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET event=excluded.event", (channel, after))
            self.db.execute("UPDATE push_metadata SET source_gaps=source_gaps+? WHERE id=1", (len(replacements),))

    def prune(self):
        now = self.clock()
        with self.transaction():
            self.db.execute("UPDATE deliveries SET state='expired',error='expired' WHERE event IN (SELECT id FROM events WHERE expires<=?) AND state IN ('queued','retry','sending','provider_accepted')", (now,))
            self.db.execute("DELETE FROM deliveries WHERE event IN (SELECT id FROM events WHERE expires<?)", (now - 86400,))
            self.db.execute("DELETE FROM events WHERE expires<?", (now - 86400,))
            self.db.execute("DELETE FROM receipts WHERE expires<=?", (now,))
            # Preserve dedup until expiry, but bound terminal audit rows under load.
            excess = max(0, self.db.execute("SELECT count(*) FROM events").fetchone()[0] - MAX_EVENTS + 64)
            if excess:
                self.db.execute("DELETE FROM events WHERE id IN (SELECT e.id FROM events e WHERE NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.event=e.id AND (d.state IN ('queued','retry','sending','provider_accepted') OR (d.state='device_received' AND e.expires>?))) ORDER BY e.created LIMIT ?)", (now, excess))
                self.db.execute("DELETE FROM deliveries WHERE event NOT IN (SELECT id FROM events)")

    def claim(self):
        with self.transaction():
            row = self.db.execute("SELECT d.*,e.body,e.expires FROM deliveries d JOIN events e ON e.id=d.event WHERE d.state IN ('queued','retry') AND d.due<=? AND e.expires>? ORDER BY d.due,e.created,d.device LIMIT 1", (self.clock(), self.clock())).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1 WHERE event=? AND device=?", (row["event"], row["device"]))
            return dict(row, attempts=row["attempts"] + 1, state="sending")

    def delivery(self, event, device):
        return self.db.execute("SELECT d.*,e.body,e.expires FROM deliveries d JOIN events e ON e.id=d.event WHERE event=? AND device=?", (event, device)).fetchone()

    def finish(self, delivery, state, *, error=None, due=None):
        # A genuine ACK can race the HTTP provider response. Never overwrite it.
        self.db.execute("UPDATE deliveries SET state=?,error=?,due=COALESCE(?,due),provider_accepted=CASE WHEN ?='provider_accepted' THEN ? ELSE provider_accepted END WHERE event=? AND device=? AND state='sending'",
            (state, error, due, state, self.clock(), delivery["event"], delivery["device"]))

    def acknowledge(self, principal, body, config):
        now = self.clock()
        with self.transaction():
            row = self.subscription(principal.device_id)
            if (row is None or row["uuid"] != body["channel_uuid"] or row["generation"] != body["generation"]
                    or row["config_generation"] != config.generation or not row["permission"]):
                raise FCMError("push_subscription_changed")
            delivery = self.delivery(body["event_id"], principal.device_id)
            if delivery is None:
                receipt = self.db.execute("SELECT * FROM receipts WHERE event=? AND device=?",
                    (body["event_id"], principal.device_id)).fetchone()
                if (receipt is None or receipt["generation"] != row["generation"]
                        or receipt["config_generation"] != config.generation or receipt["expires"] <= now):
                    raise FCMError("push_event_not_current")
                if receipt["kind"] == "challenge":
                    nonce = body.get("nonce")
                    if (not isinstance(nonce, str) or len(nonce) > 128 or row["challenge_hash"] is None
                            or row["challenge_expires"] <= now or not hmac.compare_digest(_digest(nonce), row["challenge_hash"])):
                        raise FCMError("invalid_push_challenge")
                elif "nonce" in body or row["status"] != "ACTIVE":
                    raise FCMError("invalid_push_acknowledgement")
                return {"outcome": "device_received", "status": "ACTIVE", "channel_uuid": row["uuid"]}
            if (delivery is None or delivery["generation"] != row["generation"]
                    or delivery["config_generation"] != config.generation or delivery["attempts"] < 1
                    or delivery["expires"] <= now or delivery["state"] in ("cancelled", "expired", "failed")):
                raise FCMError("push_event_not_current")
            event = json.loads(delivery["body"])
            if event["kind"] == "challenge":
                nonce = body.get("nonce")
                if (not isinstance(nonce, str) or len(nonce) > 128 or row["challenge_hash"] is None
                        or row["challenge_expires"] <= now or not hmac.compare_digest(_digest(nonce), row["challenge_hash"])):
                    raise FCMError("invalid_push_challenge")
                self.db.execute("UPDATE subscriptions SET status='ACTIVE',received=? WHERE device=?", (now, principal.device_id))
            elif "nonce" in body or row["status"] != "ACTIVE":
                raise FCMError("invalid_push_acknowledgement")
            else:
                self.db.execute("UPDATE subscriptions SET received=? WHERE device=?", (now, principal.device_id))
            self.db.execute("UPDATE deliveries SET state='device_received',received=?,error=NULL WHERE event=? AND device=?", (now, delivery["event"], principal.device_id))
        return {"outcome": "device_received", "status": "ACTIVE", "channel_uuid": row["uuid"]}

    def close(self):
        self.db.close()


class NotificationOutbox:
    def __init__(self, root, auth, preferences, *, transport=None, clock=time.time):
        self.auth, self.preferences, self.clock = auth, preferences, clock
        self.config = FCMConfigStore(root)
        self.store = PushStore(self.config.root, clock=clock)
        self.transport = transport or FCMTransport(clock=clock)
        self.source = self.tasks = self.routines = None
        self._unsubscribe = None
        self._task = None
        self._closed = False
        self._wake = asyncio.Event()
        self._blocked_channels = set()
        self._cursors = self.store.cursors()
        self._dirty_cursors = {}
        self.last_error = None

    def opted_in(self):
        return self.preferences.read(ACCOUNT_ID, ORGANIZATION_ID)[1].get("sleepsort", {}).get("enable_push") is True

    def config_changed(self):
        self.store.revoke_all()
        self._wake.set()

    def _sub_current(self, row, config, *, allow_pending=False):
        return bool(row is not None and config.enabled and config.configured
                    and row["config_generation"] == config.generation and row["permission"]
                    and (allow_pending or row["status"] == "ACTIVE") and self.opted_in()
                    and self.auth is not None and self.auth.session_valid(row["device"]))

    def register(self, principal, body, installation, *, prove=False):
        config = self.config.read()
        if not config.enabled or not config.configured:
            raise FCMError("push_transport_not_configured")
        if not self.opted_in():
            raise FCMError("push_not_enabled")
        self.sweep(config)
        row = self.store.register(principal, body, installation, config, prove=prove)
        self._wake.set()
        return self.store.native(row)

    def channels(self, principal):
        self.sweep(self.config.read())
        row = self.store.subscription(principal.device_id)
        return [] if row is None else [self.store.native(row)]

    def acknowledge(self, principal, body):
        config = self.config.read()
        if not self._sub_current(self.store.subscription(principal.device_id), config, allow_pending=True):
            raise FCMError("push_subscription_changed")
        return self.store.acknowledge(principal, body, config)

    def sweep(self, config):
        for row in self.store.db.execute("SELECT * FROM subscriptions").fetchall():
            if not config.enabled or row["config_generation"] != config.generation or not self.opted_in() or not self.auth.session_valid(row["device"]):
                self.store.revoke(row["device"])

    def bind_sources(self, source, *, tasks=None, routines=None):
        if self.source is not None:
            raise RuntimeError("Notification sources are already bound")
        self.source, self.tasks, self.routines = source, tasks, routines
        self._unsubscribe = source.add_committed_observer(self.on_committed)
        self.backfill()

    def backfill(self):
        if self.source is None:
            return
        removed = set(self._cursors) - set(self.source.home.state["channels"])
        for cid in removed:
            self.store.db.execute("DELETE FROM cursors WHERE channel=?", (cid,))
            self._cursors.pop(cid, None)
            self._dirty_cursors.pop(cid, None)
        cursors = dict(self._cursors)
        try:
            records = self.source.iter_committed_channel_events(cursors, limit=256)
        except AiboError as error:
            if error.code != "committed_events_gap":
                raise FCMError("notification_source_unavailable") from error
            bounds = self.source.committed_channel_bounds()
            replacements = {cid: bound["first"] - 1 if cursors.get(cid, 0) < bound["first"] - 1 else bound["last"]
                            for cid, bound in bounds.items()
                            if cursors.get(cid, 0) < bound["first"] - 1 or cursors.get(cid, 0) > bound["last"]}
            self.store.reconcile(replacements)
            self._cursors.update(replacements)
            for cid in replacements:
                self._dirty_cursors.pop(cid, None)
            self.last_error = "notification_history_gap"
            records = self.source.iter_committed_channel_events(dict(self._cursors), limit=256)
        for key, record in records:
            self._ingest(key, record)
        self._blocked_channels.clear()
        self.store.flush_cursors(self._dirty_cursors)
        self._dirty_cursors.clear()

    def on_committed(self, key, record):
        if self._closed or not isinstance(key, tuple) or len(key) != 2 or key[0] != "channel":
            return
        wake = False
        try:
            if key[1] not in self._blocked_channels:
                wake = self._ingest(key, record)
        except (FCMError, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            # A notification failure cannot abort model work. A persisted cursor
            # is advanced only with its outbox write; backfill retries the gap.
            self._blocked_channels.add(key[1])
            self.last_error = "notification_source_unavailable"
            wake = True
        if wake:
            self._wake.set()

    def _ingest(self, key, record):
        if key[0] != "channel" or type(record.get("id")) is not int or record["id"] <= 0:
            return
        cid, record_id = key[1], record["id"]
        cursor = self._cursors.get(cid, 0)
        if record_id <= cursor:
            return
        if record_id != cursor + 1:
            self._blocked_channels.add(cid)
            return
        event = self._source_event(cid, record)
        subscriptions = []
        if event is not None and event["expires"] > self.clock():
            config = self.config.read()
            subscriptions = [row for row in self.store.db.execute("SELECT * FROM subscriptions").fetchall()
                             if self._sub_current(row, config)]
        # Ignored stream frames need no synchronous SQLite fsync per token. A
        # crash may replay them; relevant events + their cursor are atomic.
        inserted = False
        if event is not None and subscriptions:
            inserted = self.store.enqueue(event, subscriptions, cursor=(cid, record_id))
            self._dirty_cursors.pop(cid, None)
        else:
            self._dirty_cursors[cid] = record_id
        self._cursors[cid] = record_id
        return inserted

    def _source_event(self, cid, record):
        if self.source is None or not isinstance(record.get("data"), dict):
            return None
        channel = self.source.home.channel(cid)
        if not channel or channel.get("created_by_account_id") != ACCOUNT_ID or channel.get("archived_at") or channel.get("disabled_at"):
            return None
        data, kind = record["data"], record["kind"]
        now = self.clock()
        event = {"channel_id": cid, "source_id": record["id"], "created": _epoch(data.get("at", data.get("edited_at", data.get("created_at"))), now)}
        sid = data.get("session_id")
        if kind == "session_requires_action" and data.get("pending_actions"):
            session = self.source.state["sessions"].get(sid)
            if session is None or session["channel_id"] != cid:
                return None
            approvals = []
            facts = record.get("notification_facts", {})
            versions = facts.get("approval_versions", {})
            if (not isinstance(versions, dict) or facts.get("turn_started_at") != session.get("turn_started_at")):
                return None
            for pending in data["pending_actions"]:
                rid = pending.get("request_id")
                current = self.source.approval_pending(session, rid)
                version = versions.get(rid)
                if current is not None and version == self.source.approval_version(session, rid, current):
                    approvals.append([rid, version])
            if not approvals:
                return None
            event.update(kind="approval", title="duet needs your approval", body="Open the app to review this request.",
                         subtype="thread_waits_on_you", approvals=approvals, ttl=300)
        elif kind == "session_activity" and data.get("working") is False and data.get("ended") in ("done", "failed"):
            session = self.source.state["sessions"].get(sid)
            if (session is None or session["channel_id"] != cid or self.source.is_private_run(session)
                    or self.tasks is not None and self.tasks.record_for_session(sid) is not None):
                return None
            event.update(kind="completion", title="Task finished" if data["ended"] == "done" else "Task failed",
                         body="Open the app to view the result.", subtype="coordinator_reply", ended=data["ended"],
                         turn=data.get("turn_started_at"), ttl=86400)
        elif kind in ("channel_message", "channel_message_updated") and data.get("author_routine_id"):
            sid = data.get("author_session_id")
            event.update(kind="routine", title="Your routine has an update", body="Open the app to view the update.",
                         subtype="routine_completed", routine_id=data["author_routine_id"], message_id=data["id"], ttl=3600)
        elif self.tasks is not None and (kind == "thread_session_bound" or (
                kind in ("channel_message", "channel_message_updated") and data.get("server_notice") is True
                and data.get("server_card_kind") == "thread_started")):
            # New publications have facts only on the binding. Read old card
            # facts without upgrading old bindings or deriving missing facts.
            legacy_card = kind != "thread_session_bound"
            if legacy_card:
                try:
                    bound = json.loads(data["body"])
                except (ValueError, TypeError):
                    return None
                if not isinstance(bound, dict):
                    return None
                sid = bound.get("session_id")
            else:
                bound = data.get("session")
                if not isinstance(bound, dict) or bound.get("purpose") != "task":
                    return None
                sid = bound.get("id")
            session = self.source.state["sessions"].get(sid)
            found = self.tasks.record_for_session(sid)
            if (session is None or found is None or found[0] != cid or self.source.is_private_run(session)
                    or (found[2].get("notice_id") != data.get("id") if legacy_card
                        else data.get("thread_root_id") != session["root_id"])):
                return None
            fact = record.get("notification_facts", {}).get("task_summary")
            if (not isinstance(fact, dict) or fact.get("session_id") != sid
                    or fact.get("task_id") != found[1] or fact.get("thread_root_id") != session["root_id"]
                    or fact.get("turn_started_at") != session.get("turn_started_at")):
                return None
            summary = fact.get("status")
            if (summary not in {"completed", "review_ready", "failed", "blocked"}
                    or self.tasks.session_summary(session) != summary):
                return None
            if legacy_card:
                allowed = {"completed": ("resolved", "idle"), "review_ready": ("resolved",),
                           "failed": ("failed",), "blocked": ("needs_input",)}
                if data.get("thread_status") not in allowed[summary]:
                    return None
                event["message_id"] = data["id"]
            elif not fact.get("published_at"):
                return None
            if summary == "blocked" and session.get("pending_approvals"):
                return None  # The exact approval event owns this notification.
            title = {"completed": "Task marked complete", "review_ready": "Task ready for review",
                     "failed": "Task failed", "blocked": "Task needs your input"}[summary]
            event.update(kind="task", title=title, body="Open the app to view the task.", subtype="thread_waits_on_you" if summary == "blocked" else "coordinator_reply",
                         task_id=found[1], summary=summary, turn=fact.get("turn_started_at"),
                         thread_root_id=fact["thread_root_id"], created=_epoch(fact.get("published_at"), event["created"]),
                         ttl=300 if summary == "blocked" else 86400)
        else:
            return None
        session = self.source.state["sessions"].get(sid)
        if session is None or session["channel_id"] != cid:
            return None
        root_id = event.get("thread_root_id") or data.get("thread_root_id") or record.get("thread")
        if not isinstance(root_id, str) or not root_id:
            return None
        event.update(session_id=sid, thread_root_id=root_id, category="routines" if event["kind"] == "routine" else "bogosort",
                     collapse_key="session-" + hashlib.sha256(sid.encode()).hexdigest()[:24])
        event["expires"] = event["created"] + event.pop("ttl")
        # IDs remain stable across retries/restart, while a partial restore that
        # reuses a source cursor cannot alias a different run/card publication.
        identity = [cid, record["id"], event["kind"], sid, event["thread_root_id"],
                    data.get("at", data.get("edited_at", data.get("created_at"))),
                    event.get("turn"), event.get("summary"), event.get("message_id"), event.get("approvals")]
        event["id"] = hashlib.sha256(_json(identity).encode()).hexdigest()
        return event

    def event_current(self, event):
        if event["kind"] in ("challenge", "test"):
            return True
        if self.source is None:
            return False
        channel = self.source.home.channel(event["channel_id"])
        session = self.source.state["sessions"].get(event["session_id"])
        if (not channel or channel.get("created_by_account_id") != ACCOUNT_ID or channel.get("archived_at")
                or channel.get("disabled_at") or session is None or session["channel_id"] != event["channel_id"]):
            return False
        if event["kind"] == "approval":
            return any((pending := self.source.approval_pending(session, rid)) is not None
                       and self.source.approval_version(session, rid, pending) == version for rid, version in event["approvals"])
        if event["kind"] == "completion":
            expected = "idle" if event["ended"] == "done" else ("error", "interrupted")
            return ((session["phase"] == expected if isinstance(expected, str) else session["phase"] in expected)
                    and session.get("turn_started_at") == event.get("turn"))
        if event["kind"] == "task":
            found = self.tasks.record_for_session(session["id"]) if self.tasks is not None else None
            return bool(found and found[0] == event["channel_id"] and found[1] == event["task_id"]
                        and not self.source.is_private_run(session) and session.get("root_id") == event["thread_root_id"]
                        and session.get("turn_started_at") == event.get("turn")
                        and self.tasks.session_summary(session) == event["summary"])
        if event["kind"] == "routine":
            if self.routines is None:
                return False
            trigger = self.routines.schedules.state["triggers"].get(event["routine_id"])
            return bool(trigger and self.routines.can_run(trigger)
                        and any(m.get("id") == event["message_id"] and m.get("author_routine_id") == event["routine_id"]
                                for m in session["messages"]))
        return False

    def _data(self, event, subscription):
        data = {"duet_type": "registration_challenge" if event["kind"] == "challenge" else "notification",
                "duet_event_id": event["id"], "duet_channel_uuid": subscription["uuid"],
                "duet_device_id": subscription["device"], "duet_installation_id": subscription["installation"],
                "duet_generation": subscription["generation"], "duet_config_generation": subscription["config_generation"],
                "account_uuid": ACCOUNT_ID, "organization_uuid": ORGANIZATION_ID}
        if event["kind"] == "challenge":
            data["duet_nonce"] = event["nonce"]
            return data
        data.update(title=event["title"], body=event["body"], notification_feature_category=event["category"], ccr_notif_type=event["subtype"])
        for key in ("channel_id", "session_id", "thread_root_id", "task_id", "message_id"):
            if event.get(key):
                data[key] = event[key]
        if event.get("session_id"):
            data["payload"] = _json({"service": "duet", "method": "OpenCodeSession",
                "request": {"account_uuid": ACCOUNT_ID, "organization_uuid": ORGANIZATION_ID, "session_id": event["session_id"]}})
        return data

    def _guard(self, delivery, event, config):
        current = self.config.read()
        row = self.store.subscription(delivery["device"])
        state = self.store.delivery(delivery["event"], delivery["device"])
        if (self._closed or current.generation != config.generation or row is None
                or row["generation"] != delivery["generation"] or state is None or state["state"] != "sending"
                or event["expires"] <= self.clock()
                or not self._sub_current(row, current, allow_pending=event["kind"] == "challenge")
                or not self.event_current(event)):
            raise FCMError("push_delivery_no_longer_current")
        if event["kind"] == "challenge" and row["status"] != "INACTIVE":
            raise FCMError("push_delivery_no_longer_current")
        return row

    async def tick(self):
        self.store.prune()
        try:
            self.backfill()
        except FCMError as error:
            # Ingestion pressure must not starve deliveries that could free it.
            # The failing source cursor remains unadvanced for the next tick.
            if error.code != "push_outbox_full":
                raise
            self.last_error = error.code
        config = self.config.read()
        self.sweep(config)
        for _ in range(8):
            delivery = self.store.claim()
            if delivery is None:
                break
            event = json.loads(delivery["body"])
            try:
                config = self.config.read()
                row = self._guard(delivery, event, config)
                await self.transport.send(config, row["token"], self._data(event, row), ttl=event["expires"] - self.clock(),
                    collapse_key=event["collapse_key"], priority="NORMAL" if event["kind"] == "challenge" else "HIGH",
                    guard=lambda: self._guard(delivery, event, config))
                self.store.finish(delivery, "provider_accepted")
                self.last_error = None
            except FCMError as error:
                self.last_error = error.code
                if error.invalid_token:
                    self.store.revoke_sent_subscription(row)
                    self.store.finish(delivery, "failed", error=error.code)
                elif error.retryable and delivery["attempts"] < MAX_ATTEMPTS:
                    delay = max(error.retry_after, min(300, 5 * 2 ** delivery["attempts"]))
                    self.store.finish(delivery, "retry", error=error.code, due=self.clock() + delay)
                else:
                    self.store.finish(delivery, "cancelled" if error.code == "push_delivery_no_longer_current" else "failed", error=error.code)

    def test_push(self, principal, category):
        config = self.config.read()
        row = self.store.subscription(principal.device_id)
        if not self._sub_current(row, config):
            raise FCMError("push_verified_channel_required")
        now = self.clock()
        event = {"id": "test-" + secrets.token_hex(16), "kind": "test", "collapse_key": "test",
                 "created": now, "expires": now + 60, "title": "duet test notification",
                 "body": "Your server queued this test notification.", "category": category, "subtype": "coordinator_reply"}
        self.store.enqueue(event, [row])
        self._wake.set()
        return {"status": "queued", "category": category}

    def status(self, principal):
        config = self.config.read()
        self.sweep(config)
        row = self.store.subscription(principal.device_id)
        active = self._sub_current(row, config)
        counts = dict(self.store.db.execute("SELECT state,count(*) FROM deliveries WHERE device=? GROUP BY state", (principal.device_id,)))
        gaps = self.store.db.execute("SELECT source_gaps FROM push_metadata WHERE id=1").fetchone()[0]
        return {"source_reconciliations": gaps, "source_gap_policy": "cancel_stale_and_replay_retained_current",
                "preferences_supported": True, "push_delivery_ready": active, "channel_registration_ready": config.enabled and config.configured,
                "test_push_ready": active, "badge_delivery_ready": False, "registered_channels": int(active),
                "transport": "FCM", "config_generation": config.generation, "device_status": row["status"] if row else None,
                "delivery_states": counts, "last_error": self.last_error,
                "reason": None if active else "push_transport_not_configured" if not config.enabled else "push_verified_channel_required"}

    def start(self):
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="duet-push-outbox")

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            try:
                await self.tick()
            except (FCMError, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                self.last_error = "notification_worker_unavailable"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.transport.close()

    def close(self):
        self.store.close()
