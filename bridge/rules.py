"""Account standing rules: explicit native confirmation and exact tool scopes.

SQLite is the permission authority. Native cards are idempotent projections,
not permission grants. These rules govern engine dispatch, not arbitrary OS
effects of an already permitted shell/browser (the executor is not a sandbox).
"""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from duet_server.tools import tool_definitions
from .rest import ACCOUNT_ID

RULES = web.AppKey("duet_standing_rules", object)
PROPOSE_TOOL = "propose_standing_rule"
KINDS = ("never", "ask_first", "dont_ask")
LABELS = {"never": "wont_do", "ask_first": "asks_first", "dont_ask": "does_without_asking"}
READ_ONLY = frozenset({"read_file", "list_files"})
MAX_RULES = 100
SCOPE_TEXT = "Entire account; exact tool calls only. Other tools may have similar effects."


class RuleError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class RuleDenied(RuntimeError):
    def __init__(self, rule_ids=(), message="A standing rule blocks this tool call."):
        self.rule_ids = tuple(rule_ids)
        super().__init__(message)


@dataclass(frozen=True)
class Decision:
    kind: str
    fingerprint: tuple
    rule_ids: tuple


def _text(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RuleError("invalid_argument", "Invalid " + name)
    return value


def _words(names):
    # Never let model-provided prose imply a stronger semantic prohibition.
    return "Invoke these tools: " + ", ".join(names)


class RulesStore:
    def __init__(self, data_dir, *, clock=None):
        directory = Path(data_dir) / "rules"
        if directory.is_symlink():
            raise ValueError("Rules directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / "rules.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Rules database must not be a symlink")
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              schema_version INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT);
          INSERT OR IGNORE INTO metadata VALUES(1,1,0,NULL);
          CREATE TABLE IF NOT EXISTS rules(rule_id TEXT PRIMARY KEY, body TEXT NOT NULL,
              projected_version INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS proposals(session_id TEXT NOT NULL, source_message_id TEXT NOT NULL,
              tool_call_id TEXT NOT NULL, rule_id TEXT NOT NULL, PRIMARY KEY(session_id,source_message_id,tool_call_id));
          CREATE TABLE IF NOT EXISTS receipts(request_key TEXT PRIMARY KEY, outcome TEXT NOT NULL,
              result_updated_at TEXT NOT NULL);
        """)
        if self.db.execute("SELECT schema_version FROM metadata").fetchone()[0] != 1:
            self.db.close()
            raise ValueError("Unsupported rules database version")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.closed = False

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

    def _stamp(self, record):
        previous = self.db.execute("SELECT revision,updated_at FROM metadata WHERE singleton=1").fetchone()
        now = self.clock()
        if previous["updated_at"]:
            before = datetime.fromisoformat(previous["updated_at"].replace("Z", "+00:00"))
            if now <= before:
                now = before + timedelta(microseconds=1)
        record["version"] = previous["revision"] + 1
        record["updated_at"] = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.db.execute("UPDATE metadata SET revision=?,updated_at=? WHERE singleton=1",
                        (record["version"], record["updated_at"]))

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        value = json.loads(row["body"])
        if (value.get("rule_kind") not in KINDS or value.get("state") not in ("pending", "active", "removed")
                or not isinstance(value.get("tool_names"), list) or not value["tool_names"]
                or any(not isinstance(name, str) or not name for name in value["tool_names"])):
            raise ValueError("Invalid persisted standing rule")
        value["projected_version"] = row["projected_version"]
        return value

    def get(self, rule_id):
        return self._decode(self.db.execute("SELECT body,projected_version FROM rules WHERE rule_id=?", (rule_id,)).fetchone())

    def all(self):
        return [self._decode(row) for row in self.db.execute("SELECT body,projected_version FROM rules ORDER BY rule_id")]

    def _put(self, record):
        body = {key: value for key, value in record.items() if key != "projected_version"}
        self.db.execute("INSERT INTO rules(rule_id,body) VALUES(?,?) ON CONFLICT(rule_id) DO UPDATE SET body=excluded.body",
                        (record["rule_id"], json.dumps(body, ensure_ascii=False, allow_nan=False)))

    def propose(self, names, kind, source, tool_call_id, *, replace=None, expect=None):
        with self.transaction():
            found = self.db.execute("SELECT rule_id FROM proposals WHERE session_id=? AND source_message_id=? AND tool_call_id=?",
                                    (source["session_id"], source["message_id"], tool_call_id)).fetchone()
            if found:
                previous = self.get(found["rule_id"])
                if (previous["tool_names"] != names or previous["rule_kind"] != kind
                        or previous.get("replaces", {}).get("rule_id") != replace
                        or previous.get("replaces", {}).get("updated_at") != expect):
                    raise RuleError("conflict", "This proposal request already has a different result", 409)
                return previous
            existing = self.all()
            if sum(rule["state"] != "removed" for rule in existing) >= MAX_RULES:
                raise RuleError("resource_exhausted", "Remove a rule before adding another", 429)
            old = self.get(replace) if replace else None
            if replace and (old is None or old["state"] != "active" or old["updated_at"] != expect):
                raise RuleError("conflict", "The rule to replace has changed; open Rules and try again", 409)
            record = {"rule_id": "rule_" + str(uuid4()), "rule_kind": kind, "state": "pending",
                      "tool_names": names, "words": _words(names), "card_message_id": "cmsg_" + str(uuid4()),
                      "source": source, "suggestion": "change" if old else "new"}
            if old:
                record["replaces"] = {key: old[key] for key in ("rule_id", "words", "rule_kind", "updated_at")}
            self._stamp(record)
            record["created_at"] = record["updated_at"]
            self._put(record)
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?)", (source["session_id"], source["message_id"], tool_call_id, record["rule_id"]))
        return self.get(record["rule_id"])

    def mutate(self, operation, rule_id, expected, *, kind=None, card_id=None):
        key = hashlib.sha256(json.dumps([operation, rule_id, expected, kind, card_id], separators=(",", ":")).encode()).hexdigest()
        with self.transaction():
            record = self.get(rule_id)
            if record is None:
                return "gone", None
            if card_id is not None and record["card_message_id"] != card_id:
                raise RuleError("not_found", "Unknown standing-rule card", 404)
            receipt = self.db.execute("SELECT * FROM receipts WHERE request_key=?", (key,)).fetchone()
            if receipt:
                return (receipt["outcome"] if receipt["result_updated_at"] == record["updated_at"] else "conflict"), record
            if record["updated_at"] != expected:
                return "conflict", record
            if record["state"] == "removed":
                return "gone", record
            if operation == "confirm":
                if record["state"] != "pending":
                    return "conflict", record
                replacing = record.get("replaces")
                if replacing:
                    old = self.get(replacing["rule_id"])
                    if old is None or old["state"] != "active" or old["updated_at"] != replacing["updated_at"]:
                        return "conflict", record
                    old.update(state="removed", outcome="replaced")
                    self._stamp(old)
                    self._put(old)
                record.update(state="active", outcome="changed" if replacing else "added")
                outcome = "confirmed"
            elif operation == "update":
                if record["state"] != "active":
                    return "conflict", record
                record.update(rule_kind=kind, suggestion="change", outcome="changed")
                outcome = "updated"
            elif operation == "remove":
                pending = record["state"] == "pending"
                record.update(state="removed", outcome="not_changed" if pending and record.get("replaces") else "not_added" if pending else "removed")
                if not pending:
                    record["suggestion"] = "stop"
                outcome = "removed"
            else:
                raise ValueError("Unknown standing-rule operation")
            self._stamp(record)
            self._put(record)
            self.db.execute("INSERT INTO receipts VALUES(?,?,?)", (key, outcome, record["updated_at"]))
        return outcome, self.get(rule_id)

    def mark_projected(self, rule_id, version):
        self.db.execute("UPDATE rules SET projected_version=MAX(projected_version,?) WHERE rule_id=?", (version, rule_id))

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()


class RulesManager:
    def __init__(self, store, aibo, engine=None):
        self.store, self.aibo, self.engine = store, aibo, engine

    def available_names(self):
        names = {item["name"] for item in tool_definitions()}
        if self.engine is not None:
            names.update(self.engine.current_extensions())
            names.update(self.engine.policy_tool_names)
        return names

    def dont_ask_allowed(self, names):
        extensions = self.engine.current_extensions() if self.engine is not None else {}
        return bool(names) and all(name in READ_ONLY and name not in extensions for name in names)

    def _scope(self, values):
        if (not isinstance(values, list) or not 1 <= len(values) <= 16
                or any(not isinstance(name, str) or not 0 < len(name) <= 128 for name in values)):
            raise RuleError("invalid_scope", "Choose between 1 and 16 exact registered tool names")
        names = sorted(set(values))
        if not set(names).issubset(self.available_names()):
            raise RuleError("invalid_scope", "Unknown tool scope; propose only exact registered tools")
        return names

    def _kind(self, kind, names):
        if kind not in KINDS:
            raise RuleError("invalid_argument", "Unknown standing-rule kind")
        if kind == "dont_ask" and not self.dont_ask_allowed(names):
            raise RuleError("always_asks", "Without-asking rules are limited to native read_file and list_files", 409)

    def listed(self, record):
        return {"rule_id": record["rule_id"], "words": record["words"], "rule_kind": record["rule_kind"],
                "state": record["state"], "updated_at": record["updated_at"], "can_remove": True,
                "action": record["words"], "label": LABELS[record["rule_kind"]], "scope_text": SCOPE_TEXT,
                "kind_choices": [{"kind": kind, "label": LABELS[kind],
                                  "allowed": kind != "dont_ask" or self.dont_ask_allowed(record["tool_names"]),
                                  **({"reason": "always_asks"} if kind == "dont_ask" and not self.dont_ask_allowed(record["tool_names"]) else {})}
                                 for kind in KINDS]}

    def card(self, record):
        source = record["source"]
        body = {"kind": "standing_rule", "rule_id": record["rule_id"], "words": record["words"],
                "rule_kind": record["rule_kind"], "state": "pending_change" if record["state"] == "pending" and record.get("replaces") else record["state"],
                "updated_at": record["updated_at"], "suggestion": record["suggestion"],
                "source_message_id": source["message_id"], "action": record["words"],
                "label": LABELS[record["rule_kind"]], "scope_text": SCOPE_TEXT}
        if record.get("outcome"):
            body["outcome"] = record["outcome"]
        if record.get("replaces"):
            old = record["replaces"]
            body.update(replaces_rule_id=old["rule_id"], replaces_words=old["words"], replaces_rule_kind=old["rule_kind"],
                        replaces_action=old["words"], replaces_label=LABELS[old["rule_kind"]])
        value = {"id": record["card_message_id"], "thread_root_id": source["thread_root_id"],
                 "created_at": record["created_at"], "author_session_id": source["session_id"],
                 "body": json.dumps(body, ensure_ascii=False), "server_notice": True,
                 "server_card_kind": "standing_rule", "in_timeline": True, "_complete": True}
        if record["updated_at"] != record["created_at"]:
            value["edited_at"] = record["updated_at"]
        return value

    def project(self, record):
        source = record["source"]
        session = self.aibo.state["sessions"].get(source["session_id"])
        # Account rules survive source-channel deletion. Never recreate a
        # deleted channel, move an old card to the current root, or change its ID.
        if session is None or session["channel_id"] != source["channel_id"] or not self.aibo.home.channel(source["channel_id"]):
            self.store.mark_projected(record["rule_id"], record["version"])
            return
        desired = self.card(record)
        notice = next((item for item in session["messages"] if item["id"] == record["card_message_id"]), None)
        if notice is None:
            index = len(session["messages"]) - 1 if session["phase"] == "running" else len(session["messages"])
            session["messages"].insert(index, desired)
            kind = "channel_message"
        elif all(notice.get(key) == value for key, value in desired.items()):
            if record["projected_version"] < record["version"]:
                self.aibo.save()
                self.store.mark_projected(record["rule_id"], record["version"])
            return
        else:
            notice.update(desired)
            kind = "channel_message_updated"
        self.aibo._channel_event({**session, "root_id": source["thread_root_id"]}, kind, self.aibo.message_wire(desired))
        self.aibo.save()
        self.store.mark_projected(record["rule_id"], record["version"])

    def reconcile(self):
        for record in self.store.all():
            self.project(record)

    async def propose(self, arguments, session_id):
        if not isinstance(arguments, dict) or set(arguments) - {"rule_kind", "tool_names", "replace_rule_id", "expect_updated_at"}:
            raise RuleError("invalid_argument", "Propose only a rule kind and exact tool names; confirmation is a user action")
        names = self._scope(arguments.get("tool_names"))
        kind = arguments.get("rule_kind")
        self._kind(kind, names)
        session = self.aibo.state["sessions"].get(session_id)
        user = next((item for item in session["messages"] if item["id"] == session.get("user_id")), None) if session else None
        if (session is None or session["phase"] != "running" or user is None
                or user.get("author_account_id") != ACCOUNT_ID or user.get("server_notice")):
            raise RuleError("invalid_source", "Propose a rule from the user's current chat request, not a background task", 409)
        if self.engine is None:
            raise RuleError("unavailable", "Rule proposals require the model engine", 503)
        tool = self.engine.native_tools.get(session_id, {})
        if tool.get("name") != PROPOSE_TOOL:
            raise RuleError("invalid_source", "Rule proposals require the registered proposal tool", 409)
        tool_id = _text(tool.get("id"), "tool call ID")
        replace = arguments.get("replace_rule_id")
        expected = arguments.get("expect_updated_at")
        if replace is not None:
            _text(replace, "rule ID")
            _text(expected, "expected version")
        elif expected is not None:
            raise RuleError("invalid_argument", "Replacement ID is required with an expected version")
        source = {"session_id": session_id, "channel_id": session["channel_id"], "thread_root_id": user["thread_root_id"], "message_id": user["id"]}
        record = self.store.propose(names, kind, source, tool_id, replace=replace, expect=expected)
        self.project(record)
        return {"rule_id": record["rule_id"], "state": record["state"], "rule_kind": record["rule_kind"],
                "words": record["words"], "scope_text": SCOPE_TEXT, "card_message_id": record["card_message_id"],
                "updated_at": record["updated_at"], "confirmation_required": record["state"] == "pending"}

    def decision(self, tool_name):
        active = [rule for rule in self.store.all() if rule["state"] == "active" and tool_name in rule["tool_names"]]
        fingerprint = tuple(sorted((rule["rule_id"], rule["updated_at"]) for rule in active))
        ids = tuple(rule["rule_id"] for rule in active)
        kinds = {rule["rule_kind"] for rule in active}
        if "never" in kinds:
            kind = "never"
        elif "ask_first" in kinds or ("dont_ask" in kinds and not self.dont_ask_allowed([tool_name])):
            kind = "ask_first"
        else:
            kind = "dont_ask" if "dont_ask" in kinds else "default"
        return Decision(kind, fingerprint, ids)

    def prompt(self):
        active = [self.listed(rule) for rule in self.store.all() if rule["state"] == "active"]
        policy = [{key: rule[key] for key in ("rule_id", "words", "rule_kind", "updated_at")} for rule in active]
        return ("ACCOUNT STANDING RULES\nThese user-confirmed rules govern exact tool calls, not all equivalent real-world effects. "
                "The server enforces them and rechecks changes before dispatch. Never claim a pending proposal is active. "
                "For a request to add or reword a rule, use propose_standing_rule with exact registered tool names. "
                "If the user's natural-language scope cannot be represented, explain the limitation and offer an exact, reviewable scope; "
                "do not claim to prohibit broad outcomes. Only the user's native rule-card confirmation can activate a proposal. "
                "Dont-ask is available only for native read_file and list_files; other tools still require their applicable approval. "
                "To remove a rule, direct the user to its native rule card or Rules settings.\n" + json.dumps(policy, ensure_ascii=False))

    async def _body(self, request):
        data = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            data.extend(chunk)
            if len(data) > 16384:
                raise RuleError("invalid_argument", "Rule request is too large", 413)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise RuleError("invalid_argument", "Rule request must be a JSON object") from None
        if not isinstance(value, dict):
            raise RuleError("invalid_argument", "Rule request must be a JSON object")
        return value

    async def list_rules(self, request):
        self.reconcile()
        return {"rules": [self.listed(rule) for rule in self.store.all() if rule["state"] != "removed"]}

    async def change(self, request, operation):
        body = await self._body(request)
        permitted = {"rule_id", "expect_updated_at", "rule_kind"} if operation == "update" else {"rule_id", "expect_updated_at", "card_message_id"}
        if set(body) - permitted:
            raise RuleError("invalid_argument", "Unknown rule request field")
        rule_id = _text(body.get("rule_id"), "rule ID")
        expected = _text(body.get("expect_updated_at"), "expected version")
        card_id = body.get("card_message_id")
        if card_id is not None:
            _text(card_id, "card message ID")
        if operation == "confirm" and card_id is None:
            raise RuleError("invalid_argument", "Native card ID is required to confirm a rule")
        previous = self.store.get(rule_id)
        if operation == "update":
            if previous is not None:
                self._kind(body.get("rule_kind"), previous["tool_names"])
            elif body.get("rule_kind") not in KINDS:
                raise RuleError("invalid_argument", "Unknown standing-rule kind")
        if operation == "confirm" and previous is not None and previous["state"] == "pending":
            self._scope(previous["tool_names"])
            self._kind(previous["rule_kind"], previous["tool_names"])
            source = previous["source"]
            if source["session_id"] not in self.aibo.state["sessions"] or not self.aibo.home.channel(source["channel_id"]):
                raise RuleError("card_gone", "The proposal's chat was deleted; propose it again", 409)
        outcome, record = self.store.mutate(operation, rule_id, expected, kind=body.get("rule_kind"), card_id=card_id)
        if record is not None:
            self.reconcile()
        if card_id is not None:
            if record is None:
                raise RuleError("not_found", "Unknown standing-rule card", 404)
            return {"outcome": outcome, "card": self.aibo.message_wire(self.card(record))}
        if operation == "update":
            return {"outcome": "conflict" if outcome == "gone" else outcome,
                    **({"rule": self.listed(record)} if record is not None and record["state"] != "removed" else {})}
        return {"outcome": outcome}

    def handler(self, operation):
        async def handle(request):
            try:
                value = await self.list_rules(request) if operation == "list" else await self.change(request, operation)
                return web.json_response(value, headers={"Cache-Control": "no-store"})
            except RuleError as error:
                return web.json_response({"error": {"code": error.code, "message": error.message}}, status=error.status, headers={"Cache-Control": "no-store"})
            except (sqlite3.Error, OSError):
                return web.json_response({"error": {"code": "unavailable", "message": "Rule storage or card projection is unavailable; retry"}}, status=503, headers={"Cache-Control": "no-store"})
        return handle


def register_rules(app, data_dir, aibo, engine=None):
    manager = RulesManager(RulesStore(data_dir), aibo, engine)
    app[RULES] = manager
    if engine is not None:
        engine.rules = manager
        # fhd -> yuf.c -> grh.f enables the retained Rules settings row.
        aibo.home.capabilities = sorted(set(aibo.home.capabilities) | {"standing_rules_v1"})
        definition = {"name": PROPOSE_TOOL,
                      "description": "Propose an account-wide standing rule for exact registered tool calls. Creates a native confirmation card only; NEVER activates permissions. Explain any difference from the user's natural-language request. Kinds: never blocks the selected calls; ask_first requests approval every time; dont_ask is only valid for native read_file/list_files. Optional replacement requires the existing rule ID and updated_at from active rules. Do not use for unrepresentable real-world effects or background-task instructions.",
                      "parameters": {"type": "object", "properties": {
                          "rule_kind": {"type": "string", "enum": list(KINDS)},
                          "tool_names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 16},
                          "replace_rule_id": {"type": "string"}, "expect_updated_at": {"type": "string"}},
                          "required": ["rule_kind", "tool_names"], "additionalProperties": False}}
        if PROPOSE_TOOL in engine.extensions:
            raise ValueError("Standing-rule proposal tool already registered")
        async def propose(arguments, session_id):
            try:
                return await manager.propose(arguments, session_id)
            except RuleError as error:
                # Scope/confirmation limits are actionable model feedback, not
                # a vague successful result or a generic hidden tool failure.
                return {"error": error.message, "code": error.code, "created": False}
        engine.extensions[PROPOSE_TOOL] = (definition, propose)
    for prefix in ("", "/api"):
        base = prefix + "/v1/code/aibo/standing-rules"
        app.router.add_get(base, manager.handler("list"))
        for operation in ("update", "remove", "confirm"):
            app.router.add_post(base + ":" + operation, manager.handler(operation))
    async def start(_):
        manager.reconcile()
    async def close(_):
        manager.store.close()
    app.on_startup.append(start)
    app.on_cleanup.append(close)
    return manager
