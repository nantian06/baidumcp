"""模拟官方 SSE MCP：记录建立 SSE 连接时 URL 上的 access_token，file_list 回显它。"""

import sys
from urllib.parse import parse_qs

import uvicorn
from fastmcp import FastMCP

mcp = FastMCP("official-stub")
seen = {"token": None}


@mcp.tool
def file_list(dir: str = "/") -> dict:
    """列目录（桩）。"""
    return {"dir": dir, "token_seen": seen["token"]}


inner = mcp.http_app(transport="sse")


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/sse":
        seen["token"] = parse_qs(scope.get("query_string", b"").decode()).get("access_token", [None])[0]
    await inner(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", lifespan="on")
