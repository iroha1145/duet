"""Owner-controlled Firebase configuration and bounded HTTP v1 transport.

Only public Android options leave the server. Google hosts are fixed here;
tests inject a request function, never an endpoint through client configuration.
"""
import asyncio
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

import aiohttp


TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_ORIGIN = "https://fcm.googleapis.com"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
PACKAGE = "com.duet"
CONFIG_LIMIT = 64 * 1024
RESPONSE_LIMIT = 32 * 1024


class FCMError(Exception):
    def __init__(self, code, *, retryable=False, retry_after=0, invalid_token=False):
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.invalid_token = invalid_token
        super().__init__(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration field")
        result[key] = value
    return result


def _text(value, pattern, maximum):
    return isinstance(value, str) and len(value) <= maximum and re.fullmatch(pattern, value) is not None


def _signer(info):
    # The feature remains disabled without configuration. There is no alternate
    # crypto implementation if its pinned, official dependency is unavailable.
    try:
        from google.oauth2 import service_account
    except ImportError as error:
        raise FCMError("fcm_dependency_unavailable") from error
    try:
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
        signer = credentials.signer
        if not 256 <= len(signer.sign(b"duet-fcm-configuration-validation")) <= 1024:
            raise ValueError("An RSA key of at least 2048 bits is required")
        return signer
    except (ValueError, TypeError) as error:
        raise FCMError("invalid_fcm_service_account") from error


@dataclass(frozen=True)
class FCMConfig:
    generation: str
    enabled: bool = False
    android: dict | None = None
    service_account: dict | None = field(default=None, repr=False)
    signer: object = field(default=None, repr=False, compare=False)

    @property
    def configured(self):
        return self.android is not None and self.service_account is not None

    def public(self):
        return {"enabled": self.enabled, "configured": self.configured,
                "generation": self.generation, "android": self.android}


def validate_config(body, *, generation):
    if (not isinstance(body, dict) or set(body) != {"enabled", "android", "service_account"}
            or type(body["enabled"]) is not bool):
        raise FCMError("invalid_fcm_configuration")
    android, account = body["android"], body["service_account"]
    if android is None and account is None and not body["enabled"]:
        return FCMConfig(generation)
    if not isinstance(android, dict) or set(android) != {"packageName", "projectId", "projectNumber", "appId", "apiKey"}:
        raise FCMError("invalid_fcm_android_options")
    project = android["projectId"]
    number = android["projectNumber"]
    if (android["packageName"] != PACKAGE or not _text(project, r"[a-z][a-z0-9-]{4,28}[a-z0-9]", 30)
            or not _text(number, r"[1-9][0-9]{4,31}", 32)
            or not _text(android["appId"], r"1:" + re.escape(str(number)) + r":android:[a-f0-9]{16,64}", 120)
            or not _text(android["apiKey"], r"AIza[A-Za-z0-9_-]{35}", 39)):
        raise FCMError("invalid_fcm_android_options")
    required = {"type", "project_id", "private_key_id", "private_key", "client_email", "token_uri"}
    optional = {"client_id", "auth_uri", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"}
    if not isinstance(account, dict) or not required <= set(account) or set(account) - required - optional:
        raise FCMError("invalid_fcm_service_account")
    if (account["type"] != "service_account" or account["project_id"] != project
            or account["token_uri"] != TOKEN_URL or account.get("universe_domain", "googleapis.com") != "googleapis.com"
            or not _text(account["private_key_id"], r"[a-fA-F0-9]{16,128}", 128)
            or not _text(account["client_email"], r"[a-zA-Z0-9._-]+@" + re.escape(project) + r"\.iam\.gserviceaccount\.com", 254)
            or not isinstance(account["private_key"], str) or not 1000 <= len(account["private_key"]) <= 16384):
        raise FCMError("invalid_fcm_service_account")
    fixed = {"auth_uri": "https://accounts.google.com/o/oauth2/auth",
             "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs"}
    if any(key in account and account[key] != value for key, value in fixed.items()):
        raise FCMError("invalid_fcm_service_account")
    if "client_id" in account and not _text(account["client_id"], r"[0-9]{1,64}", 64):
        raise FCMError("invalid_fcm_service_account")
    if "client_x509_cert_url" in account:
        from urllib.parse import quote
        expected = "https://www.googleapis.com/robot/v1/metadata/x509/" + quote(account["client_email"], safe="")
        if account["client_x509_cert_url"] != expected:
            raise FCMError("invalid_fcm_service_account")
    canonical = {key: account[key] for key in required}
    canonical["universe_domain"] = "googleapis.com"
    signer = _signer(canonical)
    return FCMConfig(generation, body["enabled"], dict(android), canonical, signer)


class FCMConfigStore:
    def __init__(self, root):
        self.root = Path(root).absolute() / "fcm"
        if self.root.is_symlink():
            raise ValueError("FCM directory must not be a link")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "config.json"
        self._cache = None
        self._digest = None
        self.read()

    def read(self):
        if self.path.is_symlink():
            raise FCMError("fcm_configuration_unavailable")
        if not self.path.exists():
            self._cache, self._digest = FCMConfig("unconfigured"), None
            return self._cache
        info = self.path.lstat()
        if self.path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > CONFIG_LIMIT:
            raise FCMError("fcm_configuration_unavailable")
        with self.path.open("rb") as source:
            raw = source.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            raise FCMError("fcm_configuration_unavailable")
        digest = hashlib.sha256(raw).digest()
        if digest == self._digest:
            return self._cache
        try:
            document = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(document, dict) or set(document) != {"version", "generation", "enabled", "android", "service_account"}:
                raise ValueError("Invalid stored configuration")
            version = document.pop("version")
            if type(version) is not int or version != 1 or not _text(document["generation"], r"[a-f0-9]{32}", 32):
                raise ValueError("Invalid stored configuration")
            generation = document.pop("generation")
            config = validate_config(document, generation=generation)
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise FCMError("fcm_configuration_unavailable") from error
        self._cache, self._digest = config, digest
        return config

    def write(self, body):
        if body == {"enabled": False}:
            previous = self.read()
            body = {"enabled": False, "android": previous.android, "service_account": previous.service_account}
        config = validate_config(body, generation=secrets.token_hex(16))
        document = {"version": 1, "generation": config.generation, "enabled": config.enabled,
                    "android": config.android, "service_account": config.service_account}
        raw = json.dumps(document, sort_keys=True, allow_nan=False).encode()
        if len(raw) > CONFIG_LIMIT:
            raise FCMError("invalid_fcm_configuration")
        if self.path.exists():
            self.read()  # Recheck path integrity before replacing private data.
        temporary = self.root / (".config-" + secrets.token_hex(16))
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        # Future reads compare the actual disk content, including after a
        # cancellation or an error immediately after atomic publication.
        return self.read()


class FCMTransport:
    def __init__(self, *, request=None, clock=time.time):
        self.clock, self._request_override = clock, request
        self._http = None
        self._token = None
        self._token_generation = None
        self._expires = 0
        self._lock = asyncio.Lock()

    async def _request(self, url, *, headers=None, data=None, json_body=None):
        if self._request_override is not None:
            try:
                result = await asyncio.wait_for(self._request_override(url, headers=headers, data=data, json_body=json_body), timeout=15)
                if len(result[2]) > RESPONSE_LIMIT:
                    raise FCMError("fcm_response_too_large", retryable=True)
                return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise FCMError("fcm_transport_unavailable", retryable=True) from error
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=5),
                                               trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self._http.post(url, headers=headers, data=data, json=json_body, allow_redirects=False) as response:
                raw = bytearray()
                async for part in response.content.iter_chunked(4096):
                    raw.extend(part)
                    if len(raw) > RESPONSE_LIMIT:
                        raise FCMError("fcm_response_too_large", retryable=True)
                return response.status, dict(response.headers), bytes(raw)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise FCMError("fcm_transport_unavailable", retryable=True) from error

    async def _access_token(self, config):
        async with self._lock:
            if self._token_generation == config.generation and self._token and self._expires > self.clock() + 60:
                return self._token
            from google.auth import jwt
            now = int(self.clock())
            assertion = jwt.encode(config.signer, {"iss": config.service_account["client_email"],
                "scope": FCM_SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).decode("ascii")
            status, headers, raw = await self._request(TOKEN_URL,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            try:
                body = json.loads(raw)
                token, expires = body["access_token"], body["expires_in"]
                if (status != 200 or not _text(token, r"[!-~]{1,4096}", 4096)
                        or type(expires) is not int or not 60 <= expires <= 3600
                        or body.get("token_type", "Bearer").lower() != "bearer"):
                    raise ValueError("Invalid OAuth token response")
            except (ValueError, KeyError, TypeError, UnicodeError, AttributeError) as error:
                raise FCMError("fcm_oauth_failed", retryable=status == 429 or status >= 500) from error
            self._token, self._token_generation, self._expires = token, config.generation, self.clock() + expires
            return token

    async def send(self, config, token, data, *, ttl, collapse_key, guard, priority="HIGH"):
        if not config.enabled or not config.configured:
            raise FCMError("push_transport_not_configured")
        bearer = await self._access_token(config)
        # OAuth exchange and a contended credential lock can suspend. Do not
        # send to a revoked/rotated device or a resolved approval afterwards.
        guard()
        message = {"token": token, "data": data, "android": {"priority": priority,
                   "ttl": str(max(0, min(int(ttl), 86400))) + "s", "collapse_key": collapse_key}}
        if len(json.dumps(message, ensure_ascii=False).encode()) > 4096:
            raise FCMError("fcm_payload_too_large")
        url = FCM_ORIGIN + "/v1/projects/" + config.android["projectId"] + "/messages:send"
        status, headers, raw = await self._request(url, headers={"Authorization": "Bearer " + bearer},
                                                  json_body={"message": message})
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            body = {}
        if 200 <= status < 300 and isinstance(body, dict) and isinstance(body.get("name"), str) and len(body["name"]) <= 512:
            return body["name"]
        details = body.get("error", {}).get("details", []) if isinstance(body, dict) and isinstance(body.get("error"), dict) else []
        codes = {item["errorCode"] for item in details if isinstance(item, dict) and isinstance(item.get("errorCode"), str)} if isinstance(details, list) else set()
        if status == 401:
            self._token, self._expires = None, 0
        try:
            retry_after = max(0, min(int(headers.get("Retry-After", "0")), 3600))
        except (ValueError, TypeError):
            try:
                retry_after = max(0, min(int(parsedate_to_datetime(headers.get("Retry-After", "")).timestamp() - self.clock()), 3600))
            except (ValueError, TypeError, AttributeError, OverflowError):
                retry_after = 0
        if "UNREGISTERED" in codes:
            raise FCMError("fcm_unregistered", invalid_token=True)
        if "SENDER_ID_MISMATCH" in codes:
            raise FCMError("fcm_sender_mismatch", invalid_token=True)
        raise FCMError("fcm_send_rejected", retryable=status in (401, 429) or status >= 500, retry_after=retry_after)

    async def close(self):
        if self._http is not None:
            await self._http.close()
        self._token = None
