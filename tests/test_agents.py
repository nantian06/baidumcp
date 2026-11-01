import json
import tomllib

import pytest

from baidumcp.agents import AGENTS, render

CMD = ["/usr/bin/baidumcp"]


@pytest.mark.parametrize("agent", list(AGENTS))
@pytest.mark.parametrize("mode", ["unified", "split"])
def test_render_parses(agent, mode):
    out = render(agent, mode, CMD, "https://mcp-pan.baidu.com/sse?access_token=T")
    if agent in ("cursor", "windsurf", "cline", "gemini", "json", "claude-desktop", "vscode", "opencode"):
        data = json.loads(out)
        assert "serve" in json.dumps(data)
    elif agent == "codex":
        data = tomllib.loads(out)
        assert data["mcp_servers"]
    elif agent == "hermes":
        assert out.startswith("mcp_servers:")
    elif agent == "claude-code":
        assert out.startswith("claude mcp add")


def test_unified_is_single_entry():
    data = json.loads(render("cursor", "unified", CMD))
    assert data == {"mcpServers": {"baidu-netdisk": {"command": "/usr/bin/baidumcp", "args": ["serve"]}}}


def test_split_local_disables_official():
    data = json.loads(render("json", "split", CMD, "https://x/sse?access_token=T"))
    assert data["mcpServers"]["baidu-transfer"]["args"] == ["serve", "--no-official"]
    assert data["mcpServers"]["baidu-official"]["url"].endswith("access_token=T")
