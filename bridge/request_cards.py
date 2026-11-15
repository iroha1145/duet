"""Native request cards for real, currently waiting Aibo tool approvals.

Wire: rbe/get,list,answer; jae/lae/zae display; v4n/x4n answer;
yyb/wo40 other-work settings in the retained Android client. Tokens are local
process capabilities, bound to the authenticated account and exact approval.
They carry no tool arguments. SDK and native answers share Aibo's reservation.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from aiohttp import web

from .aibo import AiboError
from .auth import AUTH_PRINCIPAL
from .rest import ACCOUNT_ID
from . import user_questions, plan_mode

REQUEST_CARDS = web.AppKey("duet_request_cards", object)
CLIENT_FLAGS = {"can_pick", "can_pick_questions", "can_plan", "can_next_request",
                "can_draw_long_plans", "can_plan_modes", "can_draw_previews", "can_sign",
                "can_sign_pick", "can_sign_pick_several", "can_connector_question"}
UNSUPPORTED_TOOLS = {"askuserquestion", "exitplanmode", "enterplanmode"}


class RequestCards:
    def __init__(self, aibo, data_dir, *, clock=time.time, token_ttl=300):
        self.aibo = aibo
        self.path = Path(data_dir) / "request-card-settings.json"
        self.clock, self.token_ttl = clock, token_ttl
        self.secret = secrets.token_bytes(32)
        self.settings = {"version": 1, "accounts": {}}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("version") != 1 or not isinstance(saved.get("accounts"), dict):
                raise ValueError("Invalid request-card settings")
            self.settings = saved

    @staticmethod
    def subject(request):
        principal = request.get(AUTH_PRINCIPAL)
        identity = [principal.account_id, principal.organization_id, principal.username] if principal else [ACCOUNT_ID]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def authorized(request):
        if not request.get("duet_authorized", lambda: True)():
            raise AiboError("unauthorized", "Authorization expired", 401)

    @staticmethod
    def text(body, name):
        value = body.get(name)
        if not isinstance(value, str) or not value or len(value) > 256:
            raise AiboError("invalid_argument", "Invalid " + name)
        return value

    @staticmethod
    def validate(body, allowed):
        if set(body) - allowed:
            raise AiboError("invalid_argument", "Unsupported request fields")
        if any(name in body and body[name] is not None and type(body[name]) is not bool for name in CLIENT_FLAGS):
            raise AiboError("invalid_argument", "Client capabilities must be boolean")

    def pending(self, session):
        return [(rid, item) for rid in session.get("pending_approvals", {})
                if (item := self.aibo.approval_pending(session, rid)) is not None]

    def chinese(self):
        project = self.aibo.home.state.get("project") or {}
        return project.get("settings", {}).get("language", "").lower().startswith("zh")

    def display(self, session, rid, pending):
        tool, arguments = pending.get("tool"), pending.get("arguments")
        chinese = self.chinese()
        if tool == user_questions.QUESTION_TOOL:
            try:
                user_questions.questions(arguments)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return None
            if not callable(getattr(self.aibo.backend, "answer_question", None)):
                return None
            receipt = session.get("approval_receipts", {}).get(rid)
            if receipt and receipt.get("version") == self.aibo.approval_version(session, rid, pending) and receipt.get("outcome") != "accepted":
                return None
            return {"title": "请回答问题" if chinese else "Answer questions", "rows": []}
        if tool == plan_mode.EXIT and pending.get("kind") == "plan":
            try:
                version = self.aibo.approval_version(session, rid, pending)
                receipt = session.get("approval_receipts", {}).get(rid)
                if receipt and receipt.get("version") == version and receipt.get("outcome") != "accepted":
                    return None
                display = plan_mode.display(pending)
                if chinese:
                    display["title"] = "审阅计划"
                    display["rows"][0]["label"] = "计划"
                return display
            except (ValueError, UnicodeError):
                return None
        if (not isinstance(tool, str) or not tool or len(tool) > 256
                or tool.rsplit("__", 1)[-1].lower() in UNSUPPORTED_TOOLS
                or pending.get("kind", "tool") != "tool" or not isinstance(arguments, dict)):
            return None
        try:
            value = json.dumps(arguments, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            if len(value.encode("utf-8")) > 16384:
                return None
            version = self.aibo.approval_version(session, rid, pending)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        receipt = session.get("approval_receipts", {}).get(rid)
        if receipt and receipt.get("version") == version and receipt.get("outcome") != "accepted":
            return None
        return {"title": "是否允许调用此工具？" if chinese else "Allow this tool call?",
                "rows": [{"label": "工具" if chinese else "Tool", "value": tool},
                         {"label": "参数" if chinese else "Arguments", "value": value}]}

    @staticmethod
    def supported(pending, body):
        if pending.get("kind") == "plan":
            return body.get("can_plan") is True
        if pending.get("tool") != user_questions.QUESTION_TOOL:
            return True
        count = len(pending.get("arguments", {}).get("questions", []))
        return body.get("can_pick") is not False and (count <= 1 or body.get("can_pick_questions") is not False)

    def token(self, subject, session, rid, pending):
        issued = int(self.clock())
        payload = [subject, session["id"], rid, self.aibo.approval_version(session, rid, pending), issued, issued + self.token_ttl]
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
        signature = base64.urlsafe_b64encode(hmac.digest(self.secret, encoded, "sha256")).rstrip(b"=")
        return (encoded + b"." + signature).decode("ascii")

    def verify(self, token, subject, sid, rid):
        if not isinstance(token, str) or len(token) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", token):
            return None
        try:
            encoded, signature = token.encode("ascii").split(b".")
            expected = base64.urlsafe_b64encode(hmac.digest(self.secret, encoded, "sha256")).rstrip(b"=")
            if not hmac.compare_digest(signature, expected):
                return None
            value = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            if (not isinstance(value, list) or len(value) != 6 or value[:3] != [subject, sid, rid]
                    or not isinstance(value[3], str) or type(value[4]) is not int or type(value[5]) is not int
                    or value[4] > self.clock() or value[5] <= self.clock()
                    or value[5] - value[4] != self.token_ttl):
                return None
            return value[3]
        except (ValueError, TypeError, UnicodeError):
            return None

    async def list_cards(self, request, body):
        self.validate(body, CLIENT_FLAGS)
        entries = []
        for session in self.aibo.state["sessions"].values():
            if not self.aibo.home.channel(session["channel_id"]):
                continue
            for rid, pending in self.pending(session):
                entry = {"session_id": session["id"], "wait_key": rid,
                         "can_open_card": self.display(session, rid, pending) is not None and self.supported(pending, body)}
                if not entry["can_open_card"]:
                    entry["why_not_sentence"] = "This request cannot be answered with a tool approval card."
                entries.append(entry)
                if len(entries) > 512:
                    return {"state": "at_least", "entries": entries[:512]}
        return {"state": "ok" if entries else "none", "entries": entries,
                "nothing_waits_as_of_ms": int(self.clock() * 1000)}

    async def get_card(self, request, body):
        self.validate(body, CLIENT_FLAGS | {"session_id"})
        session = self.aibo.session(self.text(body, "session_id"))
        pending = self.pending(session)
        if not pending:
            receipts = [item for item in session.get("approval_receipts", {}).values()
                        if item.get("run_id") == session.get("user_id") and item.get("outcome") == "accepted"]
            if receipts:
                item = receipts[-1]
                return {"state": "answered", "standing_answer": item.get("standing_answer", "allow_once" if item["allow"] else "decline")}
            return {"state": "nothing_waits"}
        rid, item = pending[0]
        display = self.display(session, rid, item)
        if item.get("kind") == "plan" and body.get("can_plan") is not True:
            return {"state": "not_served", "not_served_sentence": "This client cannot display a complete plan for approval."}
        if display is None or not self.supported(item, body):
            return {"state": "not_served", "not_served_sentence": "This request requires another approval interface."}
        card = {"request_id": rid, "token": self.token(self.subject(request), session, rid, item), "display": display,
                "session_id": session["id"], "tool_use_id": item.get("tool_call_id", rid)}
        if item["tool"] == user_questions.QUESTION_TOOL:
            questions = user_questions.card_questions(item["arguments"])
            if len(questions) == 1:
                card["question"] = questions[0]
            else:
                card["questions"] = {"entries": questions}
        if item.get("kind") == "plan":
            card["plan_modes"] = {"entries": [{"mode": "manual", "sentence":
                "批准计划；执行操作前仍需逐项确认。" if self.chinese() else "Approve plan; keep asking before execution."}]}
        return {"state": "card", "leading_request_id": rid, "leading_tool_use_id": card["tool_use_id"], "card": card}

    async def answer(self, request, body):
        if body.get("answer") == "pick":
            return await self.answer_pick(request, body)
        # Pick/sign/plan mutations are not ordinary tool approvals. Do not accept
        # an unknown field and silently turn it into permission for execution.
        allowed = {"session_id", "request_id", "token", "answer", "plan_mode", "can_sign", "device_attestation"}
        if (set(body) - allowed or body.get("device_attestation") is not None
                or body.get("answer") not in ("allow_once", "decline")
                or body.get("can_sign") is not None and type(body["can_sign"]) is not bool):
            return {"outcome": "not_served"}
        session = self.aibo.session(self.text(body, "session_id"))
        rid = self.text(body, "request_id")
        version = self.verify(body.get("token"), self.subject(request), session["id"], rid)
        if version is None:
            return {"outcome": "stale_token"}
        pending = self.aibo.approval_pending(session, rid)
        receipt = session.get("approval_receipts", {}).get(rid, {})
        is_plan = pending is not None and pending.get("kind") == "plan" or receipt.get("tool") == plan_mode.EXIT
        if body.get("plan_mode") is not None and (not is_plan or body["plan_mode"] != "manual"):
            return {"outcome": "not_served"}
        if pending is not None and self.display(session, rid, pending) is None:
            return {"outcome": "not_served"}
        if pending is not None and pending["tool"] == user_questions.QUESTION_TOOL and body["answer"] != "decline":
            return {"outcome": "not_served"}
        return await self.aibo.answer_approval(session, rid, body["answer"] == "allow_once", version=version,
            authorized=request.get("duet_authorized", lambda: True))

    async def answer_pick(self, request, body):
        allowed = {"session_id", "request_id", "token", "answer", "option_index", "option_indexes",
                   "other_text", "picks", "can_sign", "device_attestation"}
        if (set(body) - allowed or body.get("device_attestation") is not None
                or body.get("can_sign") is not None and type(body["can_sign"]) is not bool):
            return {"outcome": "not_served"}
        session = self.aibo.session(self.text(body, "session_id"))
        rid = self.text(body, "request_id")
        version = self.verify(body.get("token"), self.subject(request), session["id"], rid)
        if version is None:
            return {"outcome": "stale_token"}
        pending = self.aibo.approval_pending(session, rid)
        answers = None
        if pending is not None:
            if pending["tool"] != user_questions.QUESTION_TOOL or self.display(session, rid, pending) is None:
                return {"outcome": "not_served"}
            if self.aibo.approval_version(session, rid, pending) != version:
                return {"outcome": "changed"}
            try:
                answers = user_questions.pick_answers(pending["arguments"], body)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return {"outcome": "bad_text"}
        return await self.aibo.answer_approval(session, rid, True, version=version, answers=answers,
            authorized=request.get("duet_authorized", lambda: True))

    def save_settings(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="request-card-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(self.settings, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def get_settings(self, request, body):
        self.validate(body, set())
        values = self.settings["accounts"].get(self.subject(request), {"show": True, "notify": False})
        return {"state": "ok", "show": values["show"], "notify": values["notify"], "push_live": False}

    async def set_settings(self, request, body):
        self.validate(body, {"show", "notify"})
        if not body or any(type(value) is not bool for value in body.values()):
            raise AiboError("invalid_argument", "Settings must be boolean")
        subject = self.subject(request)
        before = self.settings["accounts"].get(subject)
        self.settings["accounts"][subject] = {"show": True, "notify": False, **(before or {}), **body}
        try:
            self.save_settings()
        except OSError:
            if before is None:
                self.settings["accounts"].pop(subject)
            else:
                self.settings["accounts"][subject] = before
            raise
        return await self.get_settings(request, {})

    def handler(self, operation):
        async def handle(request):
            try:
                self.authorized(request)
                body = await request.json()
                if not isinstance(body, dict):
                    raise AiboError("invalid_argument", "Expected an object")
                result = await getattr(self, operation)(request, body)
                self.authorized(request)
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            except AiboError as error:
                return web.json_response({"error": {"code": error.code, "message": error.message}}, status=error.status,
                                         headers={"Cache-Control": "no-store"})
            except (ValueError, UnicodeError):
                return web.json_response({"error": {"code": "invalid_argument"}}, status=400)
            except OSError:
                return web.json_response({"error": {"code": "unavailable"}}, status=503)
        return handle


def register_request_cards(app, home, aibo, data_dir, *, watch, **kwargs):
    cards = RequestCards(aibo, data_dir, **kwargs)
    app[REQUEST_CARDS] = cards
    for prefix in ("", "/api"):
        base = prefix + "/v1/code/aibo/"
        for suffix, method in (("request-cards:list", "list_cards"), ("request-cards:get", "get_card"),
                               ("request-cards:answer", "answer"), ("other-work-settings:get", "get_settings"),
                               ("other-work-settings:set", "set_settings")):
            app.router.add_post(base + suffix, cards.handler(method))
    if watch is not None and watch.aibo is aibo and callable(getattr(aibo.backend, "approve", None)):
        # z1s.C/fhd/v2e require the list capability as well as card support
        # before the original global question/plan controller will poll.
        home.capabilities = sorted(set(home.capabilities) | {
            "request_cards_v1", "request_cards_list_v1", "sessions_watch_v1"})
    return cards
