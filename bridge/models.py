"""Server-owned model profiles. HTTP responses never contain provider secrets.

One catalog instance coordinates writers inside the server process. Every read
returns a fresh snapshot; a running message never observes a later selection.
The enclosing application must authenticate/authorize every registered route.
"""
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from uuid import uuid4

from aiohttp import web

from duet_server.providers import validate_config


class ModelNotFound(KeyError):
    pass


class ModelConflict(ValueError):
    pass


class ModelNotConfigured(RuntimeError):
    pass


class CatalogStorageError(RuntimeError):
    pass


class ModelCatalog:
    CONFIG_FIELDS = frozenset(("protocol", "base_url", "api_key", "model", "max_tokens", "system_prompt", "allow_tools", "auto_approve",
                               "prompt_cache_enabled", "stream_usage_enabled", "api_mode", "reasoning_effort",
                               "prompt_cache_mode", "prompt_cache_ttl"))

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "models.json"
        self._lock = threading.RLock()
        with self._lock:
            if self.path.exists():
                self._read()
                os.chmod(self.path, 0o600)
            else:
                legacy = self.data_dir / "provider.json"
                if legacy.exists():
                    if legacy.is_symlink() or not legacy.is_file():
                        raise CatalogStorageError("Legacy model configuration is not a regular file")
                    try:
                        original = json.loads(legacy.read_text(encoding="utf-8"))
                        provider = validate_config(original)
                        profile = self._profile({key: provider[key] for key in ("display_name", "appearance") if key in provider})
                        # Retain legacy provider extensions and all secrets. Only
                        # account appearance moves out of the model profile.
                        provider.pop("display_name", None)
                        provider.pop("appearance", None)
                    except (ValueError, TypeError, KeyError, OSError) as error:
                        raise CatalogStorageError("Legacy model configuration could not be migrated; original file retained") from error
                    state = self._empty()
                    state.update(current_id="default", profile=profile)
                    state["models"]["default"] = {"id": "default", "label": "默认模型", "config": provider}
                    self._write(state)
                    # Never delete the source: an interrupted migration remains
                    # recoverable and keys are not silently discarded.
                    os.chmod(legacy, 0o600)

    @staticmethod
    def _empty():
        return {"version": 1, "current_id": None, "profile": {"display_name": "duet", "appearance": "system"}, "models": {}}

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            raise ValueError("Model id must be 1–80 letters, digits, dots, underscores or hyphens")
        return value

    @staticmethod
    def _label(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError("Model label must contain 1–200 printable characters")
        return value.strip()

    @staticmethod
    def _profile(value, base=None):
        if not isinstance(value, dict) or set(value) - {"display_name", "appearance"}:
            raise ValueError("Unknown account profile field")
        result = dict(base or {"display_name": "duet", "appearance": "system"})
        result.update(value)
        name = result["display_name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("display_name must contain 1–80 characters")
        if result["appearance"] not in ("system", "light", "dark"):
            raise ValueError("appearance must be system, light or dark")
        result["display_name"] = name.strip()
        return result

    def _read(self):
        if not self.path.exists():
            return self._empty()
        if self.path.is_symlink() or not self.path.is_file():
            raise CatalogStorageError("Model catalog is not a regular file")
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("models"), dict):
                raise ValueError("invalid catalog version")
            state["profile"] = self._profile(state["profile"])
            current = state["current_id"]
            if (state["models"] and current not in state["models"]) or (not state["models"] and current is not None):
                raise ValueError("invalid current model")
            for ident, record in state["models"].items():
                self._id(ident)
                if record["id"] != ident:
                    raise ValueError("invalid model identity")
                record["label"] = self._label(record["label"])
                record["config"] = validate_config(record["config"])
            return state
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CatalogStorageError("Model catalog cannot be read; existing data retained") from error

    def _write(self, state):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir,
                                             prefix=".models-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(state, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600)
            if os.name == "posix":
                descriptor = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _public(record, current):
        config = record["config"]
        return {"id": record["id"], "label": record["label"], "protocol": config["protocol"],
                "model": config["model"], "base_url": config["base_url"],
                "current": record["id"] == current, "has_key": bool(config.get("api_key")),
                **{field: config[field] for field in ("max_tokens", "allow_tools", "auto_approve", "prompt_cache_enabled", "stream_usage_enabled",
                                                     "api_mode", "reasoning_effort", "prompt_cache_mode", "prompt_cache_ttl")}}

    @property
    def current_id(self):
        with self._lock:
            return self._read()["current_id"]

    def load(self):
        with self._lock:
            state = self._read()
            if state["current_id"] is None:
                raise ModelNotConfigured("Configure and select a model on the server first")
            config = copy.deepcopy(validate_config(state["models"][state["current_id"]]["config"]))
            config["_model_profile_id"] = state["current_id"]
            return config

    def public_profile(self):
        with self._lock:
            return copy.deepcopy(self._read()["profile"])

    def update_profile(self, data):
        """Account appearance is global, not tied to the selected model."""
        with self._lock:
            state = self._read()
            profile = self._profile(data, state["profile"])
            state["profile"] = profile
            self._write(state)
            return copy.deepcopy(profile)

    def snapshot(self):
        """One consistent public list/current selection for HTTP consumers."""
        with self._lock:
            state = self._read()
            items = [self._public(record, state["current_id"]) for record in state["models"].values()]
            return {"models": sorted(items, key=lambda item: (item["label"].casefold(), item["id"])),
                    "current_id": state["current_id"]}

    def list(self):
        return self.snapshot()["models"]

    def get(self, ident):
        self._id(ident)
        with self._lock:
            state = self._read()
            if ident not in state["models"]:
                raise ModelNotFound(ident)
            return self._public(state["models"][ident], state["current_id"])

    def upsert(self, ident, data, *, create_only=False):
        self._id(ident)
        if not isinstance(data, dict) or set(data) - self.CONFIG_FIELDS - {"label"}:
            raise ValueError("Unknown model profile field")
        with self._lock:
            state = self._read()
            previous = state["models"].get(ident)
            if previous and create_only:
                raise ModelConflict("Model already exists")
            config = copy.deepcopy(previous["config"]) if previous else {}
            config.update({key: copy.deepcopy(value) for key, value in data.items() if key != "label"})
            config = validate_config(config)
            # These bounds prevent large secret/prompt payloads from turning a
            # catalog update into unbounded storage or memory use.
            if len(config["api_key"]) > 65536 or len(config["system_prompt"]) > 200000:
                raise ValueError("Model key or system prompt exceeds size limit")
            label = self._label(data.get("label", previous["label"] if previous else ident))
            record = {"id": ident, "label": label, "config": config}
            state["models"][ident] = record
            if state["current_id"] is None:
                state["current_id"] = ident
            self._write(state)
            return self._public(record, state["current_id"])

    def select(self, ident):
        self._id(ident)
        with self._lock:
            state = self._read()
            if ident not in state["models"]:
                raise ModelNotFound(ident)
            state["current_id"] = ident
            self._write(state)
            return self._public(state["models"][ident], ident)

    def delete(self, ident):
        self._id(ident)
        with self._lock:
            state = self._read()
            if ident not in state["models"]:
                raise ModelNotFound(ident)
            if ident == state["current_id"]:
                raise ModelConflict("Select another model before deleting the active profile")
            del state["models"][ident]
            self._write(state)
            return {"deleted": True}


def register_models(app, catalog):
    """Register behind the application's account authentication middleware."""
    async def handle(request):
        try:
            ident = request.match_info.get("id")
            if request.method == "GET":
                return web.json_response(catalog.get(ident) if ident else catalog.snapshot(), headers={"Cache-Control": "no-store"})
            if request.method == "DELETE":
                if request.can_read_body and await request.json() != {}:
                    raise ValueError("Delete accepts no fields")
                result = catalog.delete(ident)
            elif request.method == "PUT":
                result = catalog.upsert(ident, await request.json())
            elif ident:
                if request.can_read_body and await request.json() != {}:
                    raise ValueError("Selection accepts no fields")
                result = catalog.select(ident)
            else:
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("Model profile must be an object")
                data = dict(data)
                ident = data.pop("id", uuid4().hex)
                result = catalog.upsert(ident, data, create_only=True)
                return web.json_response(result, status=201, headers={"Cache-Control": "no-store"})
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except ModelNotFound:
            return web.json_response({"error": "Model not found"}, status=404)
        except ModelConflict as error:
            return web.json_response({"error": str(error)}, status=409)
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Invalid model profile"}, status=400)
        except (CatalogStorageError, OSError):
            # Storage/JSON/validation exceptions must not reflect key material.
            return web.json_response({"error": "Model catalog storage unavailable"}, status=503)

    app.router.add_get("/duet/models", handle)
    app.router.add_post("/duet/models", handle)
    app.router.add_get("/duet/models/{id}", handle)
    app.router.add_put("/duet/models/{id}", handle)
    app.router.add_delete("/duet/models/{id}", handle)
    app.router.add_post("/duet/models/{id}/select", handle)
    return catalog
