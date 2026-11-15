"""Original POST other-sessions:list, projected from real local worker state.

Native evidence: vwb.g POSTs ncz/ocz; s8q.I parses state/entries; jba(11)
opens ywb -> yqd0.t -> gkg using the separate session-page wire. This adapter
does NOT claim a Claude Code/Cowork host or implement that separate page API.
Only existing, typed local task/schedule workers appear, never coordinators.
Unknown product 'duet' uses the native parser's generic product fallback.

page.can_open stays false. task and duet_context retain the exact owning
channel/root needed for a client to navigate its existing task conversation.
Do not advertise session_page_v1 or use a vendor URL as an opening fallback.
No conversation ledger, history mutation, background task or network client.
"""
import base64
import hashlib
import hmac
import json
import re

from aiohttp import web

from .aibo import AiboError, canonical_session
from .auth import AUTH_MANAGER, AUTH_PRINCIPAL
from .rest import ACCOUNT_ID, ORGANIZATION_ID


OTHER_SESSIONS = web.AppKey("duet_other_sessions", object)
CAPABILITY = "other_sessions_list_v1"
BOOL_FIELDS = frozenset({"include_project_threads", "include_ended", "can_plan", "can_next_request",
                         "can_sign", "can_sign_pick", "can_sign_pick_several", "can_connector_question",
                         "honour_show_setting", "include_own_code_sessions"})
FIELDS = BOOL_FIELDS | {"cursor", "session_id"}
SESSION = re.compile(r"(?:session|cse)_[A-Za-z0-9-]{1,220}\Z")


class OtherSessions:
    def __init__(self, home, aibo, watch, cards, *, tasks=None, page_size=100):
        if watch.aibo is not aibo or cards.aibo is not aibo or aibo.home is not home:
            raise ValueError("Other sessions must share the owning Aibo state")
        if tasks is not None and (tasks.aibo is not aibo or tasks.home is not home):
            raise ValueError("Tasks must share the owning Aibo state")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Invalid other sessions page size")
        self.home, self.aibo, self.watch, self.cards = home, aibo, watch, cards
        self.tasks, self.page_size = tasks, page_size

    def authorize(self, request):
        self.cards.authorized(request)
        principal = request.get(AUTH_PRINCIPAL)
        if principal is None:
            raise AiboError("unauthorized", "Account authentication required", 401)
        if principal.account_id != ACCOUNT_ID or principal.organization_id != ORGANIZATION_ID:
            raise AiboError("forbidden", "This account does not own this workspace", 403)
        manager = request.app.get(AUTH_MANAGER)
        if manager is not None:
            owner = manager.public_user()
            if owner is None or principal.username != owner["username"]:
                raise AiboError("forbidden", "This account does not own this workspace", 403)
        return principal

    @staticmethod
    def validate(body):
        if not isinstance(body, dict):
            raise AiboError("invalid_argument", "Expected an object")
        if body.keys() - FIELDS:
            raise AiboError("not_implemented", "Unsupported other sessions request fields", 501)
        for name in BOOL_FIELDS:
            if name in body and body[name] is not None and type(body[name]) is not bool:
                raise AiboError("invalid_argument", "Session capabilities and filters must be boolean")
        for name in ("cursor", "session_id"):
            if name in body and body[name] is not None and (not isinstance(body[name], str)
                    or not 0 < len(body[name]) <= (2048 if name == "cursor" else 256)):
                raise AiboError("invalid_argument", "Invalid " + name)
        if body.get("session_id") is not None and not SESSION.fullmatch(body["session_id"]):
            raise AiboError("invalid_argument", "Invalid session_id")
        if body.get("session_id") and body.get("cursor"):
            raise AiboError("invalid_argument", "A single session request cannot be paginated")
        if body.get("include_own_code_sessions"):
            raise AiboError("not_implemented", "Claude Code sessions are not hosted by this service", 501)

    def _scope(self, request, body):
        # Canonical semantic filter + caller identity. Client rendering abilities
        # are also bound, so a cursor can never change its card/visibility scope.
        filters = {name: body.get(name) for name in sorted(FIELDS - {"cursor"})}
        if filters.get("session_id"):
            filters["session_id"] = canonical_session(filters["session_id"])
        data = json.dumps([self.cards.subject(request), filters], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def _cursor(self, scope, key):
        payload = json.dumps(["other-sessions-v1", self.watch.epoch, scope, list(key)], separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = base64.urlsafe_b64encode(hmac.digest(self.watch.secret, encoded, "sha256")).rstrip(b"=")
        return (encoded + b"." + signature).decode("ascii")

    def _resume(self, cursor, scope):
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", cursor):
                raise ValueError()
            encoded, supplied = cursor.encode("ascii").split(b".")
            expected = base64.urlsafe_b64encode(hmac.digest(self.watch.secret, encoded, "sha256")).rstrip(b"=")
            if not hmac.compare_digest(supplied, expected):
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            if (not isinstance(value, list) or len(value) != 4
                    or value[:3] != ["other-sessions-v1", self.watch.epoch, scope]
                    or not isinstance(value[3], list) or len(value[3]) != 2
                    or any(not isinstance(item, str) or not item or len(item) > 256 for item in value[3])):
                raise ValueError()
            return tuple(value[3])
        except (ValueError, TypeError, UnicodeError):
            raise AiboError("invalid_cursor", "Cursor is invalid for this account or filter", 410) from None

    def _worker(self, session, principal):
        cid = session.get("channel_id")
        channel = self.home.channel(cid)
        if (not channel or channel.get("created_by_account_id") != principal.account_id
                or channel.get("archived_at") or channel.get("disabled_at")):
            return None
        sid = session.get("id")
        if not isinstance(sid, str) or sid == canonical_session(channel.get("channel_session_id") or ""):
            return None
        kind = "task" if session.get("task_id") else "task_discussion" if session.get("discussion_task_id") else "scheduled_task" if session.get("trigger_id") else None
        root = session.get("root_id")
        if not kind or not isinstance(root, str) or not any(message.get("id") == root
                and message.get("thread_root_id") == root for message in session.get("messages", ())):
            return None
        # Never route a worker into another room's task conversation.
        if session.get("task_channel_id", cid) != cid:
            return None
        return channel, kind, root

    def _task(self, session, cid, root):
        tid = session.get("task_id") or session.get("discussion_task_id") or session.get("trigger_id")
        if not tid or self.tasks is None:
            return None
        try:
            row, _, _ = self.tasks.get(cid, tid)
        except AiboError as error:
            if error.status == 404:
                return None
            raise
        # The canonical task projection also covers schedules created before
        # task overrides existed. Do not require an optional override record.
        # An older run/discussion must not open a newer task conversation.
        if row.get("thread_own_session_id") != session["id"] or row["data"].get("threadRootId") != root:
            return None
        return {"channel_id": cid, "task_id": tid, "thread_id": root}

    def entry(self, session, source, context):
        channel, kind, root = context
        pending = self.cards.pending(session)
        waiting = bool(pending) and session.get("phase") == "running"
        state = "needs_you" if waiting else {"running": "working", "error": "failed", "interrupted": "failed"}.get(session.get("phase"), "idle")
        # s8q.I's wire key for the display bucket is literally files_under;
        # stage is a DIFFERENT optional lifecycle enum (starting/cut_off/ended/
        # done/at_rest). Never send a display-bucket value as stage.
        bucket = {"needs_you": "needs_input", "working": "working", "failed": "couldnt_finish", "idle": "ready_for_review"}[state]
        if state == "idle" and source.get("status_bucket") in {"done", "settled", "unknown"}:
            bucket = "settled"
        # The generic unknown-product fallback is deliberately used. These are
        # duet workers, not Claude Code/Cowork nor new-project chat replicas.
        row = {"session_id": session["id"], "title": session.get("title", "duet task"), "state": state,
               "where": {"product": "duet", "runs_on": "own_runner"},
               "connection": source.get("connection_status", "connected"),
               "started_at": session["created_at"], "last_event_at": session["updated_at"], "files_under": bucket,
               "page": {"can_open": False, "needs_device_proof": False, "can": []},
               "duet_context": {"kind": kind, "channel_id": session["channel_id"], "thread_root_id": root}}
        if session.get("phase") in {"idle", "error", "interrupted"}:
            row["stage"] = "cut_off" if session["phase"] == "interrupted" else "ended" if state == "failed" else "at_rest"
        task = self._task(session, session["channel_id"], root)
        if task is not None:
            row["task"] = task
            row["duet_context"]["task_id"] = task["task_id"]
        # s8q.I retains page verbatim as zvb.u. The narrow native adapter uses
        # this context plus the parsed yvb task to enter existing Tasks UI.
        row["page"].update(product="duet", why_not="open_in_channel_tasks",
            why_not_sentence="Open this duet worker from its channel's Tasks conversation.",
            duet_context=dict(row["duet_context"]))
        if pending:
            rid, approval = pending[0]
            can_open = self.cards.display(session, rid, approval) is not None
            row["request"] = {"wait_key": rid, "can_open_card": can_open}
            if not can_open:
                row["request"]["why_not_sentence"] = "This request requires another approval interface."
        return row

    async def listing(self, request, body):
        principal = self.authorize(request)
        self.validate(body)
        self.watch.available()
        # Same persisted single-account settings as the native settings route.
        settings = await self.cards.get_settings(request, {})
        self.authorize(request)
        if body.get("honour_show_setting") and not settings["show"]:
            return {"state": "off", "entries": [], "next_cursor": None}
        scope = self._scope(request, body)
        after = self._resume(body["cursor"], scope) if body.get("cursor") else None
        requested = canonical_session(body["session_id"]) if body.get("session_id") else None
        rows, total_waiting = [], 0
        for sid, source in self.watch.rows.items():
            session = self.aibo.state["sessions"].get(sid)
            if session is None or requested is not None and sid != requested:
                continue
            context = self._worker(session, principal)
            if context is None:
                continue
            if body.get("include_project_threads") is False and context[1] in {"task", "task_discussion"}:
                continue
            if not body.get("include_ended") and (session.get("ended_at") or source.get("status") == "archived"):
                continue
            row = self.entry(session, source, context)
            total_waiting += row["state"] == "needs_you"
            key = (session["created_at"], sid)
            if after is None or key < after:
                rows.append((key, row))
        # Creation time is immutable. Updates between pages do not duplicate or
        # skip an existing row solely because its latest activity timestamp moved.
        rows.sort(key=lambda item: item[0], reverse=True)
        selected = rows[:self.page_size]
        self.authorize(request)
        return {"state": "ok" if selected else "none", "entries": [row for _, row in selected],
                "next_cursor": self._cursor(scope, selected[-1][0]) if len(rows) > self.page_size else None,
                "totals": {"needs_you": total_waiting}}

    async def handle(self, request):
        try:
            self.authorize(request)
            body = await request.json()
            result = await self.listing(request, body)
            self.authorize(request)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except AiboError as error:
            return web.json_response({"error": {"code": error.code, "message": error.message}}, status=error.status,
                                     headers={"Cache-Control": "no-store"})
        except (ValueError, UnicodeError):
            return web.json_response({"error": {"code": "invalid_argument"}}, status=400,
                                     headers={"Cache-Control": "no-store"})


def register_other_sessions(app, home, aibo, *, watch, cards, tasks=None, advertise=False, **kwargs):
    """Register after Aibo/watch/cards/tasks. Root enables the single list cap.

    advertise defaults false until the caller has verified its native row-entry
    integration. It never enables session_page_v1 or foreign product features.
    """
    service = OtherSessions(home, aibo, watch, cards, tasks=tasks, **kwargs)
    app[OTHER_SESSIONS] = service
    for prefix in ("", "/api"):
        app.router.add_post(prefix + "/v1/code/aibo/other-sessions:list", service.handle)
    if advertise:
        home.capabilities = sorted(set(home.capabilities) | {CAPABILITY})
    return service
