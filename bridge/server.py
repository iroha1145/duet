"""Local compatibility development host. No official service forwarding."""
import argparse
import copy
import logging
import os
import re
import secrets
from pathlib import Path
from aiohttp import web
from .rpc import register_rpc
from .rest import register_rest
from .rest_home import register_home
from .aibo import register_aibo, canonical_session
from .schedules import register_schedules
from .routines import ROUTINES, register_routines
from .workspace import register_workspace
from .repositories import register_repositories
from .models import ModelCatalog, register_models
from .auth import AuthManager
from .uploads import register_uploads
from .admin import register_admin
from .speech import register_speech
from .tts import register_tts
from .voice_preferences import register_voice_preferences
from .talk import register_talk
from .channels import register_channels
from .task_watch import register_task_watch
from .request_cards import register_request_cards
from .other_sessions import register_other_sessions
from .channel_tasks import register_channel_tasks
from .search import register_search
from .usage import register_usage
from .connectors import register_connectors
from .connector_native import CONNECTOR_DIRECTORY_FEATURE, OAUTH_PUBLIC_ROUTES, register_native_connectors
from .rules import register_rules
from .notifications import register_notifications
from .plugins import register_plugins
AIBO = web.AppKey("duet_native_aibo", object)


class TextOnlyConfig:
    """The legacy RPC surface has no implemented tool-approval wire protocol."""
    def __init__(self, config):
        self.config = config

    def load(self):
        value = copy.deepcopy(self.config.load())
        value["allow_tools"] = False
        return value


def create_app(data_dir, *, token=None, fixture=False, account_login=False, public_origin="http://127.0.0.1:7080", protocol_diagnostics=False):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)

    @web.middleware
    async def trace(request, handler):
        if protocol_diagnostics and re.fullmatch(r"/(?:api/)?v1/code/(?:channels/chan_[a-z0-9-]+/(?:messages/stream|threads/cmsg_[a-z0-9-]+)|sessions/(?:cse|session)_[a-z0-9-]+/events/stream)", request.path):
            cursor = {}
            for key in ("scope", "thread_id", "from", "from_id", "from_sequence_num"):
                value = request.query.get(key)
                if value is not None:
                    cursor[key] = value if value in ("channel", "thread", "tail") or re.fullmatch(r"(?:cmsg_|session_|cse_)[a-z0-9-]{1,100}|[0-9]{1,16}", value) else "[redacted]"
            logging.info("duet stream request %s %s", request.path, cursor)
        if fixture and request.method == "POST" and request.path == "/v1/code/aibo/project":
            # This development host contains only the synthetic local test identity.
            (data_dir / "project-request.json").write_bytes(await request.read())
        if fixture and request.method == "POST" and request.path.startswith("/v1/code/sessions/") and request.path.endswith("/events"):
            (data_dir / "session-events-request.json").write_bytes(await request.read())
        response = await handler(request)
        logging.info("%s %s -> %s", request.method, request.path, response.status)
        return response

    backend = None
    config = None
    auth = None
    if fixture:
        app = web.Application(client_max_size=8 * 1024 * 1024, middlewares=[trace])
    else:
        if not token and not account_login:
            raise ValueError("An authentication token is required")
        from duet_server.app import create_app as engine_app
        from .native import EngineService, EngineBackend, NativeConfig, register_config
        if account_login:
            auth = AuthManager(data_dir)
        app = engine_app(data_dir / "engine", token or secrets.token_hex(32), service_factory=EngineService,
                         account_auth=auth, public_origin=public_origin if auth else None,
                         expose_legacy_sessions=not account_login,
                         public_routes={(method, path) for method in ("GET", "HEAD") for path in ("/duet/admin", "/duet/admin/", "/duet/admin/assets/admin.css", "/duet/admin/assets/admin.js")} | (OAUTH_PUBLIC_ROUTES if account_login else set()),
                         desktop_dir=data_dir.parent / "desktop")
        if protocol_diagnostics:
            app.middlewares.append(trace)
        config = ModelCatalog(data_dir) if account_login else NativeConfig(data_dir / "provider.json")
        backend = EngineBackend(app["service"], config)
        if account_login:
            auth.register(app)
            register_models(app, config)
            register_admin(app)
        else:
            register_config(app, config)
    # The retained Aibo UI uses its own conversation endpoints. In account mode
    # there must not be a second writable conversation ledger or an unadvertised
    # RPC path which bypasses that owner. Preserve old files for explicit legacy
    # mode rather than deleting or silently importing them.
    bridge = None
    if not account_login:
        rpc_backend = EngineBackend(app["service"], TextOnlyConfig(config)) if backend is not None else None
        bridge = register_rpc(app, backend=rpc_backend, store_path=data_dir / "conversations.json")
    def profile():
        result = config.public_profile() if config else {}
        if auth and auth.public_user():
            result.update(auth.public_user())
        return result
    def features():
        if not account_login:
            return {}
        ready = False
        try:
            if speech_bridge.config.load()["enabled"] and tts_bridge.refresh_capability():
                config.load()  # A selected, valid server-side chat profile is required.
                ready = True
        except (ValueError, OSError, RuntimeError):
            pass  # Unconfigured audio/model services must not expose a working Talk entry.
        return {**voice_preferences.features(public_origin.rstrip("/")),
                "conditional_mcp_directory_servers": CONNECTOR_DIRECTORY_FEATURE,
                "claudeai_flint_weir_pipit": {"defaultValue": True},
                "cai_sparkling_storm": {"defaultValue": True},
                "cai_steady_reef": {"defaultValue": True},
                "claudeai_aibo_conversation": {"defaultValue": ready},
                "claudeai_aibo_voicebox": {"defaultValue": False},
                "cai_fluffy_cocoa": {"defaultValue": False},
                "claudeai_aibo_voice_dtls_in_stun": {"defaultValue": False}}
    register_rest(app, bridge, profile=profile, features=features)
    home = register_home(app, bridge, data_dir)
    app[AIBO] = register_aibo(app, home, backend=backend, store_path=data_dir / "aibo.json")
    watch = register_task_watch(app, app[AIBO], engine=app.get("service"))
    cards = register_request_cards(app, home, app[AIBO], data_dir, watch=watch)
    channels = register_channels(app, home, app[AIBO], engine=app.get("service"))
    if backend is not None:
        home.capabilities = sorted(set(home.capabilities) | {"tasks_db"})
        app["service"].channel_context = lambda sid: channels.context_for_session(sid) if canonical_session(sid) in app[AIBO].state["sessions"] else None
    schedules = register_schedules(app, home, app[AIBO], config=config, data_dir=data_dir, engine=app.get("service"))
    tasks = register_channel_tasks(app, home, app[AIBO], schedules=schedules, engine=app.get("service"), data_dir=data_dir)
    if account_login:
        register_other_sessions(app, home, app[AIBO], watch=watch, cards=cards, tasks=tasks, advertise=True)
    register_search(app, home, app[AIBO], tasks)
    async def cleanup_channel(channel_id, ensure_idle):
        def ensure_all_idle():
            ensure_idle()
            app[AIBO].draft_cards.ensure_channel_idle(channel_id)
        await schedules.delete_channel(channel_id, ensure_all_idle)
        ensure_all_idle()
        tasks.delete_channel(channel_id)
        app[AIBO].draft_cards.delete_channel(channel_id)
    channels.schedule_cleanup = cleanup_channel
    workspace = register_workspace(app, home, app[AIBO], engine=app.get("service"), data_dir=data_dir, tasks=tasks)
    if account_login:
        register_repositories(app, home, app[AIBO], workspace, app.get("service"), data_dir)
    register_uploads(app, app.get("service"))
    speech_bridge = register_speech(app, data_dir, engine=app.get("service"))
    tts_bridge = register_tts(app, data_dir, home=home)
    voice_preferences = register_voice_preferences(app, data_dir, tts_bridge)
    if account_login:
        register_talk(app, catalog=config, speech=speech_bridge, tts=tts_bridge)
    register_usage(app, app.get("service"))
    connectors = register_connectors(app, data_dir, engine=app.get("service"))
    if account_login:
        register_native_connectors(app, connectors, public_origin, auth_manager=auth)
    register_rules(app, data_dir, app[AIBO], engine=app.get("service"))
    from .draft_senders import register_draft_senders
    register_draft_senders(app[AIBO], tasks, app.get("service"), connectors)
    if account_login:
        notifications = register_notifications(app, data_dir, auth_manager=auth)
        register_plugins(app, data_dir, app[AIBO], engine=app["service"], connectors=connectors, advertise=True)
        if backend is not None:
            register_routines(app, home, app[AIBO], schedules, app["service"], config, tasks)
        notifications.bind_sources(app[AIBO], tasks=tasks, routines=app.get(ROUTINES))

    async def telemetry_disabled(request):
        # BatchEventLoggingResponse requires both integer counts. Telemetry is
        # deliberately rejected, not collected, persisted or forwarded; report
        # that truthfully instead of an undecodable 204 or fake acceptance.
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid telemetry batch"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            return web.json_response({"error": "Telemetry events must be an array"}, status=400)
        return web.json_response({"accepted_count": 0, "rejected_count": len(body["events"])},
                                 headers={"Cache-Control": "no-store", "X-duet-telemetry": "disabled"})

    app.router.add_post("/event_logging/v2/batch", telemetry_disabled)
    app.router.add_post("/api/event_logging/v2/batch", telemetry_disabled)

    async def unknown(request):
        return web.json_response({"error": "duet compatibility route not implemented", "path": request.path}, status=501)

    app.router.add_route("*", "/{path:.*}", unknown)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"])
    parser.add_argument("--port", type=int, default=7080)
    parser.add_argument("--data-dir", default="bridge-data")
    parser.add_argument("--token-file")
    parser.add_argument("--pid-file")
    parser.add_argument("--fixture", action="store_true", help="Use explicit synthetic protocol responses for local testing")
    parser.add_argument("--legacy-ssh", action="store_true", help="Use the earlier private SSH service-token mode")
    parser.add_argument("--public-url", help="External HTTPS origin, for example https://duet.example.com")
    parser.add_argument("--protocol-diagnostics", action="store_true", help="Log message stream routes and non-secret cursors only")
    args = parser.parse_args()
    os.umask(0o077)
    token = None
    if not args.fixture and args.legacy_ssh:
        if not args.token_file:
            parser.error("--token-file is required outside explicit fixture mode")
        path = Path(args.token_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            with path.open("x", encoding="ascii") as output:
                output.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32 or any(c.isspace() for c in token):
            parser.error("Invalid local authentication token")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from duet_server.__main__ import owned_pidfile
    with owned_pidfile(args.pid_file):
        public_origin = args.public_url or f"http://{args.host}:{args.port}"
        web.run_app(create_app(args.data_dir, token=token, fixture=args.fixture, account_login=not args.fixture and not args.legacy_ssh, public_origin=public_origin, protocol_diagnostics=args.protocol_diagnostics), host=args.host, port=args.port,
                    access_log=None, print=lambda _: None)


if __name__ == "__main__":
    main()
