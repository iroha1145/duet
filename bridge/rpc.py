"""Local ConversationService compatibility layer; no vendor network calls."""
import asyncio
import json
import os
import time
from pathlib import Path

from aiohttp import web

from .wire import MAX_MESSAGE, FrameDecoder, blob, frame, gunzip, integer, parse, raw, text, trailers

PREFIX = "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService"


class FixtureBackend:
    """Explicit protocol fixture, never presented as a model response."""
    async def handle_message(self, conversation_id, user_id, assistant_id, text, emit):
        for part in ("duet 本地协议测试：", "这是一条固定测试响应，尚未连接 AI 模型。"):
            await emit(part)
            await asyncio.sleep(0)


class RpcError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def timestamp(value):
    seconds = int(value)
    return integer(1, seconds) + integer(2, int((value - seconds) * 1_000_000_000))


class ConversationBridge:
    def __init__(self, backend=None, store_path=None, stream_idle_timeout=60):
        self.backend = backend or FixtureBackend()
        self.store_path = Path(store_path) if store_path else None
        self.conversations = {}
        self.tasks = {}
        self.subscribers = {}
        self.stream_idle_timeout = stream_idle_timeout
        if self.store_path and self.store_path.exists():
            self.conversations = json.loads(self.store_path.read_text(encoding="utf-8"))["conversations"]
            for conversation in self.conversations.values():
                if conversation["status"] == "running":
                    conversation["status"] = "interrupted"
                    conversation["version"] += 1
                    for item in conversation["messages"]:
                        if not item["complete"]:
                            item.update(complete=True, stopped=True)
            self._persist()

    def _persist(self):
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"conversations": self.conversations}, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.store_path)

    def conversation(self, ident):
        if not ident or len(ident) > 256:
            raise RpcError("invalid_argument", "conversation_id required")
        if ident not in self.conversations:
            now = time.time()
            self.conversations[ident] = dict(id=ident, title="本地对话", created=now, updated=now,
                status="idle", version=1, messages=[], mutations={})
            self._persist()
        return self.conversations[ident]

    def entity(self, c):
        status = {"running": 2, "error": 5, "interrupted": 5}.get(c["status"], 1)
        result = blob(1, c["id"]) + blob(2, c["title"]) + integer(3, status)
        result += blob(4, timestamp(c["created"])) + blob(5, timestamp(c["updated"]))
        if c["messages"]:
            result += blob(7, c["messages"][-1]["id"])
        return result

    def snapshot(self, c):
        result = integer(1, 1) + blob(2, self.entity(c))
        for index, item in enumerate(c["messages"]):
            ident = item["id"]
            group, block = ident + ":group", ident + ":text"
            complete = item["complete"]
            stop = 10 if item.get("stopped") else (11 if item.get("error") else (1 if complete else 0))
            message = blob(1, ident) + blob(2, c["id"]) + integer(3, item["role"]) + integer(4, index)
            message += integer(6, complete) + integer(7, stop) + blob(9, timestamp(item["created"]))
            if item.get("parent"):
                message += blob(11, item["parent"])
            display = blob(1, group) + blob(2, ident) + integer(3, 0) + integer(4, 1) + integer(6, complete)
            state = 5 if item.get("stopped") else (3 if item.get("error") else (2 if complete else 1))
            content = blob(1, block) + blob(2, group) + integer(3, 0) + integer(4, complete)
            content += integer(8, state) + blob(9, item["text"]) + blob(10, integer(1, 2))
            result += blob(3, message) + blob(4, display) + blob(5, content)
        return result

    def _event(self, c, update=None, delta=None):
        event = blob(1, self.snapshot(c) if update is None else update) if delta is None else blob(2, delta)
        return blob(1, event + blob(10, integer(1, c["version"])))

    def _publish(self, c, delta=None):
        c["version"] += 1
        c["updated"] = time.time()
        # Text is also held in the canonical message. Bound crash-checkpoint
        # writes instead of fsyncing every token; transitions always flush.
        now = time.monotonic()
        if delta is None or now - getattr(self, "_last_checkpoint", 0) >= .25:
            self._persist()
            self._last_checkpoint = now
        payload = self._event(c, delta=delta)
        for queue in tuple(self.subscribers.get(c["id"], ())):
            if queue.full():
                # Slow clients receive an authoritative snapshot, never lose a delta silently.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self._event(c))
            else:
                queue.put_nowait(payload)

    async def _run(self, c, user, assistant):
        async def emit(delta):
            if not isinstance(delta, str) or len(assistant["text"]) + len(delta) > 1_000_000:
                raise ValueError("invalid backend text delta")
            assistant["text"] += delta
            self._publish(c, blob(1, assistant["id"] + ":text") + blob(2, delta))
        try:
            await self.backend.handle_message(c["id"], user["id"], assistant["id"], user["text"], emit)
            c["status"] = "idle"
        except asyncio.CancelledError:
            c["status"] = "stopped"
            assistant["stopped"] = True
            raise
        except Exception:
            c["status"] = "error"
            assistant["error"] = True
            assistant["text"] += "\n\n本地后端执行失败。"
        finally:
            assistant["complete"] = True
            self._publish(c)
            self.tasks.pop(c["id"], None)

    async def perform(self, data):
        header = parse(raw(data, 1))
        c = self.conversation(text(header, 2))
        mutation = raw(header, 1)
        parsed_mutation = parse(mutation)
        if not text(parsed_mutation, 1) or not isinstance(parsed_mutation.get(2, [0])[-1], int):
            raise RpcError("invalid_argument", "mutation_id required")
        key = json.dumps([text(parsed_mutation, 1), parsed_mutation.get(2, [0])[-1]])
        ack = blob(1, blob(1, mutation) + integer(2, 1))
        if key in c["mutations"]:
            return bytes.fromhex(c["mutations"][key])
        actions = [tag for tag in data if tag not in (1, 100)]
        if len(actions) != 1 or actions[0] not in (2, 3):
            raise RpcError("unimplemented", "Only send_message and stop_generation are implemented", 501)
        if actions[0] == 3:
            stopping_id = text(parse(raw(data, 3)), 1)
            active_id = next((item["id"] for item in reversed(c["messages"]) if not item["complete"]), None)
            if stopping_id and active_id and stopping_id != active_id:
                raise RpcError("failed_precondition", "Stop target is not the active assistant message", 409)
            task = self.tasks.get(c["id"])
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self.tasks.get(c["id"]) is task:
                    self.tasks.pop(c["id"], None)
            # A task cancelled before its first instruction cannot execute its finally.
            if c["status"] == "running":
                c["status"] = "stopped"
                for item in c["messages"]:
                    if not item["complete"]:
                        item.update(complete=True, stopped=True)
                self._publish(c)
        else:
            if c["id"] in self.tasks:
                raise RpcError("failed_precondition", "Conversation already running", 409)
            send = parse(raw(data, 2))
            user_id, assistant_id, prompt = text(send, 1), text(send, 2), text(send, 3)
            if not user_id or not assistant_id or user_id == assistant_id or len(user_id) > 256 or len(assistant_id) > 256 or not prompt.strip() or len(prompt) > 200000:
                raise RpcError("invalid_argument", "message IDs and nonempty text required")
            existing = {item["id"] for item in c["messages"]}
            if user_id in existing or assistant_id in existing:
                raise RpcError("already_exists", "Message ID already exists", 409)
            if send.get(4) or send.get(15):
                raise RpcError("unimplemented", "Attachments are not implemented in this protocol bridge", 501)
            now = time.time()
            user = dict(id=user_id, role=1, text=prompt, complete=True, parent=text(send, 5), created=now)
            assistant = dict(id=assistant_id, role=2, text="", complete=False, parent=user_id, created=now)
            c["messages"].extend((user, assistant))
            c["title"] = c["messages"][0]["text"][:80]
            c["status"] = "running"
            c["mutations"][key] = ack.hex()
            self._publish(c)
            self.tasks[c["id"]] = asyncio.create_task(self._run(c, user, assistant))
            return ack
        c["mutations"][key] = ack.hex()
        self._persist()
        return ack

    async def _body(self, request, streaming=False):
        encoding = request.headers.get("Content-Encoding", "identity").lower()
        if encoding not in ("identity", "gzip"):
            raise RpcError("unimplemented", "Unsupported HTTP compression", 415)
        body = await request.read()
        # aiohttp normally decompresses HTTP gzip before request.read(). This also
        # supports applications explicitly configured with auto_decompress=False.
        if encoding == "gzip" and body.startswith(b"\x1f\x8b"):
            body = gunzip(body)
        if len(body) > MAX_MESSAGE:
            raise ValueError("request too large")
        if streaming:
            decoder = FrameDecoder(request.headers.get("Connect-Content-Encoding", "identity"))
            envelopes = decoder.feed(body)
            decoder.finish()
            if len(envelopes) != 1 or envelopes[0][0] == 2:
                raise ValueError("one request envelope required")
            body = envelopes[0][1]
        return parse(body)

    async def handle(self, request):
        method = request.match_info["method"]
        streaming = method == "StreamTimeline"
        try:
            expected = "application/connect+proto" if streaming else "application/proto"
            if request.content_type != expected:
                raise RpcError("invalid_argument", "Expected " + expected, 415)
            data = await self._body(request, streaming)
            if streaming:
                return await self.stream(request, data)
            if method == "PerformAction":
                output = await self.perform(data)
            elif method == "ReadConversationHistory":
                output = blob(1, self.snapshot(self.conversation(text(data, 1))))
            elif method == "ReportViewing":
                output = b""
            elif method == "GetNewConversationDefaults":
                output = blob(1, b"") + blob(2, b"")
            else:
                raise RpcError("unimplemented", "Unknown local RPC method", 501)
            return web.Response(body=output, content_type="application/proto", headers={"Connect-Protocol-Version": "1"})
        except RpcError as error:
            return web.json_response({"code": error.code, "message": error.message}, status=error.status)
        except (ValueError, UnicodeError):
            return web.json_response({"code": "invalid_argument", "message": "Malformed protocol request"}, status=400)

    async def stream(self, request, data):
        authorized = request.get("duet_authorized", lambda: True)
        if not authorized():
            raise RpcError("unauthenticated", "Device session is no longer authorized", 401)
        c = self.conversation(text(data, 1))
        queue = asyncio.Queue(maxsize=128)
        listeners = self.subscribers.setdefault(c["id"], set())
        listeners.add(queue)
        response = web.StreamResponse(headers={"Content-Type": "application/connect+proto", "Connect-Protocol-Version": "1", "Cache-Control": "no-store"})
        try:
            initial = self._event(c)
            await response.prepare(request)
            if not authorized():
                await response.write(trailers("unauthenticated", "Device session is no longer authorized"))
                await response.write_eof()
                return response
            await response.write(frame(initial))
            last_event = time.monotonic()
            while True:
                if not authorized():
                    await response.write(trailers("unauthenticated", "Device session is no longer authorized"))
                    await response.write_eof()
                    break
                remaining = self.stream_idle_timeout - (time.monotonic() - last_event)
                if remaining <= 0:
                    await response.write(trailers())
                    await response.write_eof()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if not authorized():
                    await response.write(trailers("unauthenticated", "Device session is no longer authorized"))
                    await response.write_eof()
                    break
                if item is None:
                    await response.write(trailers("unavailable", "Local bridge shutting down"))
                    await response.write_eof()
                    break
                await response.write(frame(item))
                last_event = time.monotonic()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            listeners.discard(queue)
            if not listeners:
                self.subscribers.pop(c["id"], None)
        return response

    async def close(self, app):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for listeners in self.subscribers.values():
            for queue in listeners:
                while queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)
        self._persist()

    def register(self, app, prefix=PREFIX):
        app.router.add_post(prefix + "/{method}", self.handle)
        app.on_shutdown.append(self.close)
        return self


def register_rpc(app, backend=None, store_path=None, **kwargs):
    return ConversationBridge(backend, store_path, **kwargs).register(app)
