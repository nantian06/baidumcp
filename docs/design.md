# 逻辑设计与权限边界

## 项目定位

`baidumcp` 是本机运行的 Python 命令行工具和 MCP Server，连接两类能力：

1. 本机文件与百度网盘之间的上传、下载；
2. 对百度官方网盘 MCP 工具的代理接入。

它是按需启动的接入层，不是云端服务、自动同步程序或备份策略执行器。传输与网盘管理操作由用户配置的 Agent 或命令行显式发起。

## 总体结构

```text
MCP 客户端 / CLI
        │ stdio（默认）或 HTTP（Streamable HTTP）
        ▼
   baidumcp CLI
        │
        ▼
     FastMCP Server
      ├─ 本地传输工具
      │    └─ Uploader / Downloader → PanClient → 百度网盘开放平台 API
      └─ 官方工具代理（默认启用）
           └─ FastMCPProxy → 百度官方 MCP（SSE）

 TokenStore：读取本机令牌；需要时刷新并提供给 API 客户端和官方代理
 Settings：读取应用凭据、令牌、路径白名单、分片大小及官方 SSE 地址
```

`baidumcp serve` 默认同时挂载本地传输工具和官方代理。`baidumcp serve --no-official` 只启动本地传输及授权状态工具；`--transport http` 使用 Streamable HTTP，默认绑定 `127.0.0.1:8765`。

## 组件职责

| 组件 | 主要职责 |
|---|---|
| `cli.py` | `auth`、`status`、`token`、`config`、`serve`、`upload`、`download` 命令及参数入口 |
| `config.py` | 读取环境变量和配置文件；计算配置/状态目录；校验本地路径；以原子写入方式保存敏感 JSON |
| `auth.py` | 设备码/授权码 OAuth、令牌存取、到期前刷新；刷新时使用文件锁协调多个进程 |
| `server.py` | 注册本地 MCP 工具；可选挂载官方 SSE MCP；支持传输进度、后台任务及任务状态查询 |
| `api.py` | 封装上传/下载所需的百度网盘开放平台 API 请求、错误处理、退避重试与令牌失效刷新 |
| `transfer.py` | 目录遍历、冲突处理、分片上传、下载 Range 续传、进度与逐文件结果 |
| `agents.py` | 输出 Claude Code、Claude Desktop、Cursor、Windsurf、Cline、Gemini CLI、VS Code、Codex、Hermes、opencode 等客户端的配置片段 |

## 关键流程

### 授权与令牌

- `baidumcp auth` 默认走设备码授权；`baidumcp auth --code` 使用授权码流程。
- 应用凭据和令牌写入本机配置目录，不要求放入 Agent 的 MCP 配置。
- OAuth 令牌接近过期时可自动刷新；服务端明确返回令牌失效时，API 客户端也会尝试刷新一次。
- 刷新令牌具有轮换特性，因此多个本机进程刷新时会在支持文件锁的平台上串行化，并在拿锁后重新读取令牌。
- `BAIDU_NETDISK_ACCESS_TOKEN` 是静态令牌覆盖方式，不自动刷新。

### 本地文件上传

1. 解析并校验本地路径；配置了 `allowed_dirs` 时，只允许白名单目录本身及其子路径。
2. 单文件按分片大小读取并计算分片/整文件摘要，向开放平台申请上传；支持秒传、冲突策略和分片续传。
3. 目录按文件逐个上传，单个文件失败会进入结果的 `errors`，其余文件继续处理。
4. 未完成的上传状态保存于本机状态目录，记录续传所需信息；状态超过约 20 小时会被丢弃并重新开始。

### 网盘文件下载

1. 查询网盘文件或递归列出目录内容，批量取得下载链接。
2. 先写入目标旁的 `.baidumcp.part` 临时文件，网络中断时使用 HTTP Range 续传；续传元数据保存在本机状态目录。
3. 下载后核对文件字节数，再用原子替换将临时文件移动为目标文件。
4. 默认跳过本地已存在且大小相同的文件；同名但大小不同会报错，设置 `overwrite=true` 才覆盖。

大小核对不等于内容哈希验收；当前实现不会在下载完成后重新计算文件哈希。

### 官方 MCP 代理

统一模式在连接官方 SSE 服务时从 `TokenStore` 获取当前可用令牌，因此不需要把长期固定的 SSE 令牌写进 Agent 配置。官方代理连接失败会发出警告；本地上传/下载工具仍可使用。拆分模式（`baidumcp config <agent> --mode split`）会生成单独的官方 SSE 接入配置；生成结果可能包含 access token，且令牌会过期，必须按凭据处理，不要提交到仓库或公开粘贴。

## 配置和本机数据

配置目录优先级：设置 `BAIDUMCP_HOME` 时使用该目录；否则使用 `$XDG_CONFIG_HOME/baidumcp`；若未设置 `XDG_CONFIG_HOME`，默认是 `~/.config/baidumcp`。

| 路径 | 用途 |
|---|---|
| `config.json` | 百度应用 AppKey/SecretKey、路径白名单等配置 |
| `token.json` | OAuth access token、refresh token 及有效期 |
| `token.lock` | 协调多个进程刷新令牌 |
| `state/uploads/` | 未完成上传的续传记录 |
| `state/downloads/` | 下载部分文件的来源标识/续传元数据 |

受支持的配置项、Agent 接入样例和命令详见根目录 [README](../README.md)。敏感 JSON 写入时会尝试设置为 Unix 权限 `0600`；具体文件权限仍受操作系统和文件系统能力影响。续传记录可能包含本地/网盘路径，应与其他本机应用状态一样妥善保管。

## 安全边界与已知限制

- **网盘授权范围较广**：OAuth scope 为 `basic,netdisk`。启用官方代理后，Agent 可看到上游当前暴露的网盘工具，其中可能包括删除、移动、分享等管理操作。只连接可信 Agent，并按需在客户端禁用高风险工具。
- **本地路径默认不受限**：如果没有配置 `BAIDUMCP_ALLOWED_DIRS`、配置文件 `allowed_dirs` 或 `serve --allow-dir`，传输工具可访问当前进程权限范围内的本地路径。建议只开放完成任务所需的目录。
- **HTTP 模式默认无鉴权**：默认只监听回环地址 `127.0.0.1`。改变监听地址会使网络上的其他机器可能访问该 MCP 服务；除非已有额外访问控制，否则不要暴露到局域网或公网。
- **令牌输出与 split 配置**：`baidumcp token` 会把 access token 输出到标准输出；split 模式配置可能含令牌 URL。避免写入终端录屏、日志、共享配置或代码仓库。
- **后台任务状态仅在进程内**：MCP 后台任务的 `job_id`/状态保存在当前服务进程内，服务退出后不能再查询；重新发起传输时可利用磁盘上的续传记录继续未完成工作。
- **平台依赖**：OAuth、开放平台 API、官方 MCP SSE 和下载链接均依赖百度服务的可用性、权限和接口策略；本项目测试采用模拟服务，不代表真实账号、配额或平台行为已经验收。

## 开发与验证

Python 版本要求及依赖见根目录 `pyproject.toml`。在仓库根目录可创建开发环境并运行测试：

```bash
uv venv
uv pip install -e ".[dev]"
.venv/bin/pytest
```

测试使用模拟的 OAuth、网盘 API 和官方 MCP SSE 服务，不需要真实百度账号。修改文档或代码时，同步维护 [维护日志](changelog.md)；发布版本和代码变更仍以 Git 历史/标签为准。
