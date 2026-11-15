"""Original dictation WebSocket -> configured ASR, using real libopus decoding.

Linux dependency: libopus0 (Debian/Ubuntu). No audio, transcript or credential is
logged or saved. This implements dictation, not the conversation/WebRTC protocol.
The matching Android patch must give STT 30 seconds to finish after CloseStream;
the upstream deadline is at most 25 seconds, leaving time to deliver the result.
"""
import array
import asyncio
import ctypes
import ctypes.util
from functools import lru_cache
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
import wave

import aiohttp
from aiohttp import web

from .rest import ORGANIZATION_ID

MAX_PACKET = 16384
MAX_OPUS_BYTES = 8 * 1024 * 1024
MAX_PCM_BYTES = 24 * 1024 * 1024
MAX_SECONDS = 300
IDLE_SECONDS = 30
MAX_RESPONSE = 1024 * 1024
RATES = (8000, 12000, 16000, 24000, 48000)
SPEECH = web.AppKey("duet_speech", object)


async def limited_body(content, limit):
    chunks, size = [], 0
    while True:
        chunk = await content.read(min(65536, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError("Body limit exceeded")
        chunks.append(chunk)


class SpeechFailure(Exception):
    def __init__(self, code):
        self.code = code


class SpeechConfig:
    """Private server settings. Omitted api_key preserves it; an empty key clears it."""
    COMMON_FIELDS = {"enabled", "base_url", "api_key", "model", "timeout_seconds"}
    FIELDS = COMMON_FIELDS | {"language_format"}

    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "speech.json"
        if self.path.exists():
            self.load()
            os.chmod(self.path, 0o600)

    @staticmethod
    def common_defaults():
        return {"enabled": False, "base_url": "", "api_key": "", "model": "", "timeout_seconds": 25}

    @staticmethod
    def defaults():
        return {**SpeechConfig.common_defaults(), "language_format": "legacy"}

    def validate(self, updates, previous=None):
        if not isinstance(updates, dict) or set(updates) - self.FIELDS:
            raise ValueError("Unknown speech configuration field")
        result = dict(previous or self.defaults())
        result.update(updates)
        # TTS shares the private configuration plumbing, but not ASR fields.
        if "language_format" in self.FIELDS and result["language_format"] not in ("legacy", "languages"):
            raise ValueError("Invalid transcription language format")
        if type(result["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        for field, limit in (("base_url", 2048), ("api_key", 4096), ("model", 200)):
            value = result[field]
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid speech configuration text")
            result[field] = value.strip()
        if any(ord(c) > 126 or c.isspace() for c in result["api_key"]):
            raise ValueError("Invalid speech API key")
        if result["base_url"]:
            url = urlsplit(result["base_url"])
            if (any(c.isspace() for c in result["base_url"]) or "\\" in result["base_url"]
                    or url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment):
                raise ValueError("Speech base_url must be an HTTP(S) API base without credentials, query or fragment")
            try:
                url.port
            except ValueError as error:
                raise ValueError("Invalid speech base_url port") from error
            result["base_url"] = result["base_url"].rstrip("/")
        timeout = result["timeout_seconds"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 25:
            raise ValueError("timeout_seconds must be between 1 and 25")
        if result["enabled"] and (not result["base_url"] or not result["model"]):
            raise ValueError("Enabled speech requires base_url and model")
        return result

    def load(self):
        if not self.path.exists():
            return self.defaults()
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 16384:
            raise ValueError("Speech configuration must be a small regular file")
        return self.validate(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, updates):
        value = self.validate(updates, self.load())
        descriptor, name = tempfile.mkstemp(prefix=".speech-", dir=self.root)
        temporary = Path(name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return self.public(value)

    @staticmethod
    def public(value):
        return {**{key: val for key, val in value.items() if key != "api_key"}, "api_key_set": bool(value["api_key"])}


@lru_cache(maxsize=1)
def opus_library():
    """Bind the public libopus decoder ABI, not a handwritten codec."""
    library = ctypes.util.find_library("opus")
    if not library:
        raise SpeechFailure("codec_unavailable")
    try:
        lib = ctypes.CDLL(library)
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_destroy.restype = None
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_packet_get_nb_samples.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32, ctypes.c_int32]
        lib.opus_packet_get_nb_samples.restype = ctypes.c_int
        return lib
    except (OSError, AttributeError) as error:
        raise SpeechFailure("codec_unavailable") from error


class OpusAudio:
    def __init__(self, rate, channels):
        if rate not in RATES or channels not in (1, 2):
            raise SpeechFailure("invalid_audio_parameters")
        self.rate, self.channels = rate, channels
        self.lib = opus_library()
        error = ctypes.c_int()
        self.decoder = self.lib.opus_decoder_create(rate, channels, ctypes.byref(error))
        if not self.decoder or error.value:
            raise SpeechFailure("codec_unavailable")
        self.pcm = bytearray()
        self.opus_bytes = 0
        self.samples = 0

    def append(self, packet):
        if not packet or len(packet) > MAX_PACKET:
            raise SpeechFailure("malformed_audio")
        self.opus_bytes += len(packet)
        if self.opus_bytes > MAX_OPUS_BYTES:
            raise SpeechFailure("audio_limit")
        encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        count = self.lib.opus_packet_get_nb_samples(encoded, len(packet), self.rate)
        maximum = self.rate * 120 // 1000
        if not 0 < count <= maximum:
            raise SpeechFailure("malformed_audio")
        if (self.samples + count > self.rate * MAX_SECONDS
                or (self.samples + count) * self.channels * 2 > MAX_PCM_BYTES):
            raise SpeechFailure("audio_limit")
        output = (ctypes.c_int16 * (maximum * self.channels))()
        decoded = self.lib.opus_decode(self.decoder, encoded, len(packet), output, maximum, 0)
        if decoded < 0 or decoded != count:
            raise SpeechFailure("malformed_audio")
        pcm = array.array("h", output[:decoded * self.channels])
        if sys.byteorder != "little":
            pcm.byteswap()
        self.pcm.extend(pcm.tobytes())
        self.samples += decoded

    def wav(self):
        if not self.samples:
            raise SpeechFailure("empty_audio")
        result = io.BytesIO()
        with wave.open(result, "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.rate)
            output.writeframes(self.pcm)
        return result.getvalue()

    def close(self):
        if self.decoder:
            self.lib.opus_decoder_destroy(self.decoder)
            self.decoder = None
        self.pcm.clear()


def audio_parameters(request):
    allowed = {"encoding", "sample_rate", "channels", "language", "language_pinned", "forward_interims",
               "keepalive_ack", "organization_uuid", "conversation_uuid", "no_capture"}
    if len(request.query_string) > 4096 or set(request.query) - allowed:
        raise SpeechFailure("invalid_audio_parameters")
    for key in request.query:
        if len(request.query.getall(key)) != 1 or len(request.query[key]) > 200:
            raise SpeechFailure("invalid_audio_parameters")
    if request.query.get("encoding") != "opus":
        raise SpeechFailure("unsupported_encoding")
    try:
        rate = int(request.query.get("sample_rate", "16000"))
        channels = int(request.query.get("channels", "1"))
    except ValueError as error:
        raise SpeechFailure("invalid_audio_parameters") from error
    if rate not in RATES or channels not in (1, 2):
        raise SpeechFailure("invalid_audio_parameters")
    for key in ("language_pinned", "keepalive_ack", "no_capture"):
        if key in request.query and request.query[key] not in ("true", "false"):
            raise SpeechFailure("invalid_audio_parameters")
    if request.query.get("forward_interims", "typed") != "typed":
        raise SpeechFailure("invalid_audio_parameters")
    organization = request.query.get("organization_uuid")
    if organization is not None and organization != ORGANIZATION_ID:
        raise SpeechFailure("unknown_organization")
    language = request.query.get("language", "auto")
    if len(language) > 35 or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language):
        raise SpeechFailure("invalid_audio_parameters")
    language = language.split("-", 1)[0].lower()
    return rate, channels, None if language in ("auto", "und") else language


async def finish_owned(task):
    """Repeated caller cancellation cannot abandon an owned resource/task."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class SpeechBridge:
    def __init__(self, config):
        self.config = config
        self.http = None
        self.active = 0
        self.sockets = set()

    async def transcribe(self, settings, audio, language):
        form = aiohttp.FormData()
        form.add_field("file", audio, filename="recording.wav", content_type="audio/wav")
        form.add_field("model", settings["model"])
        form.add_field("response_format", "json")
        if language:
            form.add_field("languages[]" if settings.get("language_format") == "languages" else "language", language)
        headers = {"Authorization": "Bearer " + settings["api_key"]} if settings["api_key"] else {}
        try:
            timeout = aiohttp.ClientTimeout(total=settings["timeout_seconds"])
            async with self.http.post(settings["base_url"] + "/audio/transcriptions", data=form,
                                      headers=headers, timeout=timeout, allow_redirects=False) as response:
                if response.status != 200:
                    raise SpeechFailure("upstream_error")
                try:
                    raw = await limited_body(response.content, MAX_RESPONSE)
                    result = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as error:
                    raise SpeechFailure("upstream_invalid_response") from error
                if not isinstance(result, dict) or not isinstance(result.get("text"), str) or len(result["text"]) > 200000:
                    raise SpeechFailure("upstream_invalid_response")
                return result["text"]
        except asyncio.TimeoutError as error:
            raise SpeechFailure("upstream_timeout") from error
        except aiohttp.ClientError as error:
            raise SpeechFailure("upstream_unavailable") from error

    @staticmethod
    async def fail(ws, code):
        if not ws.closed:
            await ws.send_json({"type": "TranscriptError", "error_code": code})
            await ws.close(code=1011)

    async def socket(self, request):
        authorized = request.get("duet_authorized", lambda: True)
        if not authorized():
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(max_msg_size=MAX_PACKET, heartbeat=15)
        await ws.prepare(request)
        audio = receive = upstream = None
        self.sockets.add(ws)
        self.active += 1
        released = False

        async def release_work():
            async def cleanup():
                nonlocal released
                try:
                    for task in (receive, upstream):
                        if task is not None and not task.done():
                            task.cancel()
                    await asyncio.gather(*(task for task in (receive, upstream) if task is not None),
                                         return_exceptions=True)
                    if audio is not None:
                        audio.close()
                finally:
                    if not released:
                        released = True
                        self.active -= 1
            await finish_owned(asyncio.create_task(cleanup()))

        try:
            if self.active > 4:
                raise SpeechFailure("concurrent_limit")
            rate, channels, language = audio_parameters(request)
            try:
                settings = self.config.load()
            except (ValueError, OSError):
                raise SpeechFailure("not_configured")
            if not settings["enabled"]:
                raise SpeechFailure("not_configured")
            audio = OpusAudio(rate, channels)
            await ws.send_json({"type": "audio_capture_started", "voice_session_uuid": str(uuid4())})
            await ws.send_json({"type": "ProviderInfo", "finals": "at_stop"})
            started = last_audio = time.monotonic()
            while not ws.closed:
                if not authorized():
                    await release_work()
                    await ws.close(code=1008, message=b"Session expired")
                    break
                if upstream is None and (time.monotonic() - started > MAX_SECONDS or time.monotonic() - last_audio > IDLE_SECONDS):
                    raise SpeechFailure("audio_limit")
                if receive is None:
                    receive = asyncio.create_task(ws.receive())
                tasks = [receive] + ([upstream] if upstream is not None else [])
                done, _ = await asyncio.wait(tasks, timeout=.25, return_when=asyncio.FIRST_COMPLETED)
                # Client closure/revocation wins a simultaneous transcription result.
                incoming = None
                if receive in done:
                    incoming = receive.result()
                    receive = None
                    if incoming.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                if not authorized():
                    await release_work()
                    await ws.close(code=1008, message=b"Session expired")
                    break
                if upstream is not None and upstream in done:
                    result = upstream.result()
                    await release_work()
                    if not authorized():
                        await ws.close(code=1008, message=b"Session expired")
                        break
                    await ws.send_json({"type": "TranscriptText", "data": result})
                    await ws.close(code=1000)
                    break
                if incoming is None:
                    continue
                if incoming.type == aiohttp.WSMsgType.BINARY:
                    if upstream is not None:
                        raise SpeechFailure("malformed_message")
                    audio.append(incoming.data)
                    last_audio = time.monotonic()
                elif incoming.type == aiohttp.WSMsgType.TEXT:
                    if len(incoming.data) > 1024:
                        raise SpeechFailure("malformed_message")
                    try:
                        control = json.loads(incoming.data)
                    except ValueError as error:
                        raise SpeechFailure("malformed_message") from error
                    kind = control.get("type") if isinstance(control, dict) else None
                    if kind == "KeepAlive":
                        await ws.send_json({"type": "KeepAliveAck"})
                    elif kind == "CloseStream":
                        if upstream is None:
                            upstream = asyncio.create_task(self.transcribe(settings, audio.wav(), language))
                    else:
                        raise SpeechFailure("malformed_message")
        except SpeechFailure as error:
            await release_work()
            if authorized():
                await self.fail(ws, error.code)
            elif not ws.closed:
                await ws.close(code=1008, message=b"Session expired")
        except (ConnectionError, asyncio.CancelledError):
            raise
        finally:
            await release_work()
            self.sockets.discard(ws)
        return ws


def register_speech(app, data_dir, engine=None):
    """Register before catch-all. Host must restrict config endpoints to its owner/admin.

    engine is accepted for host integration; speech has its own settings and HTTP
    session, so a chat model switch cannot alter an in-flight transcription.
    """
    bridge = SpeechBridge(SpeechConfig(data_dir))
    app[SPEECH] = bridge

    async def config(request):
        try:
            if request.method == "GET":
                return web.json_response(bridge.config.public(bridge.config.load()), headers={"Cache-Control": "no-store"})
            if request.content_length is not None and request.content_length > 16384:
                return web.json_response({"error": "Speech configuration is too large"}, status=413)
            body = await limited_body(request.content, 16384)
            result = bridge.config.save(json.loads(body))
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, UnicodeDecodeError):
            return web.json_response({"error": "Invalid speech configuration"}, status=400)
        except OSError:
            return web.json_response({"error": "Speech configuration is unavailable"}, status=503)

    async def lifecycle(app):
        async with aiohttp.ClientSession(trust_env=False) as http:
            bridge.http = http
            yield
            await asyncio.gather(*(ws.close(code=1001) for ws in tuple(bridge.sockets)), return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(bridge.sockets)), return_exceptions=True)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/duet/speech/config", config)
    app.router.add_put("/duet/speech/config", config)
    for prefix in ("", "/api"):
        app.router.add_get(prefix + "/ws/speech_to_text/voice_stream", bridge.socket)
    return bridge
