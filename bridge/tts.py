"""Private, opt-in original Duet read-aloud WebSocket bridge.

Wire evidence: original t4.i, readaloud.d.q, ik90/gk90, mk90 and cco.
OpenAI-compatible /audio/speech must return headerless mono s16le at 24 kHz.
ffmpeg resamples it; libopus produces one raw 48 kHz/32 kbps packet per WS
binary frame. This is read-aloud only, not the conversation/Talk protocol.
Linux runtime dependencies: ffmpeg with azmq/atempo/asetnsamples, libopus0, and pyzmq.
Input and output are paced with bounded lookahead; live tempo preserves pitch.
No text/audio is persisted.

References:
https://developers.openai.com/api/docs/guides/text-to-speech
https://developers.openai.com/api/reference/python/resources/audio/subresources/speech/methods/create
https://www.opus-codec.org/docs/opus_api-1.5/group__opus__encoder.html
"""
import asyncio
import ctypes
from collections import deque
import importlib.util
import json
import hashlib
import io
import re
import wave
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import aiohttp
from aiohttp import web

from .rest import ORGANIZATION_ID
from .speech import SpeechConfig, SpeechFailure, finish_owned, limited_body, opus_library
from .tts_upstream import pcm_source

TTS = web.AppKey("duet_tts", object)
MAX_TEXT = 4096
MAX_AUDIO_SECONDS = 120
MAX_PCM_BYTES = 24000 * 2 * MAX_AUDIO_SECONDS
MAX_MESSAGE = 32768
IDLE_SECONDS = 30


class TempoControl:
    def __init__(self, speed):
        self.speed = speed
        self.applied_speed = self.processing_speed = speed
        self.updates = asyncio.Queue(maxsize=1)

    @property
    def conversion_speed_bound(self):
        # A dequeued command can still be in flight after a newer UI control
        # replaces the queue. Include it until both filter acks are received.
        return max(self.speed, self.applied_speed, self.processing_speed)

    def change(self, speed):
        if math.isclose(speed, self.speed, rel_tol=1e-6, abs_tol=1e-6):
            return
        self.speed = speed
        if self.updates.full():
            self.updates.get_nowait()
        self.updates.put_nowait(speed)

    async def commands(self, endpoint, initial_speed):
        import zmq
        context = zmq.Context()
        connection = context.socket(zmq.REQ)
        connection.setsockopt(zmq.LINGER, 0)
        connection.connect(endpoint)
        try:
            while True:
                speed = await self.updates.get()
                self.processing_speed = speed
                if math.isclose(speed, initial_speed, rel_tol=1e-6, abs_tol=1e-6):
                    continue
                for target in ("atempo@duet_a", "atempo@duet_b"):
                    connection.send_string(f"{target} tempo {math.sqrt(speed):.12g}", flags=zmq.DONTWAIT)
                    deadline = time.monotonic() + 2
                    # Nonblocking libzmq calls work on both Linux and Windows'
                    # Proactor loop without adding a Tornado selector thread.
                    while True:
                        try:
                            response = connection.recv(flags=zmq.DONTWAIT)
                            break
                        except zmq.Again:
                            if time.monotonic() >= deadline:
                                raise SpeechFailure("tempo_control_timeout")
                            await asyncio.sleep(.005)
                    if not response.startswith(b"0 "):
                        raise SpeechFailure("tempo_control_failed")
                initial_speed = speed
                self.applied_speed = speed
        except zmq.ZMQError as error:
            raise SpeechFailure("tempo_control_failed") from error
        finally:
            connection.close(linger=0)
            context.destroy(linger=0)


class TTSConfig(SpeechConfig):
    FIELDS = SpeechConfig.COMMON_FIELDS | {"voice", "voice_catalog", "default_language_code", "transport"}

    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "tts.json"
        if self.path.exists():
            self.load()
            os.chmod(self.path, 0o600)

    @staticmethod
    def defaults():
        return {**SpeechConfig.common_defaults(), "voice": "", "voice_catalog": [],
                "default_language_code": "en", "transport": "speech_http"}

    def validate(self, updates, previous=None):
        value = super().validate(updates, previous)
        if value["transport"] not in ("speech_http", "realtime_ws"):
            raise ValueError("Invalid TTS transport")
        if value["transport"] == "speech_http" and value["model"].startswith("gpt-realtime"):
            raise ValueError("Realtime models require the realtime_ws transport")
        voice = value["voice"]
        if (not isinstance(voice, str) or len(voice) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in voice)):
            raise ValueError("Invalid TTS voice")
        value["voice"] = voice.strip()
        catalog = value["voice_catalog"]
        if not isinstance(catalog, list) or len(catalog) > 8:
            raise ValueError("Invalid voice catalog")
        ids, codes = set(), set()
        for language in catalog:
            if not isinstance(language, dict) or set(language) != {"code", "display_name", "voices"}:
                raise ValueError("Invalid catalog language")
            code = language["code"]
            if not isinstance(code, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", code) or code in codes:
                raise ValueError("Invalid catalog language code")
            codes.add(code)
            self.label(language["display_name"])
            if not isinstance(language["voices"], list) or not 1 <= len(language["voices"]) <= 8:
                raise ValueError("Invalid catalog voices")
            for entry in language["voices"]:
                if not isinstance(entry, dict) or set(entry) != {"id", "display_name", "provider_voice"}:
                    raise ValueError("Invalid catalog voice")
                ident = entry["id"]
                if (not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", ident)
                        or ident == "mellow" or ident in ids):
                    raise ValueError("Invalid native voice ID")
                ids.add(ident)
                self.label(entry["display_name"])
                provider = entry["provider_voice"]
                if (not isinstance(provider, str) or not 1 <= len(provider) <= 200
                        or provider != provider.strip() or any(ord(c) < 32 or ord(c) == 127 for c in provider)):
                    raise ValueError("Invalid provider voice")
        if len(ids) > 16:
            raise ValueError("Too many voices")
        if (not isinstance(value["default_language_code"], str)
                or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", value["default_language_code"])
                or (catalog and value["default_language_code"] not in codes)):
            raise ValueError("Invalid default language")
        if value["enabled"] and not value["voice"] and not catalog:
            raise ValueError("Enabled TTS requires an explicit provider voice")
        # SpeechConfig.save atomically writes this complete merged object,
        # using these same serialization options. A valid small delta may
        # otherwise exceed the bounded reader once preserved secrets are added.
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16384:
            raise ValueError("TTS configuration exceeds the readable file limit")
        return value

    @staticmethod
    def label(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 80 or value != value.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Invalid catalog label")

    @staticmethod
    def catalog(settings):
        # Explicit compatibility migration: airy was the native fallback and
        # always selected this singleton before catalogs existed. No factory
        # voice resemblance is claimed by the display name.
        return settings["voice_catalog"] or ([{
            "code": settings["default_language_code"], "display_name": "Configured language",
            "voices": [{"id": "airy", "display_name": "Configured voice",
                        "provider_voice": settings["voice"]}]}] if settings["voice"] else [])

    @classmethod
    def select(cls, settings, ident):
        catalog = cls.catalog(settings)
        if ident is None:
            language = next((x for x in catalog if x["code"] == settings["default_language_code"]), None)
            ident = language["voices"][0]["id"] if language else None
        for language in catalog:
            for entry in language["voices"]:
                if entry["id"] == ident:
                    return {**settings, "voice": entry["provider_voice"]}
        raise SpeechFailure("unknown_voice")

    @classmethod
    def revision(cls, settings):
        # Catalog/provider/model changes invalidate native URL-keyed audio caches.
        # Credentials are excluded from this public, unkeyed content digest.
        return hashlib.sha256(json.dumps({k: v for k, v in settings.items() if k != "api_key"}, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:32]


class OpusEncoder:
    """Public libopus ABI. Each encode call returns a raw packet, not Ogg."""
    frame_bytes = 960 * 2

    def __init__(self):
        self.encoder = None
        self.lib = opus_library()
        try:
            self.lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                                                     ctypes.POINTER(ctypes.c_int)]
            self.lib.opus_encoder_create.restype = ctypes.c_void_p
            self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
            self.lib.opus_encoder_destroy.restype = None
            self.lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
            self.lib.opus_encode.restype = ctypes.c_int
            self.lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.lib.opus_encoder_ctl.restype = ctypes.c_int
            error = ctypes.c_int()
            self.encoder = self.lib.opus_encoder_create(48000, 1, 2049, ctypes.byref(error))
            if not self.encoder or error.value:
                raise SpeechFailure("codec_unavailable")
            # OPUS_SET_BITRATE(32000), OPUS_SET_VBR(0). Fixed 20 ms / 80 byte frames.
            for setting, value in ((4002, 32000), (4006, 0)):
                if self.lib.opus_encoder_ctl(self.encoder, setting, ctypes.c_int(value)):
                    raise SpeechFailure("codec_unavailable")
        except (OSError, AttributeError) as error:
            self.close()
            raise SpeechFailure("codec_unavailable") from error
        except BaseException:
            self.close()
            raise

    def encode(self, pcm):
        if len(pcm) != self.frame_bytes:
            raise SpeechFailure("invalid_pcm")
        source = (ctypes.c_int16 * 960).from_buffer_copy(pcm)
        packet = (ctypes.c_ubyte * 1276)()
        size = self.lib.opus_encode(self.encoder, source, 960, packet, len(packet))
        if size <= 0:
            raise SpeechFailure("codec_failure")
        return bytes(packet[:size])

    def close(self):
        if self.encoder:
            self.lib.opus_encoder_destroy(self.encoder)
            self.encoder = None


def query_parameters(request):
    allowed = {"voice", "output_format", "organization_uuid", "tts_speed"}
    if set(request.query) - allowed or any(len(request.query.getall(k)) != 1 for k in request.query):
        raise SpeechFailure("invalid_parameters")
    if request.query.get("organization_uuid", ORGANIZATION_ID) != ORGANIZATION_ID:
        raise SpeechFailure("unknown_organization")
    output_format = request.query.get("output_format")
    if output_format not in ("pcm_16000", "opus_48000_32"):
        raise SpeechFailure("unsupported_output_format")
    voice = request.query.get("voice")
    if voice is not None and (not re.fullmatch(r"[a-z0-9_-]{1,64}", voice) or voice == "mellow"):
        raise SpeechFailure("invalid_parameters")
    try:
        speed = float(request.query.get("tts_speed", "1"))
    except ValueError as error:
        raise SpeechFailure("invalid_parameters") from error
    return output_format, valid_speed(speed)


def valid_speed(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not .25 <= value <= 4:
        raise SpeechFailure("invalid_speed")
    return float(value)


class TTSBridge:
    def __init__(self, config, home=None):
        self.config = config
        self.home = home
        self.http = None
        self.sockets = set()
        self.active = set()
        self.ffmpeg = shutil.which("ffmpeg")
        self.filters_ready = None

    def readiness(self):
        if not self.ffmpeg:
            return "resampler_unavailable"
        spec = importlib.util.find_spec("zmq")
        if spec is None or spec.loader is None:
            return "tempo_control_unavailable"
        try:
            import zmq
        except (ImportError, OSError):
            return "tempo_control_unavailable"
        try:
            context = zmq.Context()
            context.destroy(linger=0)
        except AttributeError:
            return "tempo_control_unavailable"
        except zmq.ZMQError:
            return "tempo_control_unavailable"
        if self.filters_ready is None:
            try:
                result = subprocess.run([self.ffmpeg, "-hide_banner", "-filters"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                                        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                filters = result.stdout.decode("utf-8", errors="replace")
                self.filters_ready = (result.returncode == 0 and " azmq " in filters and " atempo " in filters
                                      and " asetnsamples " in filters)
            except (OSError, subprocess.TimeoutExpired):
                self.filters_ready = False
        if not self.filters_ready:
            return "tempo_control_unavailable"
        try:
            encoder = OpusEncoder()
            encoder.close()
        except SpeechFailure:
            return "codec_unavailable"
        return None

    def refresh_capability(self):
        try:
            ready = self.config.load()["enabled"] and self.readiness() is None
        except (ValueError, OSError):
            ready = False
        if self.home is not None:
            capabilities = set(self.home.capabilities) - {"read_aloud_v1"}
            if ready:
                capabilities.add("read_aloud_v1")
            self.home.capabilities = sorted(capabilities)
        return bool(ready)

    async def synthesize(self, ws, settings, text, playback, output_format, authorized):
        if not settings["voice"]:
            settings = self.config.select(settings, None)
        process = None
        producer = consumer = controller = audio_jobs = None
        encoder = None
        frames = 0
        rate = 48000 if output_format == "opus_48000_32" else 16000
        output_started = None
        # Source-time credits follow bytes actually delivered to the paced
        # player, not elapsed wall time. Two atempo stages need bounded input
        # lookahead before their first output; keep that allowance independent
        # of process startup and client/output stalls.
        input_lookahead = 24000 * 2 * .32  # Scaled to output time at speeds above 1x.
        input_fed = input_retired = 0
        output_progress = asyncio.Event()
        input_segments = deque()
        input_window_speed = playback.conversion_speed_bound

        async def send(data):
            nonlocal output_started, input_retired
            if output_started is None:
                output_started = time.monotonic()
            # Keep at most one 20 ms audio frame ahead of the native player.
            # A fast provider must not fill the phone with a whole old-tempo clip.
            delay = output_started + frames / rate - time.monotonic()
            if delay < -.02:
                # Provider/network stalls must not cause a later catch-up burst
                # that queues seconds of already-converted audio on the phone.
                output_started = time.monotonic() - frames / rate
                delay = 0
            if delay > 0:
                await asyncio.sleep(delay)
            if not authorized() or ws.closed:
                raise asyncio.CancelledError()
            await ws.send_bytes(data)
            duration = .02 if encoder is not None else len(data) / (rate * 2)
            # A source segment already queued during a slowdown may still
            # produce old, faster-tempo output. Retire against its upper speed
            # bound, never the newly requested slower ratio: under-crediting
            # those old frames can starve the filters indefinitely.
            speed_bound = playback.conversion_speed_bound
            for segment in input_segments:
                segment[1] = max(segment[1], speed_bound)
            while duration > 0 and input_segments:
                segment = input_segments[0]
                retired = min(segment[0], duration * segment[1] * 24000 * 2)
                input_retired += retired
                segment[0] -= retired
                duration -= retired / (segment[1] * 24000 * 2)
                if segment[0] < 1e-6:
                    input_retired += segment[0]
                    input_segments.popleft()
            output_progress.set()

        async def convert_output():
            nonlocal frames
            pending = bytearray()
            while True:
                chunk = await process.stdout.read(rate // 50 * 2)
                if not chunk:
                    break
                pending.extend(chunk)
                if len(pending) + frames * 2 > (MAX_AUDIO_SECONDS + .05) * rate * 2:
                    raise SpeechFailure("audio_limit")
                if encoder is not None:
                    while len(pending) >= encoder.frame_bytes:
                        packet = encoder.encode(bytes(pending[:encoder.frame_bytes]))
                        del pending[:encoder.frame_bytes]
                        await send(packet)
                        frames += 960
                else:
                    size = rate // 50 * 2
                    while len(pending) >= size:
                        await send(bytes(pending[:size]))
                        frames += size // 2
                        del pending[:size]
            if len(pending) % 2:
                raise SpeechFailure("invalid_pcm")
            if encoder is None and pending:
                await send(bytes(pending))
                frames += len(pending) // 2
            if encoder is not None and (frames or pending):
                # Raw Opus has no Ogg preskip/end-trim metadata. Pad the final
                # partial 20 ms frame and flush one frame of encoder lookahead.
                if pending:
                    await send(encoder.encode(bytes(pending).ljust(encoder.frame_bytes, b"\0")))
                    frames += 960
                await send(encoder.encode(bytes(encoder.frame_bytes)))
                frames += 960

        try:
            async with asyncio.timeout(MAX_AUDIO_SECONDS + settings["timeout_seconds"] + 5):
                if encoder is None and output_format == "opus_48000_32":
                    encoder = OpusEncoder()
                async with pcm_source(self.http, settings, text, max_bytes=MAX_PCM_BYTES) as chunks:
                    # ffmpeg owns this private loopback control listener. A bind
                    # race/failure aborts conversion; never fall back to wildcard.
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    endpoint = f"tcp://127.0.0.1:{port}"
                    initial_speed = playback.speed
                    playback.applied_speed = playback.processing_speed = initial_speed
                    tempo = math.sqrt(initial_speed)
                    filters = ("asetnsamples=n=480:p=0,"
                               f"azmq=bind_address='tcp\\://127.0.0.1\\:{port}',"
                               f"atempo@duet_a={tempo:.12g},atempo@duet_b={tempo:.12g}")
                    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                        self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "s16le", "-ar", "24000", "-ac", "1", "-probesize", "32",
                        "-analyzeduration", "0", "-avioflags", "direct", "-blocksize", "960", "-i", "pipe:0",
                        "-af", filters, "-f", "s16le", "-ar", str(rate), "-ac", "1",
                        "-flush_packets", "1", "-avioflags", "direct", "pipe:1",
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        # StreamReader's default 64 KiB threshold can queue
                        # seconds of old-tempo PCM ahead of send(). Couple real
                        # converter progress to the paced consumer instead.
                        limit=rate // 50 * 2,
                        **({"pipesize": 4096} if sys.platform.startswith("linux") else {}),
                        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})))
                    try:
                        async with asyncio.timeout(settings["timeout_seconds"]):
                            process = await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        # Capture ownership even if the client disconnects during
                        # process creation, so finally can always terminate it.
                        process = await finish_owned(spawn)
                        raise
                    except TimeoutError:
                        process = await finish_owned(spawn)
                        raise

                    # drain() must propagate that output backpressure to input,
                    # rather than adding another default 64 KiB Python queue.
                    process.stdin.transport.set_write_buffer_limits(high=960, low=0)

                    async def provide_input():
                        total = 0
                        prefix = bytearray()
                        pending = bytearray()
                        deadline = time.monotonic() - .06

                        async def feed(data):
                            nonlocal deadline, input_fed, input_window_speed
                            while True:
                                speed_bound = playback.conversion_speed_bound
                                # Commands can change the conversion rate of
                                # source bytes already in the converter. Raise
                                # their bound, but never grant credits merely
                                # because a control message arrived.
                                for segment in input_segments:
                                    segment[1] = max(segment[1], speed_bound)
                                # Frames admitted at a faster tempo still
                                # occupy its larger two-stage filter window.
                                # Do not shrink that allowance on a slowdown;
                                # otherwise the converter may need input while
                                # all credits wait for output it cannot yet emit.
                                input_window_speed = max(input_window_speed, speed_bound)
                                allowance = input_lookahead * max(1, input_window_speed)
                                if input_fed + len(data) <= input_retired + allowance + 1:
                                    break
                                if not authorized() or ws.closed:
                                    raise asyncio.CancelledError()
                                output_progress.clear()
                                await output_progress.wait()
                            if deadline < time.monotonic() - .06:
                                deadline = time.monotonic() - .06
                            delay = deadline - time.monotonic()
                            if delay > 0:
                                await asyncio.sleep(delay)
                            if not authorized() or ws.closed:
                                raise asyncio.CancelledError()
                            process.stdin.write(data)
                            input_fed += len(data)
                            speed_bound = playback.conversion_speed_bound
                            if input_segments and input_segments[-1][1] == speed_bound:
                                input_segments[-1][0] += len(data)
                            else:
                                input_segments.append([len(data), speed_bound])
                            await process.stdin.drain()
                            deadline += len(data) / (24000 * 2 * playback.speed)

                        async for chunk in chunks:
                            if not authorized() or ws.closed:
                                raise asyncio.CancelledError()
                            total += len(chunk)
                            if total > MAX_PCM_BYTES:
                                raise SpeechFailure("audio_limit")
                            if len(prefix) < 12:
                                prefix.extend(chunk)
                                if len(prefix) < 12:
                                    continue
                                if (bytes(prefix[:4]) in (b"RIFF", b"OggS", b"fLaC")
                                        or bytes(prefix[:3]) == b"ID3"):
                                    raise SpeechFailure("upstream_invalid_audio")
                                chunk = bytes(prefix)
                            pending.extend(chunk)
                            while len(pending) >= 960:
                                await feed(bytes(pending[:960]))
                                del pending[:960]
                        if not total or total % 2:
                            raise SpeechFailure("upstream_invalid_audio")
                        if total < 12:
                            pending.extend(prefix)
                        if pending:
                            await feed(bytes(pending))
                        process.stdin.close()
                        await process.stdin.wait_closed()

                    producer = asyncio.create_task(provide_input())
                    consumer = asyncio.create_task(convert_output())
                    controller = asyncio.create_task(playback.commands(endpoint, initial_speed))
                    audio_jobs = asyncio.gather(producer, consumer)
                    completed, _ = await asyncio.wait([audio_jobs, controller], return_when=asyncio.FIRST_COMPLETED)
                    if controller in completed:
                        controller.result()
                    await audio_jobs
                    if await process.wait() != 0 or not frames:
                        raise SpeechFailure("upstream_invalid_audio")
                    return frames * 1000 // rate
        except TimeoutError as error:
            raise SpeechFailure("upstream_timeout") from error
        except aiohttp.ClientError as error:
            raise SpeechFailure("upstream_unavailable") from error
        except (OSError, BrokenPipeError) as error:
            raise SpeechFailure("audio_conversion_failed") from error
        finally:
            async def cleanup():
                for task in (producer, consumer, controller):
                    if task is not None and not task.done():
                        task.cancel()
                if process is not None and process.returncode is None:
                    process.stdin.close()
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.gather(*(t for t in (producer, consumer, controller) if t is not None),
                                     return_exceptions=True)
                if audio_jobs is not None:
                    await asyncio.gather(audio_jobs, return_exceptions=True)
                if process is not None:
                    # Join the cancelled reader before draining: concurrent
                    # StreamReader reads are invalid. On Linux, kill()+wait()
                    # can still wait for pipe EOF when stdout is paused at its
                    # high-water mark. Discard the killed converter's bounded
                    # tail so pipes disconnect before awaiting process exit.
                    while await process.stdout.read(8192):
                        pass
                    await process.wait()
                if encoder is not None:
                    encoder.close()
            await finish_owned(asyncio.create_task(cleanup()))

    @staticmethod
    async def fail(ws, code):
        if not ws.closed:
            await ws.send_json({"type": "SpeechError", "error_code": code})
            await ws.close(code=1011)

    async def preview(self, request):
        authorized = request.get("duet_authorized", lambda: True)
        if not authorized():
            raise web.HTTPUnauthorized()
        if request.query:
            raise web.HTTPBadRequest()
        try:
            settings = self.config.load()
        except (ValueError, OSError):
            raise web.HTTPServiceUnavailable()
        config_snapshot = settings
        if request.match_info["revision"] != self.config.revision(settings):
            raise web.HTTPNotFound()
        try:
            settings = self.config.select(settings, request.match_info["voice"])
        except SpeechFailure:
            raise web.HTTPNotFound()
        if not settings["enabled"] or self.readiness():
            raise web.HTTPServiceUnavailable()
        if len(self.active) >= 4:
            raise web.HTTPTooManyRequests()
        pcm = bytearray()
        class Sink:
            closed = False
            async def send_bytes(inner, data):
                if len(pcm) + len(data) > 16000 * 2 * 15:
                    raise SpeechFailure("audio_limit")
                pcm.extend(data)
        sink = Sink()
        self.active.add(sink)
        def alive():
            return (authorized() and request.transport is not None and not request.transport.is_closing()
                    and self.config.load() == config_snapshot)
        job = asyncio.create_task(self.synthesize(sink, settings,
            "This is a sample of the configured voice.", TempoControl(1), "pcm_16000", alive))
        try:
            # Also revoke/cancel while a provider is silent, before any PCM.
            async with asyncio.timeout(min(25, settings["timeout_seconds"] + 16)):
                while not job.done():
                    if not alive():
                        raise web.HTTPUnauthorized()
                    await asyncio.wait({job}, timeout=.05)
                await job
            if not alive():
                raise web.HTTPUnauthorized()
            if not pcm:
                raise SpeechFailure("upstream_invalid_audio")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)
            return web.Response(body=output.getvalue(), content_type="audio/wav",
                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except (SpeechFailure, TimeoutError, aiohttp.ClientError, OSError, ValueError):
            return web.json_response({"error": "voice_preview_failed"}, status=502,
                                     headers={"Cache-Control": "no-store"})
        finally:
            job.cancel()
            await finish_owned(asyncio.create_task(self._finish_preview(job, sink)))
            pcm.clear()

    async def _finish_preview(self, job, sink):
        try:
            await asyncio.gather(job, return_exceptions=True)
        finally:
            self.active.discard(sink)

    async def socket(self, request):
        authorized = request.get("duet_authorized", lambda: True)
        if not authorized():
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE, heartbeat=15)
        await ws.prepare(request)
        self.sockets.add(ws)
        self.active.add(ws)
        receive = upstream = None
        text = []
        length = 0

        async def release_work():
            async def cleanup():
                try:
                    for task in (receive, upstream):
                        if task is not None and not task.done():
                            task.cancel()
                    await asyncio.gather(*(t for t in (receive, upstream) if t is not None),
                                         return_exceptions=True)
                finally:
                    self.active.discard(ws)
            await finish_owned(asyncio.create_task(cleanup()))

        try:
            if len(self.active) > 4:
                raise SpeechFailure("concurrent_limit")
            output_format, speed = query_parameters(request)
            playback = TempoControl(speed)
            try:
                settings = self.config.load()
            except (ValueError, OSError) as error:
                raise SpeechFailure("not_configured") from error
            if not settings["enabled"]:
                raise SpeechFailure("not_configured")
            settings = self.config.select(settings, request.query.get("voice"))
            readiness = self.readiness()
            if readiness:
                raise SpeechFailure(readiness)
            started = time.monotonic()
            while not ws.closed:
                if not authorized():
                    await release_work()
                    await ws.close(code=1008, message=b"Session expired")
                    break
                if upstream is None and time.monotonic() - started > IDLE_SECONDS:
                    raise SpeechFailure("text_timeout")
                if receive is None:
                    receive = asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait([receive] + ([upstream] if upstream else []),
                                             timeout=.1, return_when=asyncio.FIRST_COMPLETED)
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
                    duration = upstream.result()
                    await release_work()
                    if not authorized():
                        await ws.close(code=1008, message=b"Session expired")
                        break
                    if not ws.closed:
                        await ws.send_json({"type": "SpeechComplete", "audio_sent_ms": duration})
                        await ws.close(code=1000)
                    break
                if incoming is None:
                    continue
                if incoming.type != aiohttp.WSMsgType.TEXT:
                    raise SpeechFailure("malformed_message")
                try:
                    control = json.loads(incoming.data)
                except ValueError as error:
                    raise SpeechFailure("malformed_message") from error
                if not isinstance(control, dict):
                    raise SpeechFailure("malformed_message")
                kind = control.get("type")
                if kind == "keep_alive" and set(control) == {"type"}:
                    continue
                if kind == "set_playback_speed" and set(control) == {"type", "speed"}:
                    new_speed = valid_speed(control["speed"])
                    playback.change(new_speed)
                    speed = new_speed
                elif kind == "text_chunk" and set(control) == {"type", "text"}:
                    chunk = control["text"]
                    if upstream is not None or not isinstance(chunk, str):
                        raise SpeechFailure("malformed_message")
                    length += len(chunk)
                    if length > MAX_TEXT or "\0" in chunk or any(0xd800 <= ord(c) <= 0xdfff for c in chunk):
                        raise SpeechFailure("text_limit")
                    text.append(chunk)
                elif kind == "close_stream" and set(control) == {"type"}:
                    if upstream is not None:
                        raise SpeechFailure("malformed_message")
                    combined = "".join(text)
                    if not combined.strip():
                        raise SpeechFailure("empty_text")
                    upstream = asyncio.create_task(self.synthesize(ws, settings, combined, playback, output_format, authorized))
                    text.clear()
                else:
                    raise SpeechFailure("malformed_message")
        except SpeechFailure as error:
            # Terminal messages can be received before a close handshake ends.
            # Reclaim the actual jobs and quota first, so closed/closing sockets
            # cannot reject subsequent requests as if four syntheses were live.
            await release_work()
            if authorized():
                await self.fail(ws, error.code)
            elif not ws.closed:
                await ws.close(code=1008, message=b"Session expired")
        except (ConnectionError, asyncio.CancelledError):
            raise
        finally:
            await release_work()
            text.clear()
            self.sockets.discard(ws)
        return ws


def register_tts(app, data_dir, *, home=None):
    """Host middleware must authenticate private WS and owner configuration routes.

    Chat credentials are deliberately never inherited. A configured provider must
    implement the PCM audio/speech contract, with an explicitly chosen voice/model.
    """
    bridge = TTSBridge(TTSConfig(data_dir), home)
    app[TTS] = bridge
    bridge.refresh_capability()

    async def config(request):
        try:
            if request.method == "PUT":
                body = await limited_body(request.content, 16384)
                value = bridge.config.save(json.loads(body))
                await asyncio.gather(*(ws.close(code=1001, message=b"TTS settings changed")
                                       for ws in tuple(bridge.sockets)), return_exceptions=True)
                bridge.refresh_capability()
            else:
                value = bridge.config.public(bridge.config.load())
            return web.json_response({**value, "ready": bridge.refresh_capability()},
                                     headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, UnicodeDecodeError):
            return web.json_response({"error": "Invalid TTS configuration"}, status=400)
        except OSError:
            return web.json_response({"error": "TTS configuration is unavailable"}, status=503)

    async def lifecycle(app):
        async with aiohttp.ClientSession(trust_env=False) as http:
            bridge.http = http
            yield

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(bridge.sockets)), return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/duet/tts/config", config)
    app.router.add_put("/duet/tts/config", config)
    for prefix in ("", "/api"):
        app.router.add_get(prefix + "/ws/text_to_speech/text_stream", bridge.socket)
        app.router.add_get(prefix + "/duet/voice-previews/{revision:[a-f0-9]{32}}/{voice:[a-z0-9_-]{1,64}}.wav", bridge.preview, allow_head=False)
    return bridge
