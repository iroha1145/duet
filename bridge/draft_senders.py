"""Native draft Send actions bound to reviewed, installed MCP write operations.

Only fields displayed by the retained draft card become remote arguments. The
explicit Send action is the approval for that exact request; standing Never and
connector blocks still win, including after OAuth refresh or a transport lock.
"""
import copy
import hashlib
import json
import re
from urllib.parse import urlsplit

from .draft_cards import DraftSendFailed, PRODUCE_TOOL
from .connectors import MCPError


LIST_SENDERS = "list_message_draft_senders"
_REPOSITORY = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,100})(?:#([1-9][0-9]{0,9}))?\Z")
_OPERATIONS = {"issue_write": "issue", "add_issue_comment": "comment"}


def _arguments(operation, payload):
    match = _REPOSITORY.fullmatch(payload.get("to", ""))
    if not match or match[2] in (".", "..") or (operation == "comment") != bool(match[3]):
        raise ValueError("Use owner/repository for a new issue or owner/repository#number for a comment")
    if payload.get("kind") != "other" or not isinstance(payload.get("body"), str) or not payload["body"].strip():
        raise ValueError("A GitHub draft requires kind other and a nonempty body")
    args = {"owner": match[1], "repo": match[2], "body": payload["body"]}
    if operation == "issue":
        subject = payload.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("A new issue requires a visible subject")
        args.update(method="create", title=subject)
    else:
        if payload.get("subject") not in (None, ""):
            raise ValueError("A comment has no subject field")
        args["issue_number"] = int(match[3])
    return args


def _supports_schema(operation, schema):
    """A reviewed operation is exposed only when its live required inputs fit."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "if", "dependentRequired")):
        return False
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list) or any(not isinstance(x, str) for x in required):
        return False
    types = {"owner": {"string"}, "repo": {"string"}, "body": {"string"}}
    types.update({"method": {"string"}, "title": {"string"}} if operation == "issue" else {"issue_number": {"integer", "number"}})
    if not set(required).issubset(types):
        return False
    for name, allowed in types.items():
        field = properties.get(name)
        if not isinstance(field, dict) or field.get("type") not in allowed:
            return False
    method = properties.get("method", {})
    return operation != "issue" or ("enum" not in method or "create" in method["enum"])


def _delivery_receipt(operation, arguments, result, tool_name, operation_id):
    """No-error MCP replies may be forms, not a completed external operation."""
    if not isinstance(result, dict):
        raise RuntimeError("External delivery has no confirmed receipt")
    value = result.get("structuredContent")
    if value is None:
        content = result.get("content", [])
        texts = [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        if len(texts) != 1 or not isinstance(texts[0], str):
            raise RuntimeError("External delivery has no structured receipt")
        try:
            value = json.loads(texts[0])
        except ValueError:
            raise RuntimeError("External delivery has no structured receipt") from None
    if not isinstance(value, dict):
        raise RuntimeError("External delivery has no structured receipt")
    awaiting = value.get("awaiting", value)
    awaiting_shape = (set(value) <= {"method", "awaiting"} and value.get("method") == "create"
                      if "awaiting" in value else set(value) <= {"status", "reason"})
    # The official server intentionally labels this no-operation form result
    # isError=true. It is distinct from an error after an uncertain write.
    if (operation == "issue" and awaiting_shape and isinstance(awaiting, dict)
            and set(awaiting) <= {"status", "reason"} and awaiting.get("status") == "awaiting_user_submission"):
        raise DraftSendFailed("The connector requested another form and did not send the draft")
    if result.get("isError") is True:
        raise RuntimeError("External delivery has no confirmed receipt")
    if operation == "issue" and "issue" in value:
        if value.get("method") != "create":
            raise RuntimeError("External delivery operation differs from the draft")
        value = value["issue"]
    if not isinstance(value, dict):
        raise RuntimeError("External delivery receipt is missing")
    remote_id = value.get("id")
    if type(remote_id) is int:
        remote_id = str(remote_id)
    if not isinstance(remote_id, str) or not re.fullmatch(r"[1-9][0-9]{0,29}", remote_id):
        raise RuntimeError("External delivery receipt ID is missing")
    url = value.get("html_url", value.get("url"))
    if not isinstance(url, str) or len(url) > 2048:
        raise RuntimeError("External delivery receipt URL is missing")
    parsed = urlsplit(url)
    prefix = "/" + arguments["owner"] + "/" + arguments["repo"]
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query
            or not parsed.path.lower().startswith(prefix.lower() + "/")):
        raise RuntimeError("External delivery receipt target differs from the draft")
    tail = parsed.path[len(prefix):]
    if operation == "issue":
        valid = bool(re.fullmatch(r"/issues/[1-9][0-9]*", tail)) and not parsed.fragment
    else:
        valid = (tail in ("/issues/" + str(arguments["issue_number"]), "/pull/" + str(arguments["issue_number"]))
                 and parsed.fragment == "issuecomment-" + remote_id)
    if not valid:
        raise RuntimeError("External delivery receipt target differs from the draft")
    return {"sent": True, "receipt": {"tool": tool_name, "remote_id": remote_id,
                                      "url": url, "operation_id": operation_id}}


class DraftSenders:
    def __init__(self, cards, engine, manager):
        self.cards, self.engine, self.manager = cards, engine, manager
        self.bound = {}
        cards.refresh_senders = self.synchronize

    def _decision(self, name, extension):
        connector = self.manager.permission_decision(name, {name: extension})
        rule = self.engine.rules.decision(name) if self.engine.rules is not None else None
        if connector is None or connector.kind == "blocked" or (rule is not None and rule.kind == "never"):
            raise DraftSendFailed("The draft connector is unavailable or blocked")
        return connector, rule

    def synchronize(self):
        available = {}
        for name, extension in self.manager.snapshot_extensions().items():
            connection, version = extension[1].mcp_binding
            if connection.config.catalog_id != "github" or connection.config.readonly:
                continue
            remote = name.removeprefix("mcp__" + connection.config.id + "__")
            operation = _OPERATIONS.get(remote)
            if operation is None or not _supports_schema(operation, extension[0]["parameters"]):
                continue
            binding_id = hashlib.sha256(("github-draft-v1:" + version).encode()).hexdigest()
            previous = self.bound.get(name)
            if previous is not None and previous["connection"] is connection and previous["binding_id"] == binding_id:
                available[name] = previous
                continue
            account_label = "GitHub / " + connection.config.id

            def current(ext=extension, key=name):
                try:
                    self._decision(key, ext)
                    return True
                except DraftSendFailed:
                    return False

            def accepts(value, session, op=operation, account=account_label, key=name, ext=extension):
                if value.get("from_account") != account:
                    return False
                if self.engine.active_session.get() == session["id"]:
                    snapshot = self.engine.extension_snapshot.get()
                    bound = getattr(snapshot.get(key, (None, None))[1], "mcp_binding", None) if snapshot is not None else None
                    if bound != ext[1].mcp_binding:
                        return False
                try:
                    for variant in value["variants"]:
                        _arguments(op, {**value, **variant})
                except (KeyError, TypeError, ValueError):
                    return False
                return True

            async def send(payload, operation_id, recheck, ext=extension, key=name,
                           op=operation, account=account_label):
                if payload.get("from_account") != account:
                    raise DraftSendFailed("The draft account differs from its installed connector")
                try:
                    arguments = _arguments(op, payload)
                    recheck()
                except Exception as error:
                    raise DraftSendFailed("The draft changed before delivery") from error
                connector, rule = self._decision(key, ext)
                context = self.cards.resolve_operation(operation_id)
                if context["sender_id"] != key:
                    raise DraftSendFailed("The draft operation belongs to another connector")

                def authorize():
                    try:
                        recheck()
                    except Exception as error:
                        raise DraftSendFailed("Draft authorization was revoked before dispatch") from error
                    if self._decision(key, ext) != (connector, rule):
                        raise DraftSendFailed("Draft permissions changed before dispatch")
                    return True

                grant = self.manager.permission_grant.set(connector.fingerprint)
                guard = self.manager.dispatch_guard.set(authorize)
                try:
                    authorize()
                    result = await ext[1](copy.deepcopy(arguments), context["session_id"])
                except MCPError as error:
                    if error.code in ("connector_permission_required", "connector_authorization_changed", "connector_changed_start_new_turn"):
                        raise DraftSendFailed("The connector changed before dispatch") from error
                    raise
                finally:
                    self.manager.permission_grant.reset(grant)
                    self.manager.dispatch_guard.reset(guard)
                return _delivery_receipt(op, arguments, result, key, operation_id)

            self.cards.register_sender(name, send, current=current, accepts=accepts, binding_id=binding_id)
            available[name] = {"connection": connection, "binding_id": binding_id,
                               "public": {"sender_id": name, "kind": "other", "from_account": account_label,
                                          "to_format": "owner/repository" if operation == "issue" else "owner/repository#number",
                                          "subject_required": operation == "issue", "operation": "create_issue" if operation == "issue" else "add_comment"}}
        for name in self.bound.keys() - available.keys():
            self.cards.senders.pop(name, None)
        self.bound = available

    async def list_senders(self, arguments, session_id):
        if arguments != {}:
            raise ValueError("Draft sender listing takes no arguments")
        session = self.cards.aibo.session(session_id)
        if self.engine.private_session(session_id) or self.cards.aibo.is_private_run(session):
            raise ValueError("Draft senders are not exposed to a private planning run")
        self.synchronize()
        snapshot = self.engine.extension_snapshot.get()
        return {"senders": [copy.deepcopy(value["public"]) for name, value in sorted(self.bound.items())
                            if self.cards.senders[name]["current"]()
                            and (snapshot is None or getattr(snapshot.get(name, (None, None))[1], "mcp_binding", (None,))[0] is value["connection"])]}


def register_draft_senders(aibo, tasks, engine, manager):
    if engine is None:
        return None
    senders = DraftSenders(aibo.draft_cards, engine, manager)
    engine.draft_senders = senders
    aibo.draft_cards.install(engine)
    engine.extensions[LIST_SENDERS] = ({"name": LIST_SENDERS,
        "description": "List configured, permitted message-draft senders and their exact recipient/account fields. No network call or sending. Use a returned sender_id with create_message_draft; that creates only a draft for the user to edit and send explicitly.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}, senders.list_senders)
    if hasattr(engine, "plan_read_only"):
        engine.plan_read_only[LIST_SENDERS] = senders.list_senders
    engine.proposal_tools.update((LIST_SENDERS, PRODUCE_TOOL))
    tasks.home.capabilities = sorted(set(tasks.home.capabilities) | {
        "card_draft_edits", "card_draft_edit_words", "card_draft_edits_reply", "card_draft_edit_words_reply_v2",
        "tasks_lists", "tasks_card", "tasks_chat_stack", "tasks_waiting_draft",
        "tasks_chat_stack_approve", "tasks_chat_stack_approve_send",
    })

    def task_identity(session):
        found = tasks.record_for_session(session["id"])
        if found is None or session["phase"] in ("stopped", "interrupted", "error"):
            return None
        cid, tid, record = found
        data = record["data"]
        if cid != session["channel_id"] or data.get("done") or data.get("paused"):
            return None
        return tid

    aibo.draft_cards.task_identity = task_identity
    aibo.draft_cards.source_current = lambda session: not session.get("task_id") or task_identity(session) is not None
    aibo.draft_cards.task_binding = aibo.draft_cards.resolve_task_binding
    return senders
