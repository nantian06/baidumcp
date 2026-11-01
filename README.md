# baidumcp

百度网盘 MCP 整合层：**一个入口**同时提供“本机 ⇄ 网盘”的上传下载，以及百度官方 MCP 的浏览、搜索、管理工具，方便各种 Agent 快速接入。

```
Agent（Claude Code / Codex / Hermes / Cursor / …）
  └─ baidumcp serve（stdio 或 streamable HTTP）
       ├─ 本地传输工具（走你自己的开放平台应用 API）
       │    upload_local_file · download_file_to_local · transfer_status · auth_status
       └─ 代理百度官方 MCP（远程 SSE，令牌自动注入、自动刷新）
            file_list · file_keyword_search · file_semantics_search · file_move · file_sharelink_set · …
```

## 为什么要整合

| 痛点 | 直接用官方 MCP | baidumcp |
|---|---|---|
| 本地文件上传 | 只在官方 Python stdio 模式里有 | ✅ 分片上传、秒传、断点续传，支持目录 |
| 下载到本机 | ❌ 没有 | ✅ Range 断点续传，支持目录 |
| 令牌过期 | SSE 地址里写死 access_token，30 天后失效要手动改配置 | ✅ 存在本地，到期前用 refresh_token 自动刷新，多进程安全 |
| 只支持 stdio / HTTP 的 Agent（如 Codex） | 接不了 SSE | ✅ 通过本地代理也能用官方工具 |
| 配置 | 每个 Agent 格式不一样 | ✅ `baidumcp config <agent>` 直接生成 |

## 快速开始

### 1. 安装

```bash
uv tool install git+https://github.com/nantian06/baidumcp
```

或者不安装、每次用 `uvx` 运行：`uvx --from git+https://github.com/nantian06/baidumcp baidumcp ...`

### 2. 创建开放平台应用

在 [百度网盘开放平台控制台](https://pan.baidu.com/union/console) 创建一个应用，拿到 **AppKey** 和 **SecretKey**。

### 3. 授权（只需一次）

```bash
baidumcp auth
```

默认使用**设备码模式**：命令会打印一个网址和用户码，在浏览器打开并登录授权即可。如果你的应用不支持设备码模式，改用授权码模式：

```bash
baidumcp auth --code
```

AppKey、SecretKey 和令牌都保存在 `~/.config/baidumcp/`（权限 600），Agent 的配置里**不需要放任何密钥**。

已有 access_token 也可以直接保存（没有 refresh_token 时 30 天后需要重新授权）：

```bash
baidumcp auth --token <ACCESS_TOKEN>
```

### 4. 接入 Agent

```bash
baidumcp config --list
baidumcp config claude-code
```

常见 Agent 的配置如下（`baidumcp config` 会自动填入本机 `baidumcp` 的绝对路径）。

**Claude Code**

```bash
claude mcp add --scope user baidu-netdisk -- baidumcp serve
```

**Hermes Agent**（`~/.hermes/config.yaml`，改完在会话里执行 `/reload-mcp`）

```yaml
mcp_servers:
  baidu_netdisk:
    command: "baidumcp"
    args: ["serve"]
```

**OpenAI Codex CLI**（`~/.codex/config.toml`）

```toml
[mcp_servers.baidu-netdisk]
command = "baidumcp"
args = ["serve"]
startup_timeout_sec = 30
tool_timeout_sec = 3600
```

**Cursor / Claude Desktop / Windsurf / Cline / Gemini CLI**（`mcpServers` 格式）

```json
{
  "mcpServers": {
    "baidu-netdisk": { "command": "baidumcp", "args": ["serve"] }
  }
}
```

VS Code、opencode 等其他格式请运行 `baidumcp config vscode` / `baidumcp config opencode`。

## 接入模式

| 模式 | 命令 | 适用场景 |
|---|---|---|
| **unified**（默认，推荐） | `baidumcp serve` | 一个入口，官方工具和传输工具都有 |
| **split** | 官方 SSE + `baidumcp serve --no-official` | 你的 Agent 已经接了官方 SSE，只想补上传输能力。用 `baidumcp config <agent> --mode split` 生成配置。注意 SSE 地址里的令牌 30 天后过期 |
| **共享 HTTP** | `baidumcp serve --transport http --port 8765` | 多个 Agent 共用同一个进程，Agent 侧配置 `http://127.0.0.1:8765/mcp` |

官方服务连不上时，本地传输工具照常可用。

## 工具

| 工具 | 说明 |
|---|---|
| `upload_local_file(local_path, remote_path, on_conflict="rename", background=false)` | 上传本机文件或目录。`remote_path` 以 `/` 结尾、指向已有网盘目录、或本地是目录时，作为目标目录。`on_conflict`：`rename` / `overwrite` / `fail` |
| `download_file_to_local(remote_path, local_path, overwrite=false, background=false)` | 下载网盘文件或目录。`local_path` 是已有目录或以 `/` 结尾时保存到其中；同大小文件自动跳过 |
| `transfer_status(job_id=None)` | 查看后台任务（`background=true` 时返回 `job_id`） |
| `auth_status()` | 检查授权状态、过期时间、能否自动刷新 |
| 官方工具 | `file_list`、`file_meta`、`make_dir`、`file_copy`、`file_move`、`file_rename`、`file_del`、`file_keyword_search`、`file_semantics_search`、`file_sharelink_set`、`file_upload_by_url`、`user_info`、`get_quota` 等，以官方当前提供的为准 |

## 命令行（也可以当作 Agent Skill 用）

```bash
baidumcp upload ./report.pdf /apps/myapp/
baidumcp download /apps/myapp/report.pdf ./
baidumcp status
baidumcp token          # 输出当前可用的 access_token，供脚本使用
```

不支持 MCP 的 Agent 可以使用 [`skills/baidu-netdisk/SKILL.md`](skills/baidu-netdisk/SKILL.md)，它会教 Agent 调用这些命令。

## 配置项

| 环境变量 | 说明 |
|---|---|
| `BAIDU_APP_KEY` / `BAIDU_SECRET_KEY` | 覆盖 `baidumcp auth` 保存的应用信息 |
| `BAIDU_NETDISK_ACCESS_TOKEN` | 使用静态令牌（与官方 MCP 同名，不会自动刷新） |
| `BAIDUMCP_ALLOWED_DIRS` | **建议设置**：限制 Agent 能读写的本地目录，多个用 `:` 分隔（Windows 用 `;`）。也可用 `serve --allow-dir` |
| `BAIDUMCP_CHUNK_MB` | 分片大小，普通用户 4（默认），VIP 16，SVIP 32 |
| `BAIDUMCP_HOME` | 配置目录，默认 `~/.config/baidumcp` |
| `BAIDUMCP_OFFICIAL_SSE_URL` | 官方 SSE 地址，默认 `https://mcp-pan.baidu.com/sse` |

## 安全提示

- 授权范围是 `basic,netdisk`，相当于整个网盘的权限。Agent 能调用 `file_del` 等官方管理工具；如需限制，可在 Agent 侧过滤工具（例如 Hermes 的 `tools.exclude`）。
- 不设置 `BAIDUMCP_ALLOWED_DIRS` 时，Agent 可以上传本机任意可读文件，或写入任意可写位置。
- `--transport http` 默认只监听 `127.0.0.1`，没有鉴权，不要暴露到公网。

## 开发

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

测试使用模拟的百度 OAuth / xpan / PCS 接口和模拟的官方 SSE 服务，不需要真实账号。

## 参考

- [百度网盘官方 MCP](https://github.com/baidu-netdisk/mcp)
- [百度网盘开放平台文档](https://pan.baidu.com/union/doc/)
