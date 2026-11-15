"""Duet's bounded default/plan compatibility contract, never a permission grant.

The retained APK consumes ExitPlanMode.input.plan and a session setMode(default)
approval. It does not contain the upstream model schemas or enforcement policy.
This module deliberately implements only that documented local contract.
"""
import copy

ENTER = "EnterPlanMode"
EXIT = "ExitPlanMode"
MODES = frozenset({"default", "plan"})
MAX_PLAN_BYTES = 128 * 1024
KEEP_PLANNING = "The user wants to keep planning and has not said what to change. Ask them what they would like changed."
EXIT_PERMISSIONS = [{"type": "setMode", "mode": "default", "destination": "session"}]


class PlanModeDenied(ValueError):
    pass


def validate(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Plan tool arguments must be an object")
    if name == ENTER:
        if arguments:
            raise ValueError("EnterPlanMode takes no arguments")
    elif name == EXIT:
        plan = arguments.get("plan")
        if (set(arguments) != {"plan"} or not isinstance(plan, str) or not plan.strip()
                or len(plan.encode("utf-8")) > MAX_PLAN_BYTES):
            raise ValueError("ExitPlanMode requires a nonempty plan up to 128 KiB")


def mode(session):
    value = session.get("permission_mode", "default")
    if value not in MODES:
        raise ValueError("Unsupported persisted permission mode")
    return value


def definitions():
    return [
        {"name": ENTER, "description": "Enter planning mode before exploring a proposed change. Only approved read-only tools and questions are available; no execution, writes, or background workers. This grants no permissions.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": EXIT, "description": "Submit your complete plan for the user's review. Approval returns to default tool approvals; it never authorizes execution or MCP calls. If declined, keep planning and ask what should change.",
         "parameters": {"type": "object", "properties": {"plan": {"type": "string", "minLength": 1}},
                        "required": ["plan"], "additionalProperties": False}},
    ]


def readonly(engine, name):
    """Use fixed handler identity/verified MCP metadata, never a name heuristic."""
    extension = engine.current_extensions().get(name)
    if extension is None:
        return name in {"read_file", "list_files"}
    handler = extension[1]
    # These registrations are local reviewed handlers, not provider metadata.
    if engine.plan_read_only.get(name) == handler:
        return True
    binding = getattr(handler, "mcp_binding", None)
    if not isinstance(binding, tuple) or len(binding) != 2:
        return False
    connection, version = binding
    if not connection.active:
        return False
    candidates = [item for item in connection.tools if connection.versions.get(item.get("name")) == version]
    if len(candidates) != 1:
        return False
    annotations = candidates[0].get("annotations", {})
    return (isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
            and annotations.get("destructiveHint") is not True)


def check(engine, session, tool):
    if mode(session) != "plan" or tool["name"] in {ENTER, EXIT, "AskUserQuestion"}:
        return
    if not readonly(engine, tool["name"]):
        raise PlanModeDenied("This tool is unavailable in plan mode. Submit the plan and obtain approval before execution, changes, or background work.")


def set_mode(engine, session, value):
    if value not in MODES:
        raise ValueError("Only default and plan permission modes are supported")
    previous = session.get("permission_mode", "default")
    session["permission_mode"] = value
    try:
        engine.save(session, "permission_mode")
    except BaseException:
        session["permission_mode"] = previous
        raise


def sdk_exit_answer(payload, pending):
    """Only Exit's exact session transition is accepted, never arbitrary updates."""
    if not pending or pending.get("tool") != EXIT:
        return False
    validate(EXIT, pending.get("arguments"))
    if payload.get("behavior") == "allow":
        return (payload.get("updatedInput") == {} and payload.get("updatedPermissions") == EXIT_PERMISSIONS
                and not (set(payload) - {"behavior", "toolUseID", "updatedInput", "updatedPermissions"})
                and ("toolUseID" not in payload or payload["toolUseID"] == pending["tool_call_id"]))
    return (payload.get("behavior") == "deny"
            and not (set(payload) - {"behavior", "toolUseID", "message", "interrupt"})
            and ("toolUseID" not in payload or payload["toolUseID"] == pending["tool_call_id"])
            and ("message" not in payload or payload["message"] == KEEP_PLANNING)
            and payload.get("interrupt", False) is False)


def exit_response(pending, allow):
    if allow:
        return {"behavior": "allow", "updatedInput": {}, "updatedPermissions": copy.deepcopy(EXIT_PERMISSIONS)}
    return {"behavior": "deny", "message": KEEP_PLANNING}


def display(pending):
    validate(EXIT, pending.get("arguments"))
    return {"kind": "plan", "title": "Review plan", "ask": "plan_then_manual",
            "rows": [{"label": "Plan", "value": pending["arguments"]["plan"], "style": "prose", "no_label": True}]}
