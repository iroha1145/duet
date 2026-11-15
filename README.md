# duet 私人自托管服务器

一个账号、多设备同步的 Linux 服务端。此仓库包含服务端源码、管理页面、安装器与中文命令菜单，不包含 APK、手机源码、用户数据、账号或 provider key。

## 一键入口

准备 Linux、Python 3.11+、venv/pip 和普通服务用户。使用你自己的 GitHub 身份认证访问私人仓库：

```bash
gh auth login
gh repo clone iroha1145/duet
cd duet
bash duet.sh
```

也可使用已配置 SSH 身份的 `git clone git@github.com:iroha1145/duet.git`。私人仓库不能通过未认证的 raw curl 安装；先 clone、审阅脚本，再执行。仓库目录不要放进安装目录。

选择“1 安装”，按提示设置安装目录、公网 HTTPS origin、本地端口、用户服务名及首次账号。密码通过隐藏终端提示初始化，至少 12 字符。安装器不运行 sudo；TLS、域名和系统依赖由管理员准备。默认不启用可选桌面；需要时须先安装相应系统依赖。安装器创建独立 release 与 venv，并保留共享 data。选择无 systemd 时仅准备文件，按打印的前台命令启动。

服务启动后选择“2 模型配置”，用本人服务器账号登录，即可新增、修改、切换模型，设置 OpenAI-compatible（Chat Completions / Responses）或 Anthropic、API 地址、模型名、key、输出 token 和兼容的推理/缓存选项。密钥输入隐藏；编辑时直接回车保留旧 key，清除必须使用独立菜单并输入 `CLEAR`。新增档案 `auto_approve=false`，编辑保留原有审批设置。高级参数、工具/语音/连接器设置仍可在 `https://你的域名/duet/admin` 调整。

所有模型修改通过已认证的 `127.0.0.1` HTTP API 完成，保存/切换后重新读取确认，不直接修改运行中数据文件。管理器绕过 HTTP 代理并拒绝重定向；登录 token 只在内存使用，退出撤销本次设备会话。模型变更用于后续请求，当前正在运行的一轮保留其启动时配置。

## 后续操作

```bash
bash duet.sh models   # 随时修改/新增/切换模型及 key
bash duet.sh status
bash duet.sh start
bash duet.sh stop
git pull --ff-only    # 先审阅新的代码版本
bash duet.sh update   # 安装此 checkout 版本；不自动 git pull
bash duet.sh --help
```

菜单和所有命令默认使用 `$XDG_CONFIG_HOME/duet-manager/config.json`（未设置 XDG 时为 `~/.config/duet-manager/config.json`），仅保存 root/public_url/port/service/desktop/no_systemd，不保存密码、key 或 token；可用 `bash duet.sh --config /absolute/path/config.json models` 管理另一安装。

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
