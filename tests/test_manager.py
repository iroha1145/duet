"""Real loopback authentication/model API tests; no external provider calls."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from aiohttp import web
from bridge.auth import AuthManager
from bridge.models import ModelCatalog, register_models

spec = importlib.util.spec_from_file_location("manager", Path(__file__).resolve().parents[1] / "scripts/manage.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ManagerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run_server)
        self.thread.start()
        self.assertTrue(self.ready.wait(30))
        self.client = manager.Client(self.port)

    def run_server(self):
        asyncio.set_event_loop(self.loop)
        async def start():
            self.auth = AuthManager(self.temp.name)
            self.auth.initialize("fixture-owner", "fixture-password-only")
            self.catalog = ModelCatalog(self.temp.name)
            @web.middleware
            async def require_auth(request, handler):
                if not self.auth.is_public_request(request) and await self.auth.authenticate(request) is None:
                    raise web.HTTPUnauthorized()
                return await handler(request)
            app = web.Application(middlewares=[require_auth])
            self.auth.register(app)
            register_models(app, self.catalog)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.ready.set()
        self.loop.run_until_complete(start())
        self.loop.run_forever()
        self.loop.run_until_complete(self.runner.cleanup())
        self.auth.close()
        self.loop.close()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(30)
        self.temp.cleanup()

    def login(self):
        with patch.object(manager, "ask", return_value="fixture-owner"), patch.object(manager.getpass, "getpass", return_value="fixture-password-only"):
            self.client.login()

    def edit(self, answers, key=""):
        with patch("builtins.input", side_effect=answers), patch.object(manager.getpass, "getpass", return_value=key):
            manager.edit_model(self.client)

    def test_authenticated_create_edit_keep_clear_switch_and_revoke(self):
        with self.assertRaises(manager.OperatorError):
            self.client.request("GET", "/duet/models")
        self.login()
        self.edit(["primary", "主模型", "openai", "responses", "https://provider.example/v1", "fixture-model", "4096", "high", "explicit", "30m"], "fixture-provider-key")
        self.assertEqual(self.catalog.load()["api_key"], "fixture-provider-key")
        self.assertFalse(self.catalog.load()["auto_approve"])
        self.edit(["primary", "", "", "", "", "changed-model", "", "", "", ""])
        self.assertEqual(self.catalog.load()["model"], "changed-model")
        self.assertEqual(self.catalog.load()["api_key"], "fixture-provider-key")
        self.assertNotIn("fixture-provider-key", str(self.client.request("GET", "/duet/models")))
        self.edit(["second", "第二", "anthropic", "https://provider.example", "fixture-anthropic", "8192"], "")
        self.client.request("POST", "/duet/models/second/select", {})
        self.assertEqual(self.client.request("GET", "/duet/models")["current_id"], "second")
        self.client.request("PUT", "/duet/models/primary", {"api_key": ""})
        self.assertFalse(self.client.request("GET", "/duet/models/primary")["has_key"])
        token = self.client.token
        self.client.logout()
        self.client.token = token
        with self.assertRaises(manager.OperatorError):
            self.client.request("GET", "/duet/models")

    def test_existing_auto_approve_is_not_changed_by_model_edit(self):
        self.login()
        self.client.request("PUT", "/duet/models/primary", {"model": "fixture", "base_url": "https://provider.example/v1", "auto_approve": True})
        self.edit(["primary", "", "", "", "", "changed", "", ""])
        self.assertTrue(self.catalog.load()["auto_approve"])
        self.client.logout()


class SettingsTests(unittest.TestCase):
    @unittest.skipUnless(__import__("os").name == "posix", "Linux installation paths")
    def test_root_canonicalization_keeps_exact_service_ownership(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install"
            units = Path(directory) / "config/systemd/user"
            units.mkdir(parents=True)
            (units / "fixture.service").write_text("# duet-install-root: " + str(root) + "\n")
            base = {"port": 48766, "service": "fixture", "public_url": "https://duet.example.com", "no_systemd": False, "desktop": False}
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(directory) / "config")}), patch.object(manager.subprocess, "run") as run:
                for spelling in (str(root), str(root) + "/", str(root.parent) + "/tmp/../install"):
                    normalized = manager.validate({**base, "root": spelling})
                    self.assertEqual(normalized["root"], str(root))
                    manager.service({**base, "root": spelling}, "status")
                self.assertEqual(run.call_count, 3)
                # No substring or alias matching: a different root is still refused.
                with self.assertRaises(manager.OperatorError):
                    manager.service({**base, "root": str(root) + "-other"}, "start")
                alias = Path(directory) / "alias"
                root.mkdir()
                alias.symlink_to(root, target_is_directory=True)
                with self.assertRaises(manager.OperatorError):
                    manager.service({**base, "root": str(alias)}, "start")
                (units / "fixture.service").unlink()
                (units / "fixture.service").symlink_to(Path(directory) / "foreign.service")
                with self.assertRaises(manager.OperatorError):
                    manager.service({**base, "root": str(root)}, "stop")

    def test_private_settings_and_no_secret_fields(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = {"root": "/tmp/test-install", "port": 48766, "service": "duet-server", "public_url": "https://duet.example.com", "no_systemd": False, "desktop": False}
            manager.save_settings(path, manager.validate(settings))
            self.assertEqual(set(__import__("json").loads(path.read_text())), set(settings))
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_update_stops_and_backs_up_before_upgrade_without_changing_data(self):
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/tasks.json").write_text('{"fixture_task":"retained"}')
            settings = {"root": str(root), "port": 48766, "service": "fixture", "public_url": "https://duet.example.com", "no_systemd": False, "desktop": False}
            events = []
            def command(args, **kwargs):
                if "--check" in args:
                    events.append("check")
                else:
                    backups = list(root.glob("backup-data-*.tar.gz"))
                    self.assertEqual(len(backups), 1)
                    with tarfile.open(backups[0]) as archive:
                        self.assertEqual(archive.extractfile("data/tasks.json").read(), b'{"fixture_task":"retained"}')
                    events.append("upgrade")
            with patch.object(manager.subprocess, "run", side_effect=command), patch.object(manager, "service", side_effect=lambda *args: events.append("stop")), patch.object(manager, "ask", return_value="UPDATE"):
                manager.update(settings)
            self.assertEqual(events, ["check", "stop", "upgrade"])
            self.assertEqual((root / "data/tasks.json").read_text(), '{"fixture_task":"retained"}')

    def test_secret_rejects_getpass_plaintext_fallback(self):
        with patch.object(manager.getpass, "getpass", side_effect=manager.getpass.GetPassWarning("fixture unsupported terminal")):
            with self.assertRaises(manager.OperatorError):
                manager.secret("hidden: ")

    def test_refuse_redirect_before_forwarding_credentials(self):
        with self.assertRaises(manager.OperatorError):
            manager.NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.example")


if __name__ == "__main__":
    unittest.main()
