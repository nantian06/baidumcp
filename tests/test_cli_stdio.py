import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def test_serve_stdio_end_to_end(tmp_path):
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "baidumcp", "serve", "--no-official"],
        env={"BAIDUMCP_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    async with Client(transport) as c:
        names = {t.name for t in await c.list_tools()}
        assert "upload_local_file" in names
        r = await c.call_tool("auth_status", {})
        assert r.structured_content["authorized"] is False
