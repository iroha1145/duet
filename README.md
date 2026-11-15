# duet 个人自托管服务器

一个账号、多设备同步的 Linux 服务端。此仓库包含服务端源码、管理页面、安装器与中文命令菜单，不包含 APK、手机源码、用户数据、账号或 provider key。

## 一键入口

准备 Linux、Git、Python 3.11+、venv/pip；可直接 root 登录，或使用普通服务用户。此仓库已公开，使用 HTTPS 拉取无需 GitHub 账号、登录或 token：

```bash
git clone https://github.com/iroha1145/duet.git "$HOME/duet-source"
cd "$HOME/duet-source"
bash duet.sh
```

先拉取完整仓库、审阅脚本，再执行 `bash duet.sh`。`duet.sh` 是本地管理入口，依赖同仓库的 `scripts/manage.py`、安装器与服务端源码；不能只下载单个脚本或使用 `curl .../duet.sh | bash` 安装。仓库目录不要放进安装目录。公开的是服务端源码与安装/管理脚本，仓库不提供 APK 或手机客户端下载。

选择“1 安装”，按提示设置安装目录、公网 HTTPS origin、本地端口、服务名及首次账号。密码通过隐藏终端提示初始化，至少 12 字符。安装器不运行 sudo；TLS、域名和系统依赖由管理员准备。默认不启用可选桌面；需要时须先安装相应系统依赖。安装器创建独立 release 与 venv，并保留共享 data。选择无 systemd 时仅准备文件，按打印的前台命令启动。

服务启动后选择“2 模型配置”，用本人服务器账号登录，即可新增、修改、切换模型，设置 OpenAI-compatible（Chat Completions / Responses）或 Anthropic、API 地址、模型名、key、输出 token 和兼容的推理/缓存选项。密钥输入隐藏；编辑时直接回车保留旧 key，清除必须使用独立菜单并输入 `CLEAR`。新增档案 `auto_approve=false`，编辑保留原有审批设置。高级参数、工具/语音/连接器设置仍可在 `https://你的域名/duet/admin` 调整。

所有模型修改通过已认证的 `127.0.0.1` HTTP API 完成，保存/切换后重新读取确认，不直接修改运行中数据文件。管理器绕过 HTTP 代理并拒绝重定向；登录 token 只在内存使用，退出撤销本次设备会话。模型变更用于后续请求，当前正在运行的一轮保留其启动时配置。

## 后续操作

```bash
cd "$HOME/duet-source"
bash duet.sh models   # 随时修改/新增/切换模型及 key
bash duet.sh status
bash duet.sh start
bash duet.sh stop
git pull --ff-only    # 先审阅新的代码版本
bash duet.sh update   # 安装此 checkout 版本；不自动 git pull
bash duet.sh --help
```

菜单和所有命令默认使用 `$XDG_CONFIG_HOME/duet-manager/config.json`（未设置 XDG 时为 `~/.config/duet-manager/config.json`），仅保存 root/public_url/port/service/desktop/no_systemd；系统安装另存 scope/service_user，不保存密码、key 或 token；可用 `bash duet.sh --config /absolute/path/config.json models` 管理另一安装。

已有安装可创建以下无秘密配置（路径、端口、service 和桌面选项必须与现有安装一致），再运行 models/status/update；不要对已有安装重新选择 install：

```json
{
  "root": "/home/your-user/.local/share/duet",
  "public_url": "https://duet.example.com",
  "port": 48766,
  "service": "duet-server",
  "desktop": false,
  "no_systemd": false
}
```

升级先预检，再要求确认任务已结束并输入 `UPDATE`。随后停止对应用户服务，在安装目录创建仅本人可读的 `backup-data-UTC时间.tar.gz`，再复用安装器 `--upgrade`；保留账号、数据、凭据和旧 releases，不重置模型。若备份或升级失败，原有数据和 release 保留，服务可能仍停止，请检查错误后恢复对应版本，不要重新初始化账号。备份仅含 data；桌面文件应按部署说明另行停桌面备份。前台模式由你管理进程，菜单不猜测或终止 PID；按部署文档手动停服备份升级。

完整依赖、HTTPS/Caddy、可选桌面、备份恢复及功能边界见 [部署说明](README.zh-CN.md)。此服务可在批准后执行工作区命令，应使用专用、无 sudo 权限的账号；不是多租户隔离平台。

## 验证

```bash
python3 -m unittest discover -s tests -v
bash -n duet.sh installer/install-server.sh bootstrap-desktop.sh
```

测试仅使用临时目录、回环认证 API 及合成 key，不调用付费模型、真实账号或手机。安装器预检示例：

```bash
bash installer/install-server.sh --public-url https://duet.example.com --username owner --check
```

### Root 登录安装与管理

可直接在可信 checkout 目录运行 `bash duet.sh`。root 首次安装默认 `/opt/duet`，创建专用系统账号 `duet-server`（菜单可指定独立账号），安装 `/etc/systemd/system/<service>.service`，并用 system scope 管理启动、停止、状态和升级；不依赖 root 的用户会话或 linger。服务与可选桌面以专用非 root 账号运行并禁止获取新权限。venv、pip 及账号初始化同样降权执行；checkout 可以保留在 `/root`，安装器只复制必要文件，不开放 `/root`。模型配置继续通过本机 API 登录，密码和 key 仍使用隐藏输入。

root 可直接运行 `bash installer/install-server.sh --public-url https://duet.example.com --username owner --check` 预检；预检不会创建账号、目录、unit 或启动服务。去掉 `--check` 执行安装。安装目录须位于 root 持有且其他用户不可写的路径下；已有专用账号必须明确属于该安装，外部账号、unit 和非空外部目录均拒绝接管。多份 root 安装分别指定不同 `--root`、`--service-user`、`--service` 和 `--port`。

普通用户安装仍默认 `$HOME/.local/share/duet` 并使用 `systemctl --user`。旧管理配置默认 user scope，请使用原安装用户运行。root 的新配置保存 `"scope": "system"` 与 `"service_user": "duet-server"`；升级保留账号、模型和 data，停服后在安装目录创建私有备份，再安装当前 checkout。`--no-systemd` 只准备文件，root 须使用安装器打印的 `runuser ... setpriv --no-new-privs ...` 前台命令启动；不要用 root 直接启动服务 Python。
