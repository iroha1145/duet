"""Original message search over completed messages in the owning Aibo store.

Wire evidence: i1b/xv60/aw60, v1b/k1b and mih in the supplied Android source.
Search has no separate message database or model request.
"""
import re

from .aibo import AiboError
from .rest_home import object_body, response, error


def excerpt(text, query):
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    first = pattern.search(text)
    if first is None:
        return None
    start = max(0, first.start() - 80)
    end = min(len(text), max(start + 400, first.end()))
    snippet = text[start:end]
    highlights = []
    for match in pattern.finditer(snippet):
        # Kotlin String offsets count UTF-16 code units, including emoji pairs.
        highlights.append({"start": len(snippet[:match.start()].encode("utf-16-le")) // 2,
                           "end": len(snippet[:match.end()].encode("utf-16-le")) // 2})
    return snippet, highlights


class MessageSearch:
    def __init__(self, home, aibo, tasks):
        self.home, self.aibo, self.tasks = home, aibo, tasks

    async def search(self, request):
        cid = request.match_info["cid"]
        if self.home.channel(cid) is None:
            raise AiboError("not_found", "Unknown local channel", 404)
        body = await object_body(request)
        across = "/code/aibo/" in request.path
        expected = {"query", "all_channels"} if across else {"query"}
        if body.keys() != expected:
            raise AiboError("invalid_argument", "Invalid message search fields")
        query = body["query"]
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000
                or any(0xD800 <= ord(char) <= 0xDFFF for char in query)):
            raise AiboError("invalid_argument", "A search query of 1..1000 characters is required")
        if across and type(body["all_channels"]) is not bool:
            raise AiboError("invalid_argument", "all_channels must be boolean")
        channels, skipped = {cid}, 0
        if across and body["all_channels"]:
            channels = {c["id"] for c in self.home.state["channels"].values()
                        if not c.get("archived_at") and not c.get("disabled_at")}
            skipped = len(self.home.state["channels"]) - len(channels)
        hits = []
        scanned = []
        for session in self.aibo.state["sessions"].values():
            if session["channel_id"] not in channels:
                continue
            task = self.tasks.record_for_session(session["id"]) if self.tasks else None
            root_messages = {m["id"]: m for m in session["messages"]}
            for message in session["messages"]:
                # Hidden task bootstrap notices and internal tool payloads are
                # not user-authored/searchable conversation text.
                if not message.get("_complete") or message.get("server_notice"):
                    continue
                text = message.get("body")
                if not isinstance(text, str):
                    continue
                scanned.append(message["created_at"])
                found = excerpt(text, query.strip())
                if found is None:
                    continue
                root = message.get("thread_root_id") or message["id"]
                hit = {"id": message["id"], "channel_id": session["channel_id"],
                       "thread_root_id": root, "created_at": message["created_at"],
                       "in_timeline": message.get("in_timeline", True),
                       "thread_root_in_timeline": root_messages.get(root, {}).get("in_timeline", True),
                       "thread_cursor": "around:" + message["id"],
                       "snippet": found[0], "snippet_highlights": found[1]}
                if hit["in_timeline"]:
                    hit["timeline_cursor"] = "around:" + message["id"]
                for key in ("client_message_id", "author_account_id", "author_session_id", "transport", "author_routine_id"):
                    if message.get(key) is not None:
                        hit[key] = message[key]
                if task is not None:
                    hit.update(task_id=task[1], task_title=task[2]["data"].get("title", session["title"]),
                               author_session_bound_to_thread=True, thread_bound_session_ids=[session["id"]])
                elif session.get("trigger_id"):
                    hit.update(task_id=session["trigger_id"], task_title=session["title"],
                               author_session_routine_fired=True, author_session_bound_to_thread=True,
                               thread_bound_session_ids=[session["id"]])
                hits.append(hit)
        hits.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return response({"data": hits[:100], "next_cursor": None, "truncated": len(hits) > 100,
                         "scanned_through": min(scanned) if scanned else None,
                         "channels_searched": len(channels), "channels_skipped": skipped})

    async def handle(self, request):
        try:
            return await self.search(request)
        except AiboError as exc:
            return error(exc.code, exc.message, exc.status)


def register_search(app, home, aibo, tasks=None):
    search = MessageSearch(home, aibo, tasks)
    # fhd.smali maps these two declared contracts into the original search UI.
    home.capabilities = sorted(set(home.capabilities) | {"message_search", "messages_search_all_channels"})
    for prefix in ("", "/api"):
        for scope in ("/v1/code", "/v1/code/aibo"):
            app.router.add_post(prefix + scope + "/channels/{cid}/messages:search", search.handle)
    return search
