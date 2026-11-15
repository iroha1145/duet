"""Classic Bell WebSocket voice adapter. All actions remain in the native relay.

The adapter owns only temporary voice conversations. It cannot access the Aibo
engine, create Aibo messages, make approval decisions, or execute tools. Tool
definitions arrive from the actual client; results return through its existing
authenticated HTTP callback. Speech and model settings remain server-owned.
"""
import array
import asyncio
import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import sys
import time
from uuid import UUID, uuid4

import aiohttp
from aiohttp import web, WSMsgType

from duet_server import providers
from .auth import AUTH_PRINCIPAL
from .speech import OpusAudio, SpeechFailure, RATES, finish_owned, limited_body
from .tts import TempoControl
from .voice_prompt import VOICE_PROMPT

TALK = web.AppKey("duet_talk", object)
MAX_WIRE = 262144
MAX_TEXT = 16000
MAX_UTTERANCE_SECONDS = 30
MAX_COMPRESSED = 2 * 1024 * 1024
MAX_HISTORY = 100000
MAX_TOOLS = 8
TOOL_SECONDS = 60
MODEL_SECONDS = 60
REGISTER_SECONDS = 15
PLAYBACK_SECONDS = 15
IDLE_SECONDS = 120
SESSION_SECONDS = 1800
RETAIN_SECONDS = 300
SUPPORTED_TOOLS = frozenset(("ask_claude_dm", "end_voice_conversation"))
TOOL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class TalkFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def uuid_text(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def bounded_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def definitions(raw):
    """Accept the original tools_register array of JSON objects, fail closed."""
    if not isinstance(raw, list) or len(raw) > 128:
        raise TalkFailure("invalid_tools")
    result = {}
    for item in raw:
        if not isinstance(item, dict):
            raise TalkFailure("invalid_tools")
        name = item.get("name")
        if not isinstance(name, str) or not TOOL_ID.fullmatch(name):
            raise TalkFailure("invalid_tools")
        if name not in SUPPORTED_TOOLS:
            continue
        if name in result:
            raise TalkFailure("duplicate_tool")
        schema = item.get("input_schema")
        description = item.get("description", "")
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or not isinstance(description, str) or len(description) > 16000):
            raise TalkFailure("invalid_tools")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)):
            raise TalkFailure("invalid_tools")
        # These are the two recovered native contracts. An arbitrary client tool
        # schema must not turn this voice adapter into a generic execution API.
        if name == "end_voice_conversation":
            if properties or required:
                raise TalkFailure("unsupported_tool_schema")
        elif properties:
            if (set(properties) != {"request"} or not isinstance(properties["request"], dict)
                    or properties["request"].get("type") != "string"
                    or set(required) - {"request"}):
                raise TalkFailure("unsupported_tool_schema")
        elif required:
            raise TalkFailure("unsupported_tool_schema")
        result[name] = {"name": name, "description": description,
                        "parameters": copy.deepcopy(schema)}
    return result


@dataclass
class Pending:
    name: str
    generation: int
    turn: int
    future: asyncio.Future
    digest: str | None = None
    expired: bool = False


@dataclass
class VoiceSession:
    ident: str
    owner: tuple
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    generation: int = 0
    capture_id: str = field(default_factory=lambda: str(uuid4()))
    turn: int = 0
    ws: object = None
    authorized: object = None
    job: object = None
    tts_job: object = None
    registered: bool = False
    tools: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)
    audio: object = None
    voiced: float = 0
    silence: float = 0
    utterance_started: float = 0
    paused_until: float = 0
    mode: str = "speech"
    speed: float = 1
    language: str | None = None
    rate: int = 16000
    channels: int = 1
    playing: bool = False
    playback: object = None
    tempo: object = None
    deleted: bool = False
    baseline: int = 0


class AudioSink:
    """Gate every paced TTS packet against the call's socket and turn generation."""
    def __init__(self, bridge, session, generation, turn):
        self.bridge, self.session = bridge, session
        self.generation, self.turn = generation, turn

    @property
    def closed(self):
        return not self.bridge.current(self.session, self.generation, self.turn)

    async def send_bytes(self, data):
        if self.closed or self.session.mode != "speech":
            raise asyncio.CancelledError()
        await self.session.ws.send_bytes(data)


class TalkBridge:
    def __init__(self, catalog, speech, tts):
        self.catalog, self.speech, self.tts = catalog, speech, tts
        self.sessions = {}
        self.tombstones = {}
        self.http = None
        self.closed = False

    def owner(self, request):
        principal = request.get(AUTH_PRINCIPAL)
        authorized = request.get("duet_authorized")
        if principal is None or authorized is None or not authorized():
            raise web.HTTPUnauthorized()
        return (principal.account_id, principal.organization_id, principal.device_id)

    def lookup(self, request):
        owner = self.owner(request)
        ident = request.match_info["conversation"]
        if not uuid_text(ident) or request.match_info["organization"] != owner[1]:
            raise web.HTTPNotFound()
        session = self.sessions.get(ident)
        if session is None or session.owner != owner or session.deleted:
            raise web.HTTPNotFound()
        return session

    def current(self, session, generation, turn=None):
        return (not self.closed and not session.deleted and session.generation == generation
                and (turn is None or session.turn == turn)
                and session.ws is not None and not session.ws.closed
                and session.authorized is not None and session.authorized())

    async def send(self, session, generation, kind, turn=None, **fields):
        if not self.current(session, generation, turn):
            raise asyncio.CancelledError()
        await session.ws.send_json({"type": kind, **fields})

    async def sse(self, session, generation, turn, kind, data):
        await self.send(session, generation, "message_sse", turn,
                        event={"type": kind, "data": bounded_json(data)})

    def expire(self, session):
        for pending in session.pending.values():
            if pending.digest is None:
                pending.expired = True
                if not pending.future.done():
                    pending.future.cancel()

    async def stop(self, session, *, announce=False):
        session.turn += 1
        self.expire(session)
        playing = session.playing
        session.playing = False
        if session.playback is not None:
            session.playback.set()
        jobs = [job for job in (session.job, session.tts_job)
                if job is not None and job is not asyncio.current_task()]
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await finish_owned(asyncio.create_task(self.join(jobs)))
        session.job = session.tts_job = None
        if announce and self.current(session, session.generation):
            await self.send(session, session.generation, "server_interrupt")
            if playing:
                await self.send(session, session.generation, "playback_end")
            await self.send(session, session.generation, "text_reply_end", turn_complete=True)

    @staticmethod
    async def join(jobs):
        await asyncio.gather(*jobs, return_exceptions=True)

    def discard_audio(self, session):
        if session.audio is not None:
            session.audio.close()
        session.audio = None
        session.voiced = session.silence = session.utterance_started = 0

    async def failure(self, session, generation, code, turn=None):
        if self.current(session, generation, turn):
            await self.send(session, generation, "error", turn,
                            error={"type": "api_error", "details": {"error_code": code}})

    def collect(self):
        now = time.monotonic()
        for ident, session in tuple(self.sessions.items()):
            if session.ws is None and now - session.touched > RETAIN_SECONDS:
                session.history.clear()
                self.sessions.pop(ident)
                self.tombstones[ident] = now
        for ident, timestamp in tuple(self.tombstones.items()):
            if now - timestamp > RETAIN_SECONDS:
                self.tombstones.pop(ident)
        while len(self.tombstones) > 512:
            self.tombstones.pop(next(iter(self.tombstones)))

    async def tool_result(self, request):
        session = self.lookup(request)
        try:
            raw = await limited_body(request.content, 1024 * 1024)
            body = json.loads(raw)
        except (ValueError, UnicodeError, SpeechFailure):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict) or set(body) != {"tool_use_id", "content", "is_error"}:
            raise web.HTTPBadRequest()
        ident, blocks, is_error = body["tool_use_id"], body["content"], body["is_error"]
        if (not isinstance(ident, str) or not TOOL_ID.fullmatch(ident)
                or type(is_error) is not bool or not isinstance(blocks, list) or len(blocks) != 1
                or not isinstance(blocks[0], dict) or set(blocks[0]) - {"type", "text", "uuid"}
                or blocks[0].get("type") != "text" or not isinstance(blocks[0].get("text"), str)
                or len(blocks[0]["text"]) > 200000):
            raise web.HTTPBadRequest()
        pending = session.pending.get(ident)
        if pending is None:
            raise web.HTTPNotFound()
        digest = hashlib.sha256(bounded_json([blocks[0]["text"], is_error]).encode()).hexdigest()
        if pending.digest is not None:
            if digest != pending.digest:
                raise web.HTTPConflict()
            return web.Response(status=204)
        if (pending.expired or pending.generation != session.generation or pending.turn != session.turn
                or not self.current(session, pending.generation, pending.turn)):
            raise web.HTTPGone()
        pending.digest = digest
        pending.future.set_result({"role": "tool", "tool_call_id": ident,
                                   "text": blocks[0]["text"], "is_error": is_error})
        session.touched = time.monotonic()
        return web.Response(status=204)

    async def delete(self, request):
        session = self.lookup(request)
        await self.stop(session)
        session.deleted = True
        self.discard_audio(session)
        session.history.clear()
        session.pending.clear()
        self.tombstones[session.ident] = time.monotonic()
        self.sessions.pop(session.ident, None)
        if session.ws is not None:
            await session.ws.close(code=1000)
        return web.Response(status=204)

    def parameters(self, request):
        for key in request.query:
            if len(request.query.getall(key)) != 1 or len(request.query[key]) > 512:
                raise TalkFailure("invalid_parameters")
        if request.query.get("input_encoding", "opus") != "opus":
            raise TalkFailure("unsupported_encoding")
        try:
            rate = int(request.query.get("input_sample_rate", "16000"))
            channels = int(request.query.get("input_channels", "1"))
            speed = float(request.query.get("tts_speed", "1"))
        except ValueError:
            raise TalkFailure("invalid_parameters")
        mode = request.query.get("output_mode", "speech")
        capabilities = request.query.get("capabilities", "").split(",")
        if (rate not in RATES or channels not in (1, 2) or not math.isfinite(speed)
                or not .25 <= speed <= 4 or mode not in ("speech", "text")):
            raise TalkFailure("invalid_parameters")
        if "webrtc_media" in capabilities or request.query.get("agent_path"):
            raise TalkFailure("media_unsupported")
        if request.query.get("output_format", "opus_48000_32") != "opus_48000_32":
            raise TalkFailure("unsupported_encoding")
        return rate, channels, speed, mode

    async def socket(self, request):
        owner = self.owner(request)
        ident = request.match_info["conversation"]
        if request.match_info["organization"] != owner[1] or not uuid_text(ident):
            raise web.HTTPNotFound()
        try:
            rate, channels, speed, mode = self.parameters(request)
            if (not self.speech.config.load()["enabled"] or not self.tts.config.load()["enabled"]
                    or self.tts.readiness() is not None):
                raise TalkFailure("not_configured")
            self.catalog.load()
        except (TalkFailure, SpeechFailure, ValueError, OSError, RuntimeError) as error:
            code = error.code if isinstance(error, (TalkFailure, SpeechFailure)) else "not_configured"
            return web.json_response({"error": code}, status=503)
        self.collect()
        if ident in self.tombstones:
            raise web.HTTPGone()
        session = self.sessions.get(ident)
        if session is not None and session.owner != owner:
            raise web.HTTPNotFound()
        active = [s for s in self.sessions.values() if s.ws is not None and not s.ws.closed and s is not session]
        if len(active) >= 8 or sum(s.owner == owner for s in active) >= 2:
            raise web.HTTPTooManyRequests()
        if session is None:
            if len(self.sessions) >= 64:
                raise web.HTTPTooManyRequests()
            session = VoiceSession(ident, owner)
            self.sessions[ident] = session
        # Claim the generation before awaiting cleanup; a concurrent reconnect
        # cannot let an old socket's finally block detach the new socket.
        session.generation += 1
        generation = session.generation
        previous = session.ws
        await self.stop(session)
        if previous is not None:
            await previous.close(code=1001, message=b"Voice connection replaced")
        if session.deleted or self.sessions.get(ident) is not session:
            raise web.HTTPGone()
        if session.generation != generation:
            raise web.HTTPConflict()
        ws = web.WebSocketResponse(max_msg_size=MAX_WIRE, heartbeat=15)
        await ws.prepare(request)
        # The upgrade itself yields. A reconnect or DELETE may have won while
        # this socket was being prepared; a stale upgrade owns only itself.
        if (self.closed or session.deleted or self.sessions.get(ident) is not session
                or session.generation != generation or session.owner != owner
                or not request["duet_authorized"]()):
            await ws.close(code=1001 if request["duet_authorized"]() else 4001)
            return ws
        session.rate, session.channels, session.speed, session.mode = rate, channels, speed, mode
        session.capture_id = str(uuid4())
        session.authorized = request["duet_authorized"]
        session.registered = False
        session.tools = {}
        self.discard_audio(session)
        session.ws = ws
        session.touched = connected = time.monotonic()
        try:
            await self.send(session, generation, "session_server_initialized",
                            data={"session_id": ident})
            await self.send(session, generation, "audio_capture_started", voice_session_uuid=session.capture_id)
            await self.send(session, generation, "recording_started")
            while self.current(session, generation):
                try:
                    incoming = await ws.receive(timeout=.2)
                except asyncio.TimeoutError:
                    incoming = None
                if not self.current(session, generation):
                    break
                now = time.monotonic()
                if now - session.created > SESSION_SECONDS:
                    raise TalkFailure("session_limit")
                if session.audio is not None and now - session.utterance_started > MAX_UTTERANCE_SECONDS + 2:
                    raise TalkFailure("audio_limit")
                if not session.registered and now - connected > REGISTER_SECONDS:
                    raise TalkFailure("tools_not_registered")
                if now - session.touched > IDLE_SECONDS and (session.job is None or session.job.done()):
                    raise TalkFailure("idle_timeout")
                if incoming is None:
                    continue
                if incoming.type == WSMsgType.BINARY:
                    if not session.registered:
                        raise TalkFailure("tools_not_registered")
                    session.touched = now
                    await self.audio_packet(session, generation, incoming.data)
                elif incoming.type == WSMsgType.TEXT:
                    try:
                        command = json.loads(incoming.data)
                    except ValueError:
                        raise TalkFailure("invalid_message")
                    if not isinstance(command, dict) or not isinstance(command.get("type"), str):
                        raise TalkFailure("invalid_message")
                    session.touched = now
                    if not await self.command(session, generation, command):
                        break
                elif incoming.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except (TalkFailure, SpeechFailure) as error:
            await self.failure(session, generation, error.code)
        finally:
            if session.generation == generation:
                await self.stop(session)
                self.discard_audio(session)
                session.ws = None
                session.authorized = None
                session.touched = time.monotonic()
            await ws.close(code=1000 if request["duet_authorized"]() else 4001)
        return ws

    async def command(self, session, generation, command):
        kind = command["type"]
        data = command.get("data")
        if kind == "tools_register":
            if session.registered or not isinstance(data, dict) or set(data) != {"tools"}:
                raise TalkFailure("invalid_tools")
            session.tools = definitions(data["tools"])
            session.registered = True
        elif kind in ("interrupt", "client_abort_reason"):
            await self.stop(session, announce=True)
            self.discard_audio(session)
        elif kind == "manual_input_end":
            await self.finish_audio(session, generation)
        elif kind == "user_message":
            if not session.registered:
                raise TalkFailure("tools_not_registered")
            if not isinstance(data, dict) or data.get("files"):
                raise TalkFailure("unsupported_user_message")
            text = data.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise TalkFailure("invalid_text")
            await self.begin(session, generation, text=text.strip())
        elif kind == "close_stream":
            await self.stop(session)
            await self.send(session, generation, "audio_capture_stopped", voice_session_uuid=session.capture_id)
            return False
        elif kind == "pause_endpointing":
            timeout = 60000 if data is None else data.get("timeout_ms", 60000) if isinstance(data, dict) else None
            if type(timeout) is not int or not 0 <= timeout <= 60000:
                raise TalkFailure("invalid_parameters")
            session.paused_until = time.monotonic() + timeout / 1000
        elif kind == "unpause_endpointing":
            session.paused_until = 0
        elif kind == "playback_complete":
            if session.playback is not None:
                session.playback.set()
        elif kind == "clock_sync_ping":
            seq = command.get("seq")
            if type(seq) is not int or not 0 <= seq <= 2**53:
                raise TalkFailure("invalid_message")
            timestamp = int(time.time() * 1000)
            await self.send(session, generation, "clock_sync_pong", seq=seq,
                            server_t2_ms=timestamp, server_t3_ms=int(time.time() * 1000))
        elif kind == "output_mode_select":
            mode = data.get("mode") if isinstance(data, dict) else None
            if mode not in ("speech", "text"):
                raise TalkFailure("invalid_parameters")
            session.mode = mode
            if mode == "text" and session.tts_job is not None:
                session.tts_job.cancel()
            await self.send(session, generation, "voice_output_mode_changed", mode=mode, source="client")
        elif kind == "voice_select":
            if not isinstance(data, dict) or set(data) - {"voice", "speed", "language"}:
                raise TalkFailure("invalid_parameters")
            speed = data.get("speed", session.speed)
            if type(speed) not in (int, float) or not math.isfinite(speed) or not .25 <= speed <= 4:
                raise TalkFailure("invalid_parameters")
            if data.get("voice") is not None:
                raise TalkFailure("voice_selection_unsupported")
            language = data.get("language", session.language)
            if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language)):
                raise TalkFailure("invalid_parameters")
            session.speed, session.language = speed, language
            if session.tempo is not None:
                session.tempo.change(speed)
            await self.send(session, generation, "voice_audio_speed_changed", new_speed=speed, scope="session")
        elif kind in ("media_offer", "media_lost"):
            raise TalkFailure("media_unsupported")
        elif kind in ("keep_alive", "client_metrics", "attachment_flow_start", "attachment_flow_end"):
            pass
        else:
            raise TalkFailure("unsupported_message")
        return True

    async def audio_packet(self, session, generation, packet):
        if session.audio is None:
            session.audio = OpusAudio(session.rate, session.channels)
            session.utterance_started = time.monotonic()
        audio = session.audio
        before = len(audio.pcm)
        samples_before = audio.samples
        audio.append(packet)
        if audio.opus_bytes > MAX_COMPRESSED or audio.samples > session.rate * MAX_UTTERANCE_SECONDS:
            raise TalkFailure("audio_limit")
        samples = array.array("h", audio.pcm[before:])
        if sys.byteorder != "little":
            samples.byteswap()
        duration = (audio.samples - samples_before) / session.rate
        rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
        if rms >= 450:
            if session.voiced == 0 and session.job is not None and not session.job.done():
                await self.stop(session, announce=True)
            session.voiced += duration
            session.silence = 0
        else:
            session.silence += duration
        if (session.voiced >= .15 and session.silence >= .7
                and time.monotonic() >= session.paused_until):
            await self.finish_audio(session, generation)
        elif not session.voiced and audio.samples >= session.rate * 5:
            self.discard_audio(session)

    async def finish_audio(self, session, generation):
        if not session.registered:
            raise TalkFailure("tools_not_registered")
        if session.audio is None or not session.audio.samples:
            await self.send(session, generation, "transcript_empty")
            return
        audio = session.audio
        session.audio = None
        session.voiced = session.silence = session.utterance_started = 0
        try:
            wav = audio.wav()
        finally:
            audio.close()
        await self.begin(session, generation, audio=wav)

    async def begin(self, session, generation, *, text=None, audio=None):
        await self.stop(session, announce=session.job is not None and not session.job.done())
        while len(session.pending) > 256:
            session.pending.pop(next(iter(session.pending)))
        session.turn += 1
        turn = session.turn
        session.baseline = len(session.history)
        session.job = asyncio.create_task(self.run_turn(session, generation, turn, text, audio))

    async def speak(self, session, generation, turn, text, settings):
        if not text or session.mode != "speech":
            return
        if len(text) > 4096:
            raise TalkFailure("speech_text_limit")
        sink = AudioSink(self, session, generation, turn)
        if len(self.tts.active) >= 4:
            raise TalkFailure("tts_concurrent_limit")
        session.playback = asyncio.Event()
        session.tempo = TempoControl(session.speed)
        session.playing = True
        await self.send(session, generation, "playback_start", turn, turn_id=str(turn))
        self.tts.active.add(sink)
        session.tts_job = asyncio.create_task(self.tts.synthesize(
            sink, settings, text, session.tempo, "opus_48000_32", lambda: not sink.closed))
        try:
            try:
                await session.tts_job
            except asyncio.CancelledError:
                if session.mode != "text" or not self.current(session, generation, turn):
                    raise
        finally:
            self.tts.active.discard(sink)
            if self.current(session, generation, turn):
                session.playing = False
                await self.send(session, generation, "playback_end", turn)
        if session.mode == "speech":
            try:
                await asyncio.wait_for(session.playback.wait(), PLAYBACK_SECONDS)
            except asyncio.TimeoutError:
                raise TalkFailure("playback_timeout")
        session.tempo = None

    def validate_call(self, session, call):
        if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
            raise TalkFailure("invalid_model_tool")
        ident, name, args = call["id"], call["name"], call["arguments"]
        if (not isinstance(ident, str) or not TOOL_ID.fullmatch(ident) or ident in session.pending
                or name not in session.tools or not isinstance(args, dict)):
            raise TalkFailure("unregistered_model_tool")
        if len(session.pending) >= 512:
            raise TalkFailure("tool_receipt_limit")
        properties = session.tools[name]["parameters"].get("properties", {})
        required = session.tools[name]["parameters"].get("required", [])
        if set(args) - set(properties) or not set(required) <= set(args):
            raise TalkFailure("invalid_model_tool")
        if "request" in args and (not isinstance(args["request"], str) or len(args["request"]) > MAX_TEXT):
            raise TalkFailure("invalid_model_tool")

    async def model_reply(self, session, generation, turn, config, *, allow_empty=False):
        ident = str(uuid4())
        text, calls, index = "", [], 0
        await self.send(session, generation, "text_reply_start", turn)
        await self.sse(session, generation, turn, "message_start", {
            "type": "message_start", "message": {"id": ident, "uuid": ident,
            "type": "message", "role": "assistant", "content": [], "model": config["model"],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        text_open = False
        async with asyncio.timeout(MODEL_SECONDS):
            async for event in providers.stream(self.http, config, session.history,
                                                list(session.tools.values()) if config.get("allow_tools", True) else [],
                                                session_id=session.ident):
                if not self.current(session, generation, turn):
                    raise asyncio.CancelledError()
                if event["type"] == "text":
                    chunk = event["text"]
                    if not isinstance(chunk, str) or len(text) + len(chunk) > MAX_TEXT:
                        raise TalkFailure("model_text_limit")
                    if chunk:
                        if not text_open:
                            await self.sse(session, generation, turn, "content_block_start", {
                                "type": "content_block_start", "index": index,
                                "content_block": {"type": "text", "text": ""}})
                            text_open = True
                        text += chunk
                        await self.sse(session, generation, turn, "content_block_delta", {
                            "type": "content_block_delta", "index": index,
                            "delta": {"type": "text_delta", "text": chunk}})
                elif event["type"] == "tools":
                    calls.extend(event["tools"])
                    if len(calls) > MAX_TOOLS:
                        raise TalkFailure("model_tool_limit")
        if text_open:
            await self.sse(session, generation, turn, "content_block_stop", {"type": "content_block_stop", "index": index})
            index += 1
        # Validate the whole batch before publishing the first tool. The server
        # never executes a tool and never fabricates a successful tool result.
        seen = set()
        for call in calls:
            if not config.get("allow_tools", True):
                raise TalkFailure("model_tools_disabled")
            self.validate_call(session, call)
            if call["id"] in seen:
                raise TalkFailure("duplicate_model_tool")
            seen.add(call["id"])
        if not text and not calls and not allow_empty:
            raise TalkFailure("empty_model_reply")
        session.history.append({"role": "assistant", "text": text, "tool_calls": copy.deepcopy(calls)})
        for call in calls:
            session.pending[call["id"]] = Pending(call["name"], generation, turn,
                                                  asyncio.get_running_loop().create_future())
            await self.sse(session, generation, turn, "content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}}})
            await self.sse(session, generation, turn, "content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": bounded_json(call["arguments"])}})
            await self.sse(session, generation, turn, "content_block_stop", {"type": "content_block_stop", "index": index})
            index += 1
        await self.sse(session, generation, turn, "message_delta", {
            "type": "message_delta", "delta": {"stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0}})
        await self.sse(session, generation, turn, "message_stop", {"type": "message_stop"})
        await self.send(session, generation, "text_reply_end", turn, turn_complete=not calls)
        return text, calls

    async def run_turn(self, session, generation, turn, text, audio):
        baseline = session.baseline
        completed = False
        try:
            if audio is not None:
                await self.send(session, generation, "transcription_start", turn)
                settings = self.speech.config.load()
                if not settings["enabled"]:
                    raise TalkFailure("asr_not_configured")
                if self.speech.active >= 4:
                    raise TalkFailure("asr_concurrent_limit")
                self.speech.active += 1
                try:
                    text = await self.speech.transcribe(settings, audio, session.language)
                finally:
                    self.speech.active -= 1
            audio = None
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise TalkFailure("invalid_transcript")
            if not text.strip():
                await self.send(session, generation, "transcript_empty", turn)
                completed = True
                return
            await self.send(session, generation, "user_input_end", turn)
            await self.send(session, generation, "message_complete", turn, data={
                "message_uuid": str(uuid4()), "sender": "human", "content": [{"type": "text", "text": text}],
                "files": [], "attachments": []})
            config = copy.deepcopy(self.catalog.load())
            config["system_prompt"] = VOICE_PROMPT
            tts_settings = copy.deepcopy(self.tts.config.load())
            session.history.append({"role": "user", "text": text})
            allow_empty = False
            for _ in range(8):
                if len(bounded_json(session.history)) > MAX_HISTORY:
                    raise TalkFailure("context_limit")
                await self.send(session, generation, "thinking_status", turn, active=True)
                reply, calls = await self.model_reply(session, generation, turn, config, allow_empty=allow_empty)
                await self.send(session, generation, "thinking_status", turn, active=False)
                await self.speak(session, generation, turn, reply, tts_settings)
                if not calls:
                    completed = True
                    return
                try:
                    results = await asyncio.wait_for(asyncio.gather(*[
                        session.pending[call["id"]].future for call in calls]), TOOL_SECONDS)
                except asyncio.TimeoutError:
                    raise TalkFailure("tool_result_timeout")
                session.history.extend({**result, "text": "Tool reported failure: " + result["text"]}
                                       if result["is_error"] else result for result in results)
                allow_empty = any(call["name"] == "end_voice_conversation" for call in calls)
                # The native end handler can refuse with ordinary text while
                # still reporting is_error=false. Its close_stream/DELETE is
                # authoritative; HTTP acknowledgement alone cannot end a call.
            raise TalkFailure("tool_round_limit")
        except (TalkFailure, SpeechFailure) as error:
            await self.failure(session, generation, error.code, turn)
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, ValueError, OSError, KeyError, TypeError):
            await self.failure(session, generation, "voice_upstream_failed", turn)
        finally:
            if not completed:
                # A cancelled/failed tool batch cannot appear in a later model
                # request without its matching results.
                del session.history[baseline:]
                for pending in session.pending.values():
                    if pending.turn == turn and pending.digest is None:
                        pending.expired = True
                        if not pending.future.done():
                            pending.future.cancel()
            if self.current(session, generation, turn):
                await self.send(session, generation, "thinking_status", turn, active=False)


def register_talk(app, *, catalog, speech, tts):
    """Register after speech/tts and account authentication middleware.

    catalog is the existing server ModelCatalog. speech and tts are the existing
    SpeechBridge/TTSBridge instances, each owning its private configuration and
    HTTP lifecycle. This module deliberately receives no engine or Aibo object.
    """
    bridge = TalkBridge(catalog, speech, tts)
    app[TALK] = bridge

    async def lifecycle(app):
        async with aiohttp.ClientSession(trust_env=False) as http:
            bridge.http = http
            yield

    async def shutdown(app):
        bridge.closed = True
        for session in tuple(bridge.sessions.values()):
            await bridge.stop(session)
            bridge.discard_audio(session)
            session.history.clear()
            if session.ws is not None:
                await session.ws.close(code=1001)
        bridge.sessions.clear()

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    for prefix in ("", "/api"):
        path = prefix + "/organizations/{organization}/chat_conversations/{conversation}"
        app.router.add_post(path + "/tool_result", bridge.tool_result)
        app.router.add_delete(path, bridge.delete)
        app.router.add_get(prefix + "/ws/voice/organizations/{organization}/chat_conversations/{conversation}", bridge.socket)
    return bridge
