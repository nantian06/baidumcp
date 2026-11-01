import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from baidumcp.api import PanClient
from baidumcp.auth import Token, TokenStore
from baidumcp.config import Settings
from baidumcp.server import build_server

from fakepan import FakePan


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIDUMCP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BAIDU_NETDISK_ACCESS_TOKEN", raising=False)


def _data(result):
    return result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)


@pytest.fixture
async def setup(tmp_path):
    pan = FakePan()
    settings = Settings(app_key="ak", secret_key="sk", chunk_size=1024)
    http = httpx.AsyncClient(transport=pan.transport(), headers={"User-Agent": "pan.baidu.com"})
    store = TokenStore(settings, http=http)
    store.save(Token(access_token="tok-1", refresh_token="rt-1", expires_at=time.time() + 30 * 86400))
    yield pan, settings, PanClient(store, http=http)
    await http.aclose()


async def test_tools_listed_and_upload_download(tmp_path, setup):
    pan, settings, pc = setup
    server = build_server(settings, official=False, client=pc)
    f = tmp_path / "n.txt"
    f.write_bytes(os.urandom(3000))
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"upload_local_file", "download_file_to_local", "transfer_status", "auth_status"}
        r = _data(await c.call_tool("upload_local_file", {"local_path": str(f), "remote_path": "/apps/t/"}))
        assert r["ok"] and r["transferred"] == 1
        r = _data(await c.call_tool("download_file_to_local", {"remote_path": "/apps/t/n.txt", "local_path": str(tmp_path / "back.txt")}))
        assert r["ok"]
        assert (tmp_path / "back.txt").read_bytes() == f.read_bytes()
        st = _data(await c.call_tool("auth_status", {}))
        assert st["authorized"] and st["auto_refresh"]


async def test_background_job(tmp_path, setup):
    pan, settings, pc = setup
    server = build_server(settings, official=False, client=pc)
    f = tmp_path / "bg.bin"
    f.write_bytes(os.urandom(4096))
    async with Client(server) as c:
        r = _data(await c.call_tool("upload_local_file", {"local_path": str(f), "remote_path": "/bg/", "background": True}))
        job = r["job_id"]
        for _ in range(100):
            s = _data(await c.call_tool("transfer_status", {"job_id": job}))
            if s["status"] != "running":
                break
            await __import__("asyncio").sleep(0.05)
        assert s["status"] == "succeeded", s
        assert s["result"]["files"][0]["remote"] == "/bg/bg.bin"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def official_stub():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("official_stub.py")), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/sse"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(5)


async def test_official_proxy_uses_current_token(tmp_path, setup, official_stub):
    pan, settings, pc = setup
    settings.official_sse_url = official_stub
    server = build_server(settings, official=True, client=pc)
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"file_list", "upload_local_file"} <= names
        r = _data(await c.call_tool("file_list", {"dir": "/apps"}))
        assert r["token_seen"] == "tok-1"
        # 令牌刷新后，代理的新连接应使用新令牌
        await pc.tokens.refresh()
        r = _data(await c.call_tool("file_list", {"dir": "/apps"}))
        assert r["token_seen"] == "tok-2"


async def test_official_unreachable_keeps_local_tools(setup):
    pan, settings, pc = setup
    settings.official_sse_url = f"http://127.0.0.1:{_free_port()}/sse"
    server = build_server(settings, official=True, client=pc)
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert "upload_local_file" in names
