# duet 自托管服务器部署

此包提供一个个人账号、多设备同步的服务器。手机 APK 只保存自己的服务器会话，通过 HTTPS 登录；模型、提供商密钥、对话、文件、排程都由服务器管理。不要把服务器账号密码与模型 API key 混用。此服务能按你的授权在运行用户的工作区执行命令，建议使用专用、无 sudo 权限的 Linux 用户。

## 安装

需要 Linux、Python 3.11 或更新版本、venv/pip、可联网安装 requirements 的环境，以及指向服务器的域名。Debian/Ubuntu 可由管理员预先安装 `python3-venv python3-pip`；语音听写还需要 `libopus0`，朗读还需要带 `azmq`、`atempo` 过滤器的 `ffmpeg`。Python 依赖包含 `pyzmq`。安装器本身不运行 sudo、不改系统防火墙、不申请 TLS 证书。

如自行给 systemd 服务增加 `RestrictAddressFamilies`，语音转换的 ZeroMQ 依赖可能还需要 `AF_NETLINK` 来枚举网络接口。已知部署限制：仅允许 `AF_INET AF_INET6 AF_UNIX` 时，ffmpeg 在接口解析阶段退出；保留其余限制并加入 `AF_NETLINK` 后，同样的 PCM/Opus 转换通过。随包安装器不设置这项限制；自定义服务应在实际运行用户和沙箱配置下验证转换，而不只在 SSH 终端检查 ffmpeg。

将服务器 zip 解压到独立目录，例如 `$HOME/duet-package`；不要解压到安装目录或其子目录。包中应有 `bridge/`、`duet_server/`、`requirements.txt`、`bootstrap-desktop.sh`、`launch.py`、`installer/install-server.sh` 和本说明。用普通服务用户运行：

```bash
cd "$HOME/duet-package"
bash installer/install-server.sh --public-url https://duet.example.com --username owner --check
bash installer/install-server.sh --public-url https://duet.example.com --username owner
```

首次安装会在终端通过 getpass 两次读取密码（至少 12 字符），不把密码写入参数、脚本、日志或环境变量。已有账号不会重新初始化，`--username` 也不会更名或重置密码。默认安装到 `$HOME/.local/share/duet`，可用 `--root /absolute/path` 指定由当前用户持有的专用空目录；目前路径仅接受无空格的 ASCII 绝对路径，不接受符号链接祖先。已有安装必须显式传 `--upgrade`。

安装器复制代码到独立 `releases/release-*` 目录，创建该版本自己的 venv，使用共享的 `data/`，生成并启动 `duet-server.service` 用户服务。`current` 是记录版本路径的普通文本文件。失败时保留已暂存版本便于检查，不删除旧版本或数据。`--check` 只做参数、源文件、账号和路径预检，不联网、不写文件、不启动服务。

```bash
systemctl --user status duet-server.service
journalctl --user -u duet-server.service -n 80
curl --fail http://127.0.0.1:48766/duet/auth/status
```

服务仅监听 `127.0.0.1:48766`，无需对公网开放此端口。用户服务默认依赖用户 systemd 会话。需要退出 SSH 后仍运行或开机启动时，由管理员为此专用用户启用 linger：`loginctl enable-linger USERNAME`。这不是安装器自动执行的动作。自定义端口用 `--port`；同一用户安装多份需使用不同 `--root`、`--service`、`--port`、域名。

## HTTPS 与反向代理

由管理员安装并运行 Caddy，为域名配置 DNS，开放 HTTPS 所需端口。以下 Caddyfile 将整个站点原样转发，包含普通 API、SSE、上传和语音 WebSocket：

```caddyfile
duet.example.com {
    reverse_proxy 127.0.0.1:48766 {
        flush_interval -1
    }
}
```

Caddy 的 reverse_proxy 自动处理 WebSocket upgrade。`flush_interval -1` 立即刷新 SSE；不要在额外代理/CDN 层缓存、缓冲 SSE，或设置过短的流读取超时。保持路径和请求体完整，不在 URL 中添加账号 token，不启用会记录 Authorization/密码请求体的调试日志。使用该域名的有效 TLS 证书，并使安装参数 `--public-url` 与实际公开 HTTPS origin 一致（仅 origin，不含路径、查询或凭证）。不依赖客户端提供的 Forwarded 头改变公开 origin。HTTPS 验证：

```bash
curl --fail https://duet.example.com/duet/auth/status
```

浏览器访问 `https://duet.example.com/duet/admin`，用初始化的同一账号登录。手机“账号与服务器”填写 `https://duet.example.com` 和同一账号密码；多台设备会同步同一服务器数据。不要将模型密钥填到手机登录密码框。

## 模型与语音配置

在 `/duet/admin` 创建模型档案：设置 OpenAI-compatible 或 Anthropic-compatible 协议、API base URL、模型 ID 和该提供商密钥，再选择当前档案。模型档案/密钥仅存服务器，管理页面不回传密钥明文；留空的编辑与明确清除操作应按界面区分。模型切换用于之后的新一轮请求，正在运行的请求使用其启动时的配置。访问外部提供商会把相关提示词和附件发给该提供商，应选择你认可的服务。

OpenAI 档案可选 Chat Completions 或 Responses。GPT-6 Luna 的带推理工具调用使用 Responses，并可选择推理强度、显式/隐式缓存及 30 分钟/12 小时有效期。旧档案保留 Chat Completions 默认值。编辑档案时留空密钥或系统提示不会清除已有内容，明确清除需使用对应选项。详见 `docs/openai-responses.txt`。

同一页面可配置语音听写的 OpenAI-compatible transcription base URL、模型和 key；服务调用其 `/audio/transcriptions`。使用 OpenAI `gpt-transcribe` 时选择对应的转写接口格式；已有兼容服务保留传统格式。Linux 需能加载 `libopus0`。这是录音停止后得到最终文本的听写；Talk 还要求朗读、文字模型和运行依赖均就绪。协议测试不能代替真实提供商与手机验收。可在设备管理中撤销某台设备的服务器会话。

“朗读回复”另行配置 base URL、模型、声音 ID 和密钥，并选择 HTTP 语音合成或 OpenAI Realtime。旧服务继续使用 `/audio/speech`；`gpt-realtime-2.1-mini` 使用独立 Realtime WebSocket。两者均要求 24 kHz、单声道、16 位小端 PCM。服务器通过 ffmpeg 转换为原客户端要求的 PCM 或 Opus，支持播放中改变速度并保持音调。朗读默认关闭，不自动继承聊天或听写密钥。启用后的运行依赖检查不等于已经验证提供商账号、声音名称或合成质量。Realtime 转录若偏离原文会报错，但已播放的音频不能撤回；它不保证逐字朗读，也不会自动改用其他模型。接口设置与验证边界见 `docs/audio-upstream.md`。手机中的朗读开关仍由用户控制。

`gpt-4o-mini-tts` 可使用 HTTP 文本合成接口。选择模型并检查依赖后，仍需自行验证提供商及设备链路。该模型和 `tts-1`、`tts-1-hd` 官方计划在 **2027-01-06** 停服，需要在此之前明确选择新的兼容方案。服务不会自动更换模型或追加重试费用。[官方停服与迁移说明](https://developers.openai.com/api/docs/guides/text-to-speech)

原生声音、语速、语音语言及听写语言偏好由服务器保存并跨设备同步；声音目录必须映射到实际提供商声线，预览也是实际配置的合成结果。没有可用声线时不显示伪造的原厂声线。配置结构与预览行为见 `docs/native-voice.md`。

## 上下文、提示词与缓存

聊天历史、频道和后台任务都保存在服务器。每个频道有独立对话与可配置指令；Everything 可汇集其他频道的已完成上下文。当前一轮启动时固定模型、提示词和工具配置，切换模型不会改变已经启动的请求。

模型档案中的 prompt cache 与 stream usage 默认开启。Anthropic 协议对稳定系统提示和历史末尾设置缓存标记；OpenAI 协议使用稳定缓存键并请求流式 usage。管理页展示提供商实际返回的输入、输出与缓存 token 数；缺少统计显示“未报告”，不会把缺失数据记作命中或零消耗。兼容网关若拒绝这些可选字段，可关闭对应档案的开关。最终是否缓存、多久有效、如何计费由所用提供商决定；本地协议测试不代表真实付费接口已经产生缓存命中。

一轮回答保留原生 Markdown AST 分段：一个消息可显示为多个气泡，代码块、列表和表格保留原结构。原生段落引用使用实际父消息 ID 和 `reply_to_quote`；服务器保存所选引用并作为数据交给模型，正文仍是用户输入。不会用简单换行规则伪造多个服务端消息，详见 `docs/reply-quotes.md`。

## 后台提醒与例行任务

推送使用服务器所有者自己的 Firebase 项目，Android 应用包名为 `com.duet`。服务账号私钥只保存在服务器；手机取得公开客户端配置并向自己的服务器注册。原生通知开关、系统通知权限和实际收到注册挑战三者均满足后，才启用该设备推送。普通访问令牌刷新不取消已验证授权；退出登录、撤销设备、更换令牌或项目配置会使旧授权失效。Google 接受发送不等于手机已显示通知，部署后仍须做真实后台收件和点击验收。配置及状态协议见 `docs/fcm.md`。

主动 check-in 和 morning brief 默认关闭。启用后使用服务器当前模型、真实上下文与当前工具权限；静默生成内容不会自动进入聊天，只有通过实际来源验证的结果才发布。晨报遵守配置时区、安静时段和夏令时；check-in 采用本服务可配置的周期策略，不声称复现原厂服务端调度算法。已跟踪的私有 `write_file` 产物不会自动进入 Make 或 Files；获批 exec、外部连接器及其他进程写盘不属于完整文件隔离保证。详见 `docs/routines.md`。

原 APK 中提取到 34 组提示词及规则片段，随包保存在 `bridge/prompt_assets/original-duet-prompts.json`，带源位置和适用范围。普通文字聊天完整系统提示词没有在静态包中恢复出来。服务器使用稳定的 duet 文字代理提示词，并吸收其中可适用的事实核验、工具结果和指令/数据边界原则。实时语音使用独立协议和提示词，不将它冒充普通聊天提示词。自定义系统提示和频道指令分别追加，可在自己的服务器代码/配置中检查。

## 官方 MCP 连接器与联网搜索

在服务器管理页配置连接器。当前目录包含核对过的官方远程 MCP：GitHub、Hugging Face、Exa 和 Gmail。GitHub 可使用自己的 PAT/access token，Hugging Face 可使用自己的 HF token；Exa 可显式启用免 key 连接，或填写自己的 API key。凭据只在服务器保存；不读取 Codex 插件或原厂账号凭据。服务端也支持 OAuth 浏览器授权，但须先由服务器所有者配置服务商允许的客户端注册方式、客户端信息及公网 HTTPS 回调地址；默认不启用，也不借用原厂客户端身份。

同一服务可以建立独立安装并设置标签；各安装的账号、工具权限和连接状态分别管理。OAuth 授权仅在显式选择时共享，连接或共享授权不等于批准执行工具。连接成功后发现实际工具并交给模型，通过原生工具审批执行。原 APK 的 Connections 页面支持当前工具的禁止、询问和显式允许；更严的 Rules 仍优先。权限绑定当前安装代次和完整工具定义，更换凭据、重新授权或工具定义改变时不继承旧的允许。原生单账号多设备设置按增量事务合并，同一项最后提交生效；发送 If-Match 的 API 客户端可选择版本冲突检查。GitHub 可选择官方只读模式；不要把模型可见的远程工具说明当作额外授权。

Gmail 仅通过 Google 官方 MCP 保存草稿及提供允许的读取、搜索和标签工具，默认关闭；没有 Gmail REST、发送邮件工具或生产邮件发送适配器。保存草稿不是已发送邮件，需用户自行在 Gmail 中发送。配置者须先获得 Workspace Developer Preview 资格，在自己的 Google Cloud 项目启用 Gmail API 与 Gmail MCP，创建勾选 **Use this client for an AI-powered agent** 的 Web OAuth 客户端并配置自己的 client ID/secret。回调必须是 `https://你的域名/duet/oauth/callback/gmail`。只支持 `pre_registered`，不接受手填 bearer token、DCR 或 CIMD。Google 的 `gmail.compose` scope 本身包含发送权限；本服务通过 MCP 工具允许列表限制为草稿，不应把 token 称为天然只读或仅草稿权限。详见 `docs/connectors.md` 与 [Google 配置说明](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server)。请在自己的项目与账号下完成 Google 用户授权，并检查真实发现的工具。

GitHub 原生草稿卡可在明确点击 Send 后，将选定版本及编辑后的文字交给相应安装的已审查官方 MCP 工具。创建草稿和编辑均不执行外部写入；远端返回的可核验回执才会标为 sent。未知结果不自动重试。当前生产适配器仅支持 GitHub issue/comment；卡片支持的 email/textMessage 类型不代表已配置邮件或短信发送。任务建议可在聊天或 Up Next 采用，启动后台任务仍受任务确认与工具权限约束；原生 Hide 只隐藏本地轮播项，不等于服务端删除建议。

原版 APK 展示 `WebSearch`/`web_search` 和 `WebFetch`/`web_fetch` 的工具调用与结果，但静态包不能证明原厂搜索供应商或服务器算法。本版通过 Exa 官方 MCP 提供联网搜索/读取，不声称复制了原厂搜索后端。每个连接器的可用工具、账号权限及实际执行需在部署环境分别验证。未启用搜索工具时，助手应说明无法联网验证，不能编造搜索结果。

## 私有 Plugins 库

Plugins 使用服务器所有者导入的声明式包。包内可有 `.claude-plugin/plugin.json`、`skills/*/SKILL.md` 及受限 Markdown/JSON 参考资料，也可声明当前目录中精确匹配的官方 MCP URL。包不能带连接凭据，不能运行安装脚本、hooks、CLI、原生二进制等执行组件；这些组件会明确被拒绝。导入默认停用，可在手机原生 Plugins 页面 Add/Remove。更新包内容会重新停用，需再次明确启用。

在安装版本目录中使用其 Python 环境导入：

```bash
./venv/bin/python -m bridge.plugins --data-dir /absolute/install/data import /absolute/path/plugin.zip
./venv/bin/python -m bridge.plugins --data-dir /absolute/install/data list
```

也提供需要本人服务器登录的 `POST /duet/plugins/import`（`application/zip`）与 `GET /duet/plugins`。它不是原厂公共插件商店。包中的 MCP 引用不会自动创建连接或绕过工具审批，连接仍在 Connections 中独立管理。启用技能按每次模型请求读取；停用后不再主动提供或选择该技能，不声称擦除模型已经看到的历史。`content_scan:null` 明确表示未完成安全扫描。

## 本地仓库任务

服务器可显式注册本地 standalone Git 仓库，并通过原生仓库选择器启动任务；安装本功能的 APK 与服务器须匹配。先由所有者准备仓库和本地已提交版本，再按 `docs/repositories.md` 在私有 `data/repositories.json` 配置允许根目录、显示名称、相对路径及完整 commit 或本地分支 ref，空闲时重启服务加载。不配置则不提供仓库任务。

服务不 clone/fetch，不接管源仓库 GitHub 登录或复用凭据，也不复制源 config/hooks/filters。任务获得独立 detached committed checkout 和精简 `.git`，支持本地修改及 commit/diff；源仓库的脏文件保持原样。仓库文件不会自动映射到全局工作区，仓库任务的 `publish_artifact` 明确拒绝；没有隐含 push/PR/发布能力。链接、子模块、linked worktree 和超过预算的仓库不支持。

## Helpers 与 Make

Helper 使用原生请求的名称、用途、图标和幂等键创建独立私有频道，不覆盖主项目。这里的私有是单账号所有者频道；主 Everything 会聚合 active helper 的已完成消息及主题，其他 helper 按自身频道读取上下文，不是跨用户隔离平台。原生 helper 入口受客户端原有条件控制，服务不会伪造原厂账号状态强制打开。详见 `docs/helpers.md`。

Make 提供九种本地 family：HTML page、DOCX document、PPTX deck、XLSX sheet，以及 dashboard、design、animation、whiteboard、checklist 五种有实际交互的 HTML starter。它们使用真实本地 starter/prompt，不是恢复的原厂托管模板目录。Office 写入依赖 requirements 中固定的 `python-docx==1.2.0`、`python-pptx==1.0.2`、`openpyxl==3.1.5`；缺库时对应 family 不显示。下载保留实际 Office 原字节，成功发布后按内容版本保存。

Office 视觉预览需要 Linux 上的 LibreOffice Writer/Impress/Calc、Poppler、bubblewrap、prlimit 及字体。Debian/Ubuntu 管理员可按发行版审核后预装：

```bash
sudo apt-get update
sudo apt-get install git libreoffice-writer libreoffice-impress libreoffice-calc poppler-utils bubblewrap util-linux fonts-noto-cjk
```

安装器不自动执行上述系统安装。Debian 13 可使用 LibreOffice 25.2.3、Poppler 25.03、bubblewrap 0.12 与 Noto CJK；其它版本需在实际运行用户和沙箱下核对。内核必须允许所需 user/network/PID namespace，systemd 的 namespace 策略也须允许建立隔离。不要为取得预览而改成无沙箱转换。不能建立隔离、转换失败或超预算时明确退为语义文本预览。

视觉路径在严格隔离中运行 LibreOffice→PDF→JPEG，没有宿主用户目录、应用工作区或外网访问；字体和分页可能不同于 Microsoft Office。普通 Calc 公式可在隔离视觉转换中重算，普通附件文本提取不执行公式或宏。HTML starter 的交互状态只存在当前 view，需通过后续 Make 发布文件才形成持久版本；历史 Office 的源 SHA 进入编辑提示，但不是强制 CAS 编辑器。详见 `docs/make-formats.md` 的预算、回退、下载和版本语义。

## 可选服务器桌面

聊天、模型配置与后台任务不要求运行图形桌面。需要让 AI 使用截图和电脑操作工具时，先由管理员安装 `bootstrap-desktop.sh` 开头列出的桌面依赖（Debian/Ubuntu 通常为 `xvfb xauth x11-utils xfce4 xfce4-terminal x11vnc novnc xdotool scrot dbus-x11 xdg-utils`），并单独安装浏览器。脚本提供 `bash bootstrap-desktop.sh --install`，**该明确选项会调用 sudo/apt**；按发行版核对后再使用。安装器 `--desktop` 自身只检查依赖，不调用它的 `--install`。

首次安装添加 `--desktop`，或在后续安装中传 `--upgrade --desktop`。它生成 `duet-server-desktop.service`，使用：

```text
DUET_DESKTOP_DIR=<安装目录>/desktop
```

这必须与 `<安装目录>/data` 的父目录匹配；服务端从这里读取 VNC 凭证和显示状态。默认显示 `:99`、VNC 端口 `127.0.0.1:5900`，不要暴露 5900 到公网。已有同显示或端口的桌面时先明确处理冲突，不让脚本杀其它进程。可选桌面 unit 使用 oneshot/RemainAfterExit，子进程归它的 systemd control group，停止 unit 会停止这组桌面进程；不要把已手工启动的桌面混入此 unit。桌面启动脚本遇到未知显示会拒绝复用。

```bash
systemctl --user status duet-server-desktop.service
systemctl --user stop duet-server-desktop.service
```

本版按用户要求暂不开放手机查看虚拟桌面或手动接管，保留原版电脑/任务入口；上述组件仅为服务器的 AI 电脑操作提供运行环境。该服务面向同一个人的多个设备。若不再需要图形环境，先 `systemctl --user disable --now duet-server-desktop.service`，再不带 `--desktop` 升级服务器 unit；省略该选项不会擅自停止已有桌面。

## 备份、升级与恢复

先确认后台任务结束，再停止服务，避免升级中断命令或模型请求。完整备份 `data/`（包括 auth 数据库、模型密钥、对话、上传、排程、工作区和偏好），有桌面时也备份 `desktop/`。备份含秘密，保持目录/归档仅本人可读，并在离机存储时加密。运行中的 SQLite/WAL 和多个 JSON 文件不宜直接拼凑复制；这里采用停服后的一致性备份：

```bash
systemctl --user stop duet-server.service
umask 077
tar -C "$HOME/.local/share/duet" -czf "$HOME/duet-data-backup.tar.gz" data
```

请将示例归档名换为带日期的独立文件，避免覆盖唯一备份。若桌面仍有应用在写文件，备份 desktop 前也停止桌面 unit。

新版本解压到另一个独立源目录，保留安装目录 `data/`，执行：

```bash
cd "$HOME/duet-package-new"
bash installer/install-server.sh --public-url https://duet.example.com --upgrade --check
bash installer/install-server.sh --public-url https://duet.example.com --upgrade
```

自定义 root/port/service 或启用桌面的安装，升级时继续传入相同参数及 `--desktop`。安装器不会覆盖 data、重置已有密码或自动删除旧版本，但依赖安装会访问 Python 包索引；需要可复现供应链时先准备并审核自己的锁定 requirements/wheel 源。启动失败查看 journal；不要反复重置账号试图修复部署。

回退前停服，保存当前数据，使用备份时对应版本的代码和依赖。**新版本若迁移了数据，旧代码不一定能直接读取**，需要恢复匹配的数据备份。可通过 `systemctl --user edit --full duet-server.service` 将 WorkingDirectory/ExecStart 改回安装器打印的 Previous release，再 daemon-reload/restart；同步更正 `current` 的版本路径。不要删除仍被 unit 引用的 release。手机保存的是服务器设备会话；恢复较早备份可能导致重新登录。

## 无 systemd 的前台运行

使用相同参数加 `--no-systemd`，安装器完成代码、venv 和账号准备，打印完整前台命令，不启动后台进程。按打印命令运行，并保持终端开启；或自行配置受控进程管理器。命令等价于：

```bash
cd /absolute/install/releases/release-XXXXXXXX
./venv/bin/python -m bridge.server --host 127.0.0.1 --port 48766 \
  --data-dir /absolute/install/data --public-url https://duet.example.com \
  --pid-file /absolute/install/data/service.pid
```

同一个 data 目录只能运行一个服务进程。手工桌面需先在自己的进程管理器里设置 `DUET_DESKTOP_DIR=/absolute/install/desktop` 后运行 `bash bootstrap-desktop.sh`，明确管理该次启动的子进程；不要同时启动可选 systemd 桌面 unit。

## 能力边界

这是个人自托管兼容服务，不接入原厂账号、原厂托管电脑或原厂订阅。未实现的兼容路由明确报错。当前目录外的连接器、日历 OAuth、真实供应商授权/写入、公开页面分享及完整 Talk 实机链路仍需单独配置、实现或验收；通知偏好保存不等于通知已送达。后台任务、例行任务和显式排程依赖服务器持续运行；手机离线不等于服务器任务停止，重新连接可读取同步结果。

图片附件支持 PNG/JPEG/GIF/WebP（单张最多 6 MiB），PDF、DOCX、XLSX、PPTX 和 UTF-8 文本原始文件最多 20 MiB；每条消息最多 10 个附件、总计最多 20 MiB。文档文本有明确提取上限，超限不会静默截断，提取本身不执行公式或宏，也没有 OCR 或文档视觉布局理解。下载保留原始字节，详见 `docs/attachments.md`。Make 使用真实 HTML/Office 文件和保留的原生预览组件，历史产物按内容版本保存；没有公开分享 URL。

模型能用哪些工具、电脑操作环境是否就绪、语音是否可用取决于服务器配置与系统依赖。服务器可在工具审批后执行工作区文件和命令；单账号方案不是多租户隔离平台。协议测试使用明确标注的本地样例；真实提供商、实际缓存命中、手机后台通知及语音效果必须单独记录验证结果，不能从协议样例推定。
