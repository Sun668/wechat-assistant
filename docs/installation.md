# 安装与运行指南（macOS）

本项目是绑定人专用的 ClawBot 微信收发网关。它把绑定人的消息路由到已登记的**本地 Codex 项目**，在项目主目录运行官方 Codex CLI，并把结果发回同一个 ClawBot 聊天。文件默认接收到项目主目录，也可用 `default_upload_path` 配置独立收件目录；运行数据库、微信凭据、队列和日志放在单独工作区的 `.wechat-assistant`。模型推理需要联网，项目文件不会因本地接收而自动进入 ChatGPT 云端 Sources。

当前代码只实现 macOS 常驻服务及 `launchd` 定时触发。没有 Windows/Linux 服务安装流程，也没有从普通终端自动发现 Codex 客户端项目的命令。

## 1. 准备软件和账号

也可双击仓库根目录的 `setup-wechat-assistant.command` 自动完成下述依赖检查、安装、
工作区初始化和微信二维码绑定。先在 `Config.toml` 设置 `workspace`（默认 `.Workspace`）；
脚本会创建目录。已有依赖和绑定会复用，登录确认与扫码仍由你完成。
它不启动收发服务、不执行模型；完成登录与绑定后会拉取一次项目，`init` 命令也会拉取一次。
无需单独的项目拉取脚本。拉取失败保留旧列表；完成后继续第 4 节。
安装器来源为 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/)和
[Codex CLI 官方说明](https://learn.chatgpt.com/docs/codex/cli)。

- 一台可联网的 Mac，Python 3.11–3.13，`uv`，以及能用 `codex` 命令调用的官方 Codex CLI。建议使用 ChatGPT 登录，不要把 API key 或 `~/.codex` 中的认证文件复制进项目或工作区。
- 已在 ChatGPT/Codex 客户端创建至少一个**本地项目**，并确认其第一个（主）本地目录存在、可读写。云端项目不能作为这里的执行目标。
- 能在手机微信中完成 ClawBot 扫码绑定。首次绑定和真实消息验收必须由账号所有者亲自操作。

如果尚未安装，可按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)安装 `uv`；Homebrew 用户可运行 `brew install uv`。按 [Codex CLI 官方安装说明](https://learn.chatgpt.com/docs/codex/cli)安装 `codex`，再运行 `codex login` 并在浏览器中选择 ChatGPT 登录。用以下命令检查：

```sh
uv --version
uv python install 3.13
codex --version
codex login status
```

`codex login status` 应显示 `Logged in using ChatGPT`。这份代码调用 `codex --approve-for-me`、`codex exec --output-schema` 和 `codex exec resume`；首次部署前应核对安装的 CLI 支持这些参数。Codex CLI 若只使用 API key 登录，定时请求会被保留而不会按订阅通路执行。

## 2. 取得代码，建立独立工作区

从 GitHub 取得代码后，在终端进入仓库。默认工作区为仓库根目录下的 `.Workspace`，已加入 Git 忽略规则。以下路径请换成自己 Mac 上的实际仓库路径：

```sh
git clone https://github.com/Sun668/wechat-assistant.git
cd wechat-assistant
mkdir -p "/path/to/wechat-assistant/.Workspace"
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" init
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" doctor
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" engine-status
```

第一次运行会由 `uv` 按 `uv.lock` 安装 Python 依赖，需要能访问包下载源。脚本把虚拟环境、下载缓存和临时文件放在该工作区的 `.wechat-assistant` 下。工作区必须事先存在，除作为启动入口的本仓库外，应与执行目标的项目主目录分开；项目目录不能包含工作区状态目录，两个项目的本地目录也不能重叠。不要把工作区或收到的个人文件上传到代码仓库。

`doctor` 和 `engine-status` 应显示 `execution_backend: codex_shared`、`local_engine: ready`；绑定前的 `clawbot_bound: false` 属于正常状态。如果是 `binary_missing`，检查 `codex` 是否在服务进程可见的 `PATH` 中；必要时可在工作区的 `.wechat-assistant/clawbot-projects/engine.toml` 设置官方 CLI 的绝对路径：

```toml
execution_backend = "codex_shared"
codex_binary = "/absolute/path/to/codex"
```

如果是 `login_required`，请由操作者在这台 Mac 的正常 Codex 状态目录完成 `codex login`。不要另建工作区内的 Codex 身份或复制全局认证文件。

## 3. 首次绑定 ClawBot

仅在当前工作区**尚未绑定**时执行：

```sh
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" ilink-login
```

命令会给出本地二维码 PNG 路径。由账号所有者查看该图片、用手机微信扫码确认；如果手机要求验证码，按终端提示输入。命令确认成功后，凭据保存在工作区的 `.wechat-assistant/credentials/ilink.json`，不会从别处复制。已有绑定不会被自动替换。扫码阶段不要把二维码、验证码或凭据截图发给其他人。

## 4. 启动服务，刷新本地项目

```sh
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" service-start
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" service-status
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" status
```

`service-start` 首次可能返回 `starting`，随后再运行 `service-status`，确认 `registered: true`；`status` 应显示 `running: true`。默认端口是本机 `127.0.0.1:8765`。若首次启动要换端口，可在 `service-start` 后加 `--port 8766`，之后的 `status`、`projects-import` 也要加相同的 `--port`。不要同时运行 `run` 与后台服务，或在同一工作区启动两个实例。

服务启动后，绑定人在微信发送“项目列表”。每条新指令都会调用本机官方 Codex App Server
的实验性 `project/list` 接口，读取全部分页并更新菜单；无需每次手动导出或导入文件。
它使用本机正常 Codex 状态，不复制凭据，不调用模型，不需要 GUI 自动化。
旧 CLI 不支持接口、未登录、连接中断或超时时，会回复“项目刷新失败”，并显示上次保存的列表。
首次失败且没有历史项目时，列表为空；需修复登录或升级支持此协议的官方 CLI 后重发指令。
无法访问、包含工作区运行状态或与其他项目重叠的目录不会加入本次列表；云端占位目录也不导入。
主目录位于 `~/Documents/ChatGPT` 及其子目录的项目也会排除，兼容的手动导入与失败回退菜单
使用同一规则。旧缓存中的此类项目不能被重新选择或执行；历史记录和客户端文件保留。
已移除项目仅从菜单隐藏，历史会话、文件与在途工作单保留。

兼容方式：仍可导入可信客户端 `list_projects` 回执。在支持该工具的 Codex 客户端中，
让助手调用工具，把原始 JSON 保存为工作区内的 `.wechat-assistant/projects.json`；不要手编项目 ID。
导入器读取 `projects` 数组，仅处理 `projectKind` 为 `local` 的条目。结构示意如下，尖括号内容不是可用项目 ID：

```json
{
  "projects": [
    {
      "projectId": "<客户端返回的真实 UUID>",
      "projectKind": "local",
      "label": "我的本地项目",
      "path": "/path/to/my-project"
    }
  ]
}
```

```sh
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" projects-import "/path/to/wechat-assistant/.Workspace/.wechat-assistant/projects.json"
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" status
```

刷新或导入后，`status` 的 `project_count` 应大于 0。服务停止时才可使用 `projects-import ... --offline`。
`project_catalog_status` 表示最近一次自动刷新状态；失败不会清空之前的菜单。

## 5. 使用、停止与查看问题

账号所有者在已绑定的 ClawBot 私聊中发送“项目列表”，再发送“选择项目 1”。随后可发普通问题或文件；文件默认落在所选项目的主本地目录，普通请求始终由项目主目录中的官方 Codex CLI 执行，可能读取、修改文件并运行命令。只应绑定可信的所有者。服务只回复绑定人，不处理微信联系人消息，也不会替联系人自动回复。`项目目录`、`文件列表`、`新对话` 等指令见 [README](../README.md)。

可在 `Config.toml` 设置 `default_upload_path = "/绝对路径/收件目录"`（也支持 `~/`）更改所有项目
后续收件的默认目录，目录可在工作区外；默认空值 `""` 保持现有行为。
机器人 `/upload-path` 查看配置、`/upload-path /绝对路径` 修改并保存、`/upload-path reset` 清空恢复。
这些指令只允许绑定人私聊并单独发送，可不选择项目；对在途与已收文件不生效。
设置目录后可不选项目直接发文件，文件记入未归属项目的“收件箱”，不会调用 Codex。
未选项目时，“文件列表”和“重试文件《文件名》”查看或重试这部分收件；之后选项目不自动迁移或归类。
服务进程需要该目录的读写权限，无法访问时会报错；修改后无须重启。

```sh
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" service-status
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" status
sh scripts/workspace.sh "/path/to/wechat-assistant/.Workspace" service-stop
```

默认后台服务的日志在工作区 `.wechat-assistant/clawbot-projects/service/background.log`；网关数据库在 `.wechat-assistant/clawbot-projects/gateway.sqlite`。系统重启后要重新运行 `service-start`；Mac 睡眠时不会处理消息。可选的 `service-start --launchd` 需要该系统服务另行具有项目目录的访问权限，不建议作为首次部署方式。定时任务依赖 macOS `launchd` 和本机时区，时区应设为北京时间；完整的真实“到期执行并微信回传”流程尚未验收。

若 `status` 的 `collector` 长时间不是 `ok`，先检查微信网络、绑定状态和工作区日志。若 `execution_readiness` 不是 `ready`，先运行 `engine-status` 和 `codex login status`。若某次执行或发送状态不明，**先核对记录和实际结果**，不要盲目重复提交或外发。

## 当前验证边界

- **代码层验证：**测试覆盖队列并发、重启、文件证据失效、引用匹配、回声抑制、绑定和定时适配器。部署说明中的命令与当前 CLI 源码核对过。
- **本机实测记录：**已有真实 Codex CLI 的本地读文件和精确会话续聊记录；新版 `launchd` 的系统 `curl` 触发器只做过不发微信消息的真实探针。详见 [架构与验证范围](clawbot-projects.md)。
- **尚未验收：**其他人的全新 Mac 安装、真实联系人之外的绑定人端到端消息链路，以及新版定时任务到微信回传。不要把模拟测试结果当作这些能力的实测证明。
