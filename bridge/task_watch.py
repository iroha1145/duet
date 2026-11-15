"""Original GET code/sessions/watch SSE, backed by the owning Aibo state.

Register after register_aibo and before app.freeze/catch-all:
    watch = register_task_watch(app, aibo, engine=app.get("service"))

The appended middleware remains INSIDE the application's existing authentication
middleware. It handles the exact list/watch paths (including /api aliases), so an
earlier sessions/{sid} dynamic route cannot swallow "watch". No auth is invented.
request['duet_authorized'], if supplied by that outer middleware, is rechecked on
long-lived streams. A watch must belong to one Aibo/tenant, like its parent API.

Wire evidence, relative to work/direct-inspect-network/sources/defpackage:
* kz70.d:48-68: resume_token, include_trigger_sessions, repeated session_ids and
  exclude_tags. kz70.b:76+: list limit/cursor/statuses/only_trigger_sessions.
* phb0 (also original smali_classes2/phb0.smali:25-65): added/changed/removed/sync.
* original smali/jrd.smali:327-453: added/changed data is DIRECT SessionSummaryV2
  (ps70), removed is {id, final?} (qw80); sync needs no business payload.
* tfz:14-17 + smali/wcl.smali:582-610,1040+: the initial ListSessions response
  must contain resume_token; the client seed list has no watch-specific filters.

Our signed opaque *seed* token therefore bootstraps a current filtered snapshot;
it is not a vendor cursor encoding. Once snapshot sync is delivered, SSE IDs are
scope-bound signed cursors into a bounded, real change ledger. Old-process or
evicted cursors return 410 (re-list), rather than silently skipping changes.
No snapshot added frame has an ID until sync, so a partial snapshot reconnect
cannot acknowledge unseen rows. Neither disconnect nor auth loss cancels tasks.

Observation uses a transparent per-instance save tap plus bounded polling of
real Aibo state. engine.changed wakes polling sooner; it is never cleared or
replaced here. The tap is removed on shutdown. No aibo.py edits are required.
"""
import asyncio
import base64
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import time

from aiohttp import web

from .aibo import canonical_session


LIST_PATHS = {"/v1/code/sessions", "/api/v1/code/sessions"}
WATCH_PATHS = {path + "/watch" for path in LIST_PATHS}
LIFECYCLES = {"unknown", "SESSION_STATUS_UNSPECIFIED", "active", "archived", "paused", "failed"}


def reject(status, code):
    kind = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 410: web.HTTPGone,
            429: web.HTTPTooManyRequests, 501: web.HTTPNotImplemented, 503: web.HTTPServiceUnavailable}[status]
    raise kind(text=json.dumps({"error": {"code": code}}), content_type="application/json",
               headers={"Cache-Control": "no-store"})


def boolean(query, name, default=False):
    values = query.getall(name, [])
    if not values:
        return default
    if len(values) != 1 or values[0] not in ("true", "false"):
        reject(400, "invalid_filter")
    return values[0] == "true"


def repeated(query, name, maximum):
    values = query.getall(name, [])
    if len(values) > maximum or any(not value or len(value) > 256 for value in values):
        reject(400, "invalid_filter")
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class Scope:
    include_triggers: bool
    ids: tuple
    excluded: tuple

    def matches(self, row):
        return row is not None and (self.include_triggers or not row.get("trigger_id")) \
            and (not self.ids or row["id"] in self.ids) \
            and not set(self.excluded).intersection(row.get("tags") or [])

    def digest(self):
        data = json.dumps([self.include_triggers, self.ids, self.excluded], separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()[:32]


class SourceUnavailable(Exception):
    pass


class TaskWatch:
    def __init__(self, aibo, engine=None, *, heartbeat=10, poll_interval=.25,
                 max_sessions=512, max_events=4096, max_clients=32, queue_size=256, write_timeout=5):
        if not 0 < heartbeat <= 15 or not 0 < poll_interval <= 1 or min(max_sessions, max_events, max_clients, queue_size) < 1 or write_timeout <= 0:
            raise ValueError("Invalid task watch bounds")
        self.aibo, self.engine = aibo, engine
        self.heartbeat, self.poll_interval = heartbeat, poll_interval
        self.max_sessions, self.max_clients = max_sessions, max_clients
        self.queue_size, self.write_timeout = queue_size, write_timeout
        self.epoch, self.secret = secrets.token_hex(12), secrets.token_bytes(32)
        self.sequence, self.rows = 0, {}
        self.ledger = deque(maxlen=max_events)
        self.clients = set()
        self.closed = False
        self.unavailable = False
        self.monitor = None
        self.original_save = aibo.save

        def tapped_save(*args, **kwargs):
            result = self.original_save(*args, **kwargs)
            # An observer must never make a successful business save/task fail.
            self.observe()
            return result

        self.save_tap = tapped_save
        self.observe()

    def project(self, session):
        row = dict(self.aibo.session_wire(session))
        # Summary schema only; never project messages, approval input, config or credentials.
        known = {"id", "environment_id", "self_hosted_runner_pool_id", "environment_kind", "title", "status", "tags",
                 "connection_status", "created_at", "updated_at", "last_event_at", "unread", "trigger_id",
                 "chat_project_id", "agent_id", "started_by_account_id", "session_grouping_id"}
        row = {key: deepcopy(value) for key, value in row.items() if key in known}
        if not isinstance(row.get("id"), str) or not isinstance(row.get("created_at"), str):
            raise SourceUnavailable()
        if session.get("trigger_id"):
            row["trigger_id"] = session["trigger_id"]
        live = getattr(self.engine, "live_sessions", {}).get(session["id"], {}) if self.engine else {}
        phase = session.get("phase", "idle")
        pending = bool(session.get("pending_approvals") or live.get("pending_approval") or live.get("manual_takeover"))
        running = phase == "running"
        if pending and running:
            row.update(worker_status="requires_action", status_bucket="blocked")
            # Original eia -> f10 uses the singular pending_action's request and
            # tool-use IDs as its card-refresh key. Status alone cannot wake a
            # second request. Project identity only; tool input stays on the
            # authenticated detail/card routes (kg50/dl70 fields are optional).
            first = next(iter(session.get("pending_approvals", {}).items()), None)
            if first is not None:
                request_id, action = first
                row["external_metadata"] = {"pending_action": {
                    "request_id": request_id, "tool_use_id": action["tool_call_id"],
                    "tool_name": action["tool"]}}
        elif running:
            row.update(worker_status="running", status_bucket="working")
        elif phase in ("error", "interrupted"):
            row.update(worker_status="idle", status_bucket="failed")
        elif phase == "idle" and session.get("messages"):
            row.update(worker_status="idle", status_bucket="review_ready")
        else:
            row.update(worker_status="idle", status_bucket="unknown")
        task_summary = getattr(self.aibo, "task_summary", None)
        if task_summary is not None:
            bucket = task_summary(session)
            if bucket is not None:
                row["status_bucket"] = bucket
        # Lifecycle active is not worker running (ps70/WorkerStatusV2 are separate).
        if row.get("status") not in LIFECYCLES:
            raise SourceUnavailable()
        if len(json.dumps(row, ensure_ascii=False).encode()) > 32768:
            raise SourceUnavailable()
        return row

    def observe(self):
        try:
            sessions = self.aibo.state["sessions"]
            channels = self.aibo.home.state["channels"]
            if len(sessions) > self.max_sessions or len(channels) > self.max_sessions:
                raise SourceUnavailable()
            rows = {sid: self.project(session) for sid, session in sessions.items()
                    if session.get("channel_id") in channels}
        except Exception:
            # Fail this observer closed, without propagating failure into aibo.save.
            # No exception strings/state are logged or sent over the wire.
            self.unavailable = True
            self.disconnect_clients()
            return
        self.unavailable = False
        for sid in sorted(set(self.rows) | set(rows)):
            before, after = self.rows.get(sid), rows.get(sid)
            if before == after:
                continue
            self.sequence += 1
            record = (self.sequence, before, after)
            self.ledger.append(record)
            for queue in tuple(self.clients):
                if queue.full():
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)  # Close; reconnect from the last committed cursor.
                    self.clients.discard(queue)
                else:
                    queue.put_nowait(record)
        self.rows = rows

    def disconnect_clients(self):
        for queue in tuple(self.clients):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self.clients.clear()

    def available(self):
        self.observe()
        if self.unavailable or self.closed:
            reject(503, "task_watch_source_unavailable")

    def token(self, kind, sequence, scope=""):
        payload = json.dumps([self.epoch, kind, sequence, scope], separators=(",", ":")).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def parse_token(self, token, scope):
        if not isinstance(token, str) or not 1 <= len(token) <= 1024:
            reject(400, "invalid_resume_token")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.new(self.secret, payload, hashlib.sha256).digest()):
                reject(410, "resume_token_expired")
            epoch, kind, sequence, bound = json.loads(payload)
        except (ValueError, TypeError, UnicodeError):
            reject(400, "invalid_resume_token")
        if epoch != self.epoch:
            reject(410, "resume_token_expired")
        if kind not in ("seed", "watch") or type(sequence) is not int or not 0 <= sequence <= self.sequence:
            reject(400, "invalid_resume_token")
        if kind == "watch" and bound != scope.digest():
            reject(400, "resume_scope_mismatch")
        if kind == "watch" and self.ledger and sequence < self.ledger[0][0] - 1:
            reject(410, "resume_token_expired")
        return kind, sequence

    def scope(self, query):
        ids = tuple(sorted({canonical_session(value) for value in repeated(query, "session_ids", self.max_sessions)}))
        return Scope(boolean(query, "include_trigger_sessions"), ids,
                     repeated(query, "exclude_tags", 64))

    def authorized(self, request):
        callback = request.get("duet_authorized")
        return callback() if callback else True

    async def list_response(self, request):
        if not self.authorized(request):
            reject(401, "authorization_expired")
        self.available()
        query = request.query
        allowed = {"cursor", "limit", "include_trigger_sessions", "only_trigger_sessions", "statuses", "grouping_scope", "session_ids", "exclude_tags"}
        if set(query) - allowed:
            reject(400, "unsupported_filter")
        if query.get("grouping_scope"):
            reject(501, "grouping_scope_not_implemented")
        scope = self.scope(query)
        only = boolean(query, "only_trigger_sessions")
        # only_trigger_sessions is the stronger list filter; it does not change watch scope.
        if only:
            scope = Scope(True, scope.ids, scope.excluded)
        statuses = repeated(query, "statuses", len(LIFECYCLES))
        if set(statuses) - LIFECYCLES:
            reject(400, "invalid_filter")
        try:
            limit = int(query.get("limit", str(min(100, self.max_sessions))))
        except ValueError:
            reject(400, "invalid_limit")
        if not 1 <= limit <= self.max_sessions:
            reject(400, "invalid_limit")
        rows = [row for row in reversed(list(self.rows.values())) if scope.matches(row)
                and (not only or row.get("trigger_id")) and (not statuses or row["status"] in statuses)]
        cursor = query.get("cursor")
        if cursor:
            index = next((i for i, row in enumerate(rows) if row["id"] == cursor), None)
            if index is None:
                reject(400, "invalid_list_cursor")
            rows = rows[index + 1:]
        selected = rows[:limit]
        return web.json_response({"data": selected, "next_cursor": selected[-1]["id"] if len(rows) > limit else None,
                                  "resume_token": self.token("seed", self.sequence)}, headers={"Cache-Control": "no-store"})

    @staticmethod
    def change(scope, before, after):
        old, new = scope.matches(before), scope.matches(after)
        if old and not new:
            return "removed", {"id": before["id"], "final": after is None}
        if new:
            return ("changed" if old else "added"), after
        return None

    async def stream(self, request):
        if not self.authorized(request):
            reject(401, "authorization_expired")
        self.available()
        if set(request.query) - {"resume_token", "include_trigger_sessions", "session_ids", "exclude_tags"}:
            reject(400, "unsupported_filter")
        if len(request.query.getall("resume_token", [])) > 1:
            reject(400, "invalid_resume_token")
        scope = self.scope(request.query)
        seed = request.query.get("resume_token")
        header = request.headers.get("Last-Event-ID")
        if not seed and not header:
            reject(400, "resume_token_required")
        parsed = self.parse_token(seed, scope) if seed else None
        kind, after = self.parse_token(header, scope) if header else parsed
        if header and kind != "watch":
            reject(400, "invalid_resume_token")
        if len(self.clients) >= self.max_clients:
            reject(429, "too_many_watchers")
        queue = asyncio.Queue(maxsize=self.queue_size)
        self.clients.add(queue)
        initial_sequence = self.sequence
        initial = [row for row in self.rows.values() if scope.matches(row)] if kind == "seed" else []
        replay = [record for record in self.ledger if after < record[0] <= initial_sequence] if kind == "watch" else []
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        async def send(event, data, sequence=None):
            if not self.authorized(request):
                raise ConnectionError()
            wire = ("id: " + self.token("watch", sequence, scope.digest()) + "\n") if sequence is not None else ""
            wire += "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            await asyncio.wait_for(response.write(wire.encode()), self.write_timeout)

        seen = committed = initial_sequence
        try:
            await response.prepare(request)
            for row in initial:
                await send("added", row)  # Commit snapshot cursor only after all rows.
            for sequence, before, current in replay:
                change = self.change(scope, before, current)
                if change:
                    await send(*change, sequence)
            await send("sync", {}, initial_sequence)  # Real current source barrier; valid first frame even if empty.
            last_write = time.monotonic()
            while not self.closed and self.authorized(request):
                transport = request.transport
                if transport is None or transport.is_closing():
                    break
                try:
                    record = await asyncio.wait_for(queue.get(), min(self.heartbeat, 1))
                except asyncio.TimeoutError:
                    if not self.authorized(request):
                        break
                    if time.monotonic() - last_write >= self.heartbeat:
                        if seen > committed:
                            await send("sync", {}, seen)
                            committed = seen
                        else:
                            await asyncio.wait_for(response.write(b": duet task-watch keepalive\n\n"), self.write_timeout)
                        last_write = time.monotonic()
                    continue
                if record is None:
                    break
                sequence, before, current = record
                if sequence <= seen:
                    continue
                seen = sequence
                change = self.change(scope, before, current)
                if change:
                    await send(*change, sequence)
                    committed = sequence
                    last_write = time.monotonic()
            await asyncio.wait_for(response.write_eof(), self.write_timeout)
        except (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(queue)
        return response

    async def start(self, app):
        async def monitor():
            while not self.closed:
                self.observe()
                changed = getattr(self.engine, "changed", None)
                if changed is not None and not changed.is_set():
                    try:
                        await asyncio.wait_for(changed.wait(), self.poll_interval)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self.poll_interval)
        self.monitor = asyncio.create_task(monitor())

    async def close(self, app):
        self.closed = True
        self.disconnect_clients()
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.aibo.save is self.save_tap:
            self.aibo.save = self.original_save


def register_task_watch(app, aibo, engine=None, **kwargs):
    if app.frozen:
        raise RuntimeError("Register task watch before app.freeze")
    watch = TaskWatch(aibo, engine, **kwargs)
    aibo.save = watch.save_tap

    @web.middleware
    async def original_session_watch(request, handler):
        if request.method == "GET":
            if request.path in WATCH_PATHS:
                return await watch.stream(request)
            if request.path in LIST_PATHS:
                return await watch.list_response(request)
        return await handler(request)

    app.middlewares.append(original_session_watch)
    for path in WATCH_PATHS:
        app.router.add_get(path, watch.stream)
    app.on_startup.append(watch.start)
    app.on_shutdown.append(watch.close)
    return watch
