"""生成各种 Agent 的 MCP 配置片段。

两种接入模式：
- unified（推荐）：只配一个本地 stdio 服务，它同时提供传输工具并代理官方工具，令牌自动刷新。
- split：官方 SSE（远程）+ 本地传输服务（--no-official）分开配置。注意 SSE 地址里的令牌 30 天后过期，需要重新生成。
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys

AGENTS = {
    "claude-code": "Claude Code（命令行添加）",
    "claude-desktop": "Claude Desktop：claude_desktop_config.json",
    "cursor": "Cursor：~/.cursor/mcp.json",
    "windsurf": "Windsurf：~/.codeium/windsurf/mcp_config.json",
    "cline": "Cline / Roo Code：cline_mcp_settings.json",
    "gemini": "Gemini CLI：~/.gemini/settings.json",
    "vscode": "VS Code (Copilot)：.vscode/mcp.json",
    "codex": "OpenAI Codex CLI：~/.codex/config.toml",
    "hermes": "Hermes Agent：~/.hermes/config.yaml",
    "opencode": "opencode：opencode.json",
    "json": "通用 mcpServers JSON",
}

NAME = "baidu-netdisk"
LOCAL_NAME = "baidu-transfer"
OFFICIAL_NAME = "baidu-official"


def default_command() -> list[str]:
    exe = shutil.which("baidumcp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "baidumcp"]


def _servers(mode: str, command: list[str], sse_url: str | None) -> dict[str, dict]:
    """返回 {名字: {"command": [...]} 或 {"url": ...}}。"""
    if mode == "unified":
        return {NAME: {"command": command + ["serve"]}}
    return {
        OFFICIAL_NAME: {"url": sse_url or "https://mcp-pan.baidu.com/sse?access_token=<ACCESS_TOKEN>"},
        LOCAL_NAME: {"command": command + ["serve", "--no-official"]},
    }


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render(agent: str, mode: str = "unified", command: list[str] | None = None, sse_url: str | None = None) -> str:
    if agent not in AGENTS:
        raise ValueError(f"不支持的 agent：{agent}，可选：{', '.join(AGENTS)}")
    command = command or default_command()
    servers = _servers(mode, command, sse_url)

    if agent == "claude-code":
        lines = []
        for name, s in servers.items():
            if "url" in s:
                lines.append(f"claude mcp add --scope user --transport sse {name} {shlex.quote(s['url'])}")
            else:
                lines.append(f"claude mcp add --scope user {name} -- {shlex.join(s['command'])}")
        return "\n".join(lines)

    if agent in ("claude-desktop", "cursor", "windsurf", "cline", "gemini", "json"):
        out = {}
        for name, s in servers.items():
            if "url" in s and agent == "claude-desktop":
                # Claude Desktop 配置文件只支持 stdio，用 mcp-remote 桥接远程 SSE
                out[name] = {"command": "npx", "args": ["-y", "mcp-remote", s["url"]]}
            elif "url" in s:
                key = "serverUrl" if agent == "windsurf" else "url"
                entry = {key: s["url"]}
                if agent == "cline":
                    entry["type"] = "sse"
                out[name] = entry
            else:
                out[name] = {"command": s["command"][0], "args": s["command"][1:]}
        return json.dumps({"mcpServers": out}, ensure_ascii=False, indent=2)

    if agent == "vscode":
        out = {}
        for name, s in servers.items():
            if "url" in s:
                out[name] = {"type": "sse", "url": s["url"]}
            else:
                out[name] = {"type": "stdio", "command": s["command"][0], "args": s["command"][1:]}
        return json.dumps({"servers": out}, ensure_ascii=False, indent=2)

    if agent == "opencode":
        out = {}
        for name, s in servers.items():
            if "url" in s:
                out[name] = {"type": "remote", "url": s["url"], "enabled": True}
            else:
                out[name] = {"type": "local", "command": s["command"], "enabled": True}
        return json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": out}, ensure_ascii=False, indent=2)

    if agent == "codex":
        blocks = []
        for name, s in servers.items():
            if "url" in s:
                blocks.append(
                    f"# Codex 不支持旧版 SSE 传输，官方工具请用 unified 模式\n# [mcp_servers.{name}]\n# url = {_toml_str(s['url'])}"
                )
            else:
                args = ", ".join(_toml_str(a) for a in s["command"][1:])
                blocks.append(
                    f"[mcp_servers.{name}]\ncommand = {_toml_str(s['command'][0])}\nargs = [{args}]\n"
                    "startup_timeout_sec = 30\ntool_timeout_sec = 3600"
                )
        return "\n\n".join(blocks)

    if agent == "hermes":
        lines = ["mcp_servers:"]
        for name, s in servers.items():
            key = name.replace("-", "_")
            lines.append(f"  {key}:")
            if "url" in s:
                lines.append(f"    url: {_yaml_str(s['url'])}")
            else:
                lines.append(f"    command: {_yaml_str(s['command'][0])}")
                args = ", ".join(_yaml_str(a) for a in s["command"][1:])
                lines.append(f"    args: [{args}]")
        return "\n".join(lines)

    raise AssertionError(agent)
