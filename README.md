# 微信助理：微信转发层

新机器部署请先看 [安装与运行指南](docs/installation.md)；执行路径和验证边界见[项目架构](docs/clawbot-projects.md)。

在 ClawBot 中先选择项目，再持续对话、发送文件和开展工作。
收到的文件直接保存到所选项目的本地目录，不等待 ChatGPT 云端文件上传。

执行边界已由操作者确认：使用本机 Codex 本地项目执行，不采用 Work Cloud，
不把原文件上传到 ChatGPT 云端项目或 Sources。允许官方模型分析所需的正文、片段及工具结果
发送给 OpenAI；这不属于离线推理，也不保证只发送少量内容，读取全文时可能发送全文。
文件读取、修改和命令在 Mac 上进行，OpenAI 模型推理在云端进行。

微信助理维护绑定人身份、腾讯收发游标、消息去重、项目/会话映射、转发队列和发送回执。
`codex_shared` 模式通过官方 Codex CLI 在客户端项目的第一个本地目录执行，
使用本机原有的 Codex 登录、会话和记忆，不在微信工作区另建 Codex 身份或记忆。
项目指令由网关确定性处理；其余所有者文字先由只读 Codex CLI 判断是普通请求、定时创建
还是需要澄清，不依赖关键词或固定话术。普通请求再进入既有 Codex 项目会话，因此每条
普通文字通常会多消耗一次订阅额度内的 CLI 调用。定时请求由 CLI 解析口语化时间和任务内容；
macOS `launchd` 保存并触发计划，届时由本机已登录的
Codex CLI 在所选项目目录执行，结果经原有 ClawBot 发件箱返回。无需 API key 或独立 API 计费。
此计划不属于桌面 Codex Scheduled；执行消耗 Codex 订阅额度。
收到的文件保存在选定项目的第一个本地目录；已在途文件继续使用接收时固定的旧路径。
本地文件不会自动进入 ChatGPT 云端 Sources。

**验证边界：**订阅定时通路通过模拟 `launchd`、Codex CLI 和微信发件箱测试；
此前真实 Codex CLI 已在隔离目录和实际项目目录完成只读验收，
相同 CLI 会话可按精确 thread ID 续聊。CLI `exec` 创建的会话可由客户端按 ID 读取，
但目前不会自动出现在客户端项目的会话列表；对已有客户端会话，`codex queue --thread`
已验证可以追加一轮。旧版 `launchd` Python 触发器曾在真实运行中卡于解释器初始化，
未把到期计划入队；现改为系统 `curl` 回调，并已用不含微信发送的真实 `launchd`
探针验证它能启动退出。新版尚未完成真实定时计划到微信回传的验收。

## 使用

```text
你：项目列表
ClawBot：1. 项目甲  2. 项目乙 ……
你：选择项目 1
ClawBot：当前项目：项目甲。后续消息和文件归入此项目。
你：[发送计划.docx]
ClawBot：↪ 对应你的消息：文件《计划.docx》
        【项目甲】已收到《计划.docx》。
ClawBot：【项目甲】《计划.docx》已保存到项目目录。
        /项目甲在客户端登记的第一个目录/计划.docx
你：请概括《计划.docx》的主要内容
ClawBot：↪ 对应你的消息：请概括《计划.docx》的主要内容
        【项目甲】T000001 已收到。
你：再问一个问题
ClawBot：↪ 对应你的消息：再问一个问题
        【项目甲】T000001 已收到，正在排队；前一项 J000001 完成后会执行。
ClawBot：↪ 对应你的消息：请概括《计划.docx》的主要内容
        【项目甲】T000001 (1/1)
……答复正文……

✅ 本轮任务已完成（J000001）
ClawBot：↪ 对应你的消息：再问一个问题
        【项目甲】T000001 (1/1)
……第二个问题的答复……

✅ 本轮任务已完成（J000002）
```

选定项目后，普通消息延续该项目的当前对话，不必逐条写项目名。
定时任务也无需固定话术，例如“每天早上九点帮我检查这个项目并汇报进展”、
“明天下午三点提醒我整理项目待办”、“1分钟后再向我发一次财经新闻”、
“每隔2天3小时汇报一次”或“1小时后开始，每隔3天检查”。支持每天、工作日、
每周、每月指定日期、每年指定日期、指定时间一次，以及明确的相对秒、分钟、小时、天、
周、月、年和复合时长。相对时间从计划创建时起算，首次和循环间隔分别记录；
循环任务最短间隔为 1 分钟，月份与固定秒数不能混合为一个循环间隔。
时间按北京时间解释。不明确时会请你补充，不会猜测执行时间。创建成功会返回本机计划 ID，
每次完成后的文字结果回到同一个 ClawBot 聊天。
每轮 Codex 答复的最后一条消息带有“✅ 本轮任务已完成”和工作单编号；长答复仅在末段显示。
所有项目共用一条按接收顺序执行的队列。接收时只回复一次确认；若前面还有任务，
确认中会说明排队并标出前一项编号。执行期间不发送进度或心跳；结束时只发送结果，
失败时发送失败通知。确认和最终结果在同一条消息中标出对应的原问题；当前 iLink 出站通道
未验证能显示微信原生引用框，因此不把成功回执当作引用已显示。
若当前会话来自旧执行入口，普通新问题会自动改用新的 Codex CLI 会话；
精确指定旧任务的续问会被拒绝，避免误接续。队列因执行状态待核对而暂停时，
新问题会在确认中提示阻塞的工作单编号。
项目序号支持中文数字，例如“选择项目 二”或“选择项目二”；阿拉伯数字继续可用。
“切换项目 项目乙”切换后续工作；正在下载的文件继续归原项目。
“新对话”另开会话；文件仍属于同一项目。
引用完整结果或使用“继续 T000001 修改要求”继续指定任务。
服务重启后沿用上次选择的项目及其当前会话；项目主目录失效时才需重新选择。
排队中或尚未提交给 Codex 的工作单在重启后自动继续；已生成完整原生结果的工作单
只恢复结果，不重复调用模型。已开始执行却没有可靠结果的工作单会暂停队列并通知核对，
避免重复修改文件或重复外发。
同名项目可用完整 ID 区分。

常用指令：`项目列表`、`当前项目`、`项目目录`、`文件列表`、`发送文件《完整文件名》`。
文件同名时另存为“计划 (2).docx”，不会覆盖。失败文件可用“重试文件《文件名》”重新接收。
新建的 ChatGPT 原生项目可以刷新导入，不限制为固定几个项目。
文件直接保存到客户端本地目录；接收时固定项目、目录和文件名，切换项目不改变在途文件归属。
`codex_shared` 的项目列表仅显示已登记的本地项目；云端项目或缺少主目录的项目不能执行。
切换到 `codex_shared` 后，新收件改用客户端登记的第一个本地目录；
旧收件目录中的文件不自动迁移。
ClawBot 内项目已可重命名；客户端项目名称和挂载文件夹仍需通过客户端正式入口调整，不能靠修改本地映射代替。

## 启动

本机已有工作区和微信绑定时，可双击仓库根目录的 `start-wechat-assistant.command`。
默认使用 `~/Documents/wechat-assistant-workspace`，检查执行入口后启动后台服务并显示状态；
不会自动扫码或导入项目。也可在终端运行 `sh start-wechat-assistant.command "/你的工作区"`。
首次配置仍按下方步骤完成。

需要 uv、Python 3.11–3.13 和官方 Codex 二进制（支持自动寻找 ChatGPT.app 内置版本）。
工作区必填；已有 ClawBot 绑定继续使用。`codex_shared` 使用本机既有 Codex 登录与状态目录，
不复制认证文件。编辑工作区 `.wechat-assistant/clawbot-projects/engine.toml`，设置
`execution_backend = "codex_shared"` 后，停止旧服务再启动。先用 `engine-status` 检查登录。

```sh
cd /path/to/wechat-assistant
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" init
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" doctor
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" engine-status
# 仅尚未绑定时执行，由你扫码：
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" ilink-login
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" service-start
sh scripts/workspace.sh "/path/to/wechat-assistant-workspace" service-status
```

`service-start` 从已有目录访问权限的客户端/终端启动独立后台守护进程，离开临时终端后继续运行，运行进程异常退出会重新启动。
`service-status` 查看守护进程状态，`status` 查看应用健康状态，`service-stop` 停止守护进程和运行进程。
配置和日志保存在工作区 `clawbot-projects/service/`，不复制登录文件、不创建系统级服务。
系统重启后需再次执行 `service-start`；Mac 睡眠时无法处理消息。
调试时可执行前台 `run`，停止前台进程后再启动托管，不能同时运行两份。
`service-start --port 8766` 可调整首次注册端口。服务仅监听 loopback，所有接口要求访问令牌。

可选 `service-start --launchd` 使用 macOS launchd；`service-status --launchd`、`service-stop --launchd` 管理该模式。
它需要 Python 另行获准访问“文稿”目录；若系统等待授权，不能把已注册误判成收件已正常。
本程序不修改系统隐私权限，也不处理授权窗口。默认后台模式使用当前客户端已有的访问权限。
项目列表由可信客户端导出后导入：

```sh
sh scripts/workspace.sh "/你的工作区" projects-import "/你的工作区/.wechat-assistant/projects.json"
```

服务停止时可加 `--offline`。导入只登记项目身份，不执行模型、不上传文件、不复制原生记忆。

`engine-status` 报告 Codex 本地登录和执行入口。普通对话与定时执行都使用本机官方 CLI。
客户端项目列表须通过可信 `list_projects` 回执刷新导入；Codex CLI 本身不列出客户端项目。

## 订阅定时任务

Mac 必须保持当前用户的图形会话和 `launchd` 可用，所选项目目录仍可访问；到期执行时
ClawBot 服务也要运行，或稍后重启处理排队工作。计划配置和日志均位于工作区
`.wechat-assistant/clawbot-projects/schedules/`。服务启动时会重新注册已保存的计划。
`launchd` 使用 Mac 的本地时区解释日历时间；请将系统时区设为北京时间。相对时间计划
以创建时的时间戳为锚点，由 `launchd` 按间隔唤醒并在数据库中核对时间槽；休眠时可能
错过触发，逾期超过 30 分钟的时间槽不会补跑。同一循环计划上一轮尚未完成时不再入队，
待完成后只处理最新时间槽，避免工作单无限积压。计划使用已有的 Codex 订阅登录和额度；
`engine-status` 应显示 `ready`。创建和到期执行前还会核对 CLI 显示为 ChatGPT 登录；
若切换为 API key 登录，定时工作会保留排队，不会产生 API 计费。创建结果不明或 Codex
执行中断时不会自动重复提交。

## 存储

```text
工作区/.wechat-assistant/
├── credentials/ilink.json       # ClawBot 绑定
├── runtime.lock
├── clawbot-projects/
│   ├── gateway.sqlite          # 原始事件、项目路由、原生交接、任务、文件与回执
│   ├── operator-token
│   ├── engine.toml             # codex_shared 时直接驱动本机 Codex
│   ├── schedules/              # launchd 计划配置与触发日志
│   └── service/                # 微信收发服务的守护进程记录与日志
├── archive/legacy-history.sqlite # 保留原始历史，不含运行逻辑
├── cleanup-report.json
└── venv/、cache/、tmp/          # 精简依赖、安装缓存与临时文件
```

微信转发运行数据位于指定工作区内。用户文件直接位于所选本地项目目录。
旧独立执行器数据和旧未归类附件封存在项目目录的 `.migration-archive/`；
它们是迁移备份，未导入为 ChatGPT 原生记忆，不能把其中草稿当成已发送事实。
清理脚本 `scripts/cleanup_workspace.py --workspace WORKSPACE` 默认预览，`--apply` 才执行；
只处理已知旧目录，运行锁被占用时拒绝操作，先保留原始历史和用户上传文档。
代码职责和执行接口见 [项目架构](docs/clawbot-projects.md)。
