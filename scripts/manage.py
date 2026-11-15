"""Interactive local operator; secrets remain in memory and travel only to loopback."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import warnings
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

SOURCE = Path(__file__).resolve().parent.parent


class OperatorError(Exception):
    pass


def ask(label, default=""):
    value = input(f"{label}" + (f" [{default}]" if default != "" else "") + ": ").strip()
    return value or default


def secret(label):
    # getpass may otherwise fall back to echoing stdin on an unsuitable terminal.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(label)
        except getpass.GetPassWarning:
            raise OperatorError("无法隐藏输入；请在交互终端运行") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperatorError("本地 API 重定向已拒绝")


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{int(port)}"
        self.token = None
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        try:
            with self.opener.open(Request(self.base + path, payload, headers, method=method), timeout=30) as response:
                raw = response.read(1024 * 1024)
                return json.loads(raw) if raw else None
        except HTTPError as error:
            # Do not reflect upstream bodies which could contain sensitive values.
            raise OperatorError(f"本地 API 返回 HTTP {error.code}；请检查输入或服务状态") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise OperatorError("无法读取本地 API；请检查服务、端口和登录状态") from None

    def login(self):
        username = ask("服务器账号")
        password = secret("服务器密码（隐藏）: ")
        result = self.request("POST", "/duet/auth/login", {
            "username": username, "password": password, "device_name": "duet local manager"})
        self.token = result["access_token"]

    def logout(self):
        if self.token:
            try:
                self.request("POST", "/duet/auth/logout", {})
            finally:
                self.token = None


def edit_model(client):
    ident = ask("模型档案 ID（新增用新 ID；字母/数字/点/下划线/横线）")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", ident):
        raise OperatorError("模型档案 ID 无效")
    snapshot = client.request("GET", "/duet/models")
    existing = next((item for item in snapshot["models"] if item["id"] == ident), {})
    data = {"label": ask("显示名称", existing.get("label", ident))}
    protocol = ask("协议 openai / anthropic", existing.get("protocol", "openai"))
    data["protocol"] = protocol
    data["api_mode"] = ask("API 模式 chat_completions / responses", existing.get("api_mode", "chat_completions")) if protocol == "openai" else "chat_completions"
    data["base_url"] = ask("API 地址（如 https://api.example.com/v1）", existing.get("base_url", ""))
    data["model"] = ask("模型名称", existing.get("model", ""))
    data["max_tokens"] = int(ask("最大输出 token", str(existing.get("max_tokens", 8192))))
    effort = ask("推理强度 default/none/minimal/low/medium/high/xhigh/max", existing.get("reasoning_effort") or "default") if protocol == "openai" else "default"
    data["reasoning_effort"] = None if effort == "default" else effort
    responses = data["api_mode"] == "responses"
    cache = ask("Responses 缓存模式 default/implicit/explicit", existing.get("prompt_cache_mode") or "default") if responses else "default"
    ttl = ask("Responses 缓存有效期 default/30m/12h", existing.get("prompt_cache_ttl") or "default") if responses else "default"
    data["prompt_cache_mode"] = None if cache == "default" else cache
    data["prompt_cache_ttl"] = None if ttl == "default" else ttl
    key = secret("API key（隐藏；直接回车保留旧 key）: ")
    if key:
        data["api_key"] = key
    if not existing:
        data["auto_approve"] = False
    # PUT merges omitted fields with existing values on the server.
    client.request("PUT", "/duet/models/" + quote(ident, safe=""), data)
    saved = client.request("GET", "/duet/models/" + quote(ident, safe=""))
    if any(saved.get(field) != data[field] for field in ("model", "protocol", "api_mode", "base_url")):
        raise OperatorError("保存后的 API 读取与输入不一致")
    print(f"已保存并重新读取：{saved['id']} / {saved['model']} / key={'已配置' if saved['has_key'] else '未配置'}")


def models(settings):
    client = Client(settings["port"])
    try:
        client.login()
        while True:
            snapshot = client.request("GET", "/duet/models")
            for item in snapshot["models"]:
                print(f"{'*' if item['current'] else ' '} {item['id']}  {item['label']}  {item['protocol']}/{item['api_mode']}  {item['model']}")
            action = ask("1 新增/修改  2 切换  3 明确清除 key  0 返回", "0")
            if action == "0":
                return
            if action == "1":
                edit_model(client)
            elif action in ("2", "3"):
                ident = ask("模型档案 ID")
                path = "/duet/models/" + quote(ident, safe="")
                if action == "2":
                    client.request("POST", path + "/select", {})
                    if client.request("GET", "/duet/models")["current_id"] != ident:
                        raise OperatorError("切换后读取不一致")
                    print("当前模型已切换；之后的新请求生效")
                elif ask("输入 CLEAR 确认清除该档案的 API key") == "CLEAR":
                    client.request("PUT", path, {"api_key": ""})
                    if client.request("GET", path)["has_key"]:
                        raise OperatorError("清除后读取不一致")
                    print("API key 已清除并重新读取确认")
    finally:
        try:
            client.logout()
        except OperatorError:
            print("本次管理会话撤销失败；可在网页设备管理中撤销", file=sys.stderr)


def validate(settings):
    if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", settings["root"]):
        raise OperatorError("安装目录必须为无空格 ASCII 绝对路径")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", settings["service"]):
        raise OperatorError("服务名称无效")
    if type(settings["port"]) is not int or not 1024 <= settings["port"] <= 65535:
        raise OperatorError("端口须为 1024..65535")
    if type(settings["no_systemd"]) is not bool or type(settings["desktop"]) is not bool:
        raise OperatorError("安装选项无效")
    return settings


def save_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OperatorError("拒绝写入配置符号链接")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(settings, output, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def installer_args(settings, upgrade=False):
    args = ["bash", str(SOURCE / "installer/install-server.sh"), "--root", settings["root"],
            "--source", str(SOURCE), "--public-url", settings["public_url"],
            "--port", str(settings["port"]), "--service", settings["service"]]
    for field, flag in (("no_systemd", "--no-systemd"), ("desktop", "--desktop")):
        if settings[field]:
            args.append(flag)
    if upgrade:
        args.append("--upgrade")
    return args


def service(settings, action):
    if settings["no_systemd"]:
        raise OperatorError("此安装为前台模式：使用安装器打印的命令启动，以 Ctrl-C 停止；无 systemd 状态管理")
    unit = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user" / (settings["service"] + ".service")
    marker = "# duet-install-root: " + settings["root"]
    if unit.is_symlink() or not unit.is_file() or marker not in unit.read_text().splitlines():
        raise OperatorError("未找到属于此安装的用户服务")
    subprocess.run(["systemctl", "--user", action, settings["service"] + ".service"], check=True)


def install(path, previous):
    defaults = previous or {"root": str(Path.home() / ".local/share/duet"), "public_url": "https://duet.example.com", "port": 48766, "service": "duet-server", "desktop": False, "no_systemd": False}
    settings = validate({"root": ask("安装目录", defaults["root"]), "public_url": ask("公网 HTTPS origin", defaults["public_url"]),
        "port": int(ask("本地端口", str(defaults["port"]))), "service": ask("用户服务名称", defaults["service"]),
        "no_systemd": ask("无 systemd，仅准备文件？y/n", "y" if defaults["no_systemd"] else "n") == "y",
        "desktop": ask("安装可选桌面 unit（须已有系统依赖）？y/n", "y" if defaults["desktop"] else "n") == "y"})
    args = installer_args(settings)
    username = ask("首次账号名（已有账号直接回车）")
    if username:
        args.extend(["--username", username])
    subprocess.run(args + ["--check"], check=True)
    subprocess.run(args, check=True)
    save_settings(path, settings)
    print("安装参数已保存（不含密码/key）；使用 models 配置模型")


def update(settings):
    args = installer_args(settings, upgrade=True)
    subprocess.run(args + ["--check"], check=True)
    print("更新当前 checkout 版本。请先结束任务；更新会停服、备份 data，再调用已有升级器。")
    if settings["no_systemd"]:
        raise OperatorError("前台模式请按 README 停服备份后手动运行 installer --upgrade；管理器不会猜测进程归属")
    if ask("确认任务已结束并停服升级，输入 UPDATE") != "UPDATE":
        return
    service(settings, "stop")
    root = Path(settings["root"])
    backup = root / ("backup-data-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".tar.gz")
    # Exclusive create; stopped service provides a consistent data snapshot.
    with backup.open("xb") as output:
        os.chmod(backup, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz", dereference=False) as archive:
            archive.add(root / "data", arcname="data", recursive=True)
    print("数据备份：" + str(backup))
    subprocess.run(args, check=True)
    save_message = "已升级；保留原有账号、数据及旧 release。"
    print(save_message)


def main():
    parser = argparse.ArgumentParser(description="duet 中文一键安装/管理；仅使用本机 API，不保存登录 token 或 provider key")
    parser.add_argument("action", nargs="?", default="menu", choices=("menu", "install", "models", "status", "start", "stop", "update"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "duet-manager/config.json", help="安装参数文件（不含秘密）")
    options = parser.parse_args()
    os.umask(0o077)
    if sys.platform != "linux" or sys.version_info < (3, 11):
        raise OperatorError("请在 Linux 上用 Python 3.11+ 运行")
    while True:
        settings = validate(json.loads(options.config.read_text())) if options.config.exists() else None
        action = options.action
        if action == "menu":
            action = {"1": "install", "2": "models", "3": "status", "4": "start", "5": "stop", "6": "update", "0": "exit"}.get(ask("1 安装  2 模型配置  3 状态  4 启动  5 停止  6 升级当前 checkout  0 退出", "0"))
        if action == "exit":
            return
        if action == "install":
            install(options.config, settings)
        elif action in ("models", "status", "start", "stop", "update"):
            if not settings:
                raise OperatorError("请先运行 install；已有安装可按 README 创建无秘密的安装参数文件")
            if action == "models":
                models(settings)
            elif action == "update":
                update(settings)
            else:
                service(settings, action)
        if options.action != "menu":
            return


if __name__ == "__main__":
    try:
        main()
    except (OperatorError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        # Values/errors from JSON/files/subprocess could contain credentials: never dump them.
        print("操作未完成：" + (str(error) if isinstance(error, OperatorError) else "参数、文件或命令检查失败；原有数据已保留"), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n已取消", file=sys.stderr)
        sys.exit(130)
