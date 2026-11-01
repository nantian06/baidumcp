import json
import os
import time

import httpx
import pytest

from baidumcp.api import PanClient
from baidumcp.auth import Token, TokenStore
from baidumcp.config import Settings, state_dir, token_path
from baidumcp.transfer import Downloader, Uploader

from fakepan import FakePan


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIDUMCP_HOME", str(tmp_path / "home"))
    for k in ("BAIDU_APP_KEY", "BAIDU_SECRET_KEY", "BAIDU_NETDISK_ACCESS_TOKEN", "BAIDUMCP_ALLOWED_DIRS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def pan():
    return FakePan()


@pytest.fixture
def settings():
    return Settings(app_key="ak", secret_key="sk", chunk_size=1024)


@pytest.fixture
async def client(pan, settings):
    http = httpx.AsyncClient(transport=pan.transport(), headers={"User-Agent": "pan.baidu.com"}, follow_redirects=True)
    store = TokenStore(settings, http=http)
    store.save(Token(access_token="tok-1", refresh_token="rt-1", expires_at=time.time() + 30 * 86400))
    c = PanClient(store, http=http)
    yield c
    await http.aclose()


async def test_upload_multi_chunk(tmp_path, pan, client, settings):
    data = os.urandom(3500)
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    res = await Uploader(client, settings).upload(str(f), "/apps/t/")
    assert res.to_dict()["ok"], res.errors
    assert pan.files["/apps/t/a.bin"] == data
    assert pan.parts_uploaded == 4  # 3500 / 1024 -> 4 片
    assert not list((state_dir() / "uploads").glob("*.json"))  # 完成后清理续传记录


async def test_upload_resume_after_failure(tmp_path, pan, client, settings, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("baidumcp.api.asyncio.sleep", no_sleep)
    data = os.urandom(5000)
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    up = Uploader(client, settings)
    pan.fail_part_after = 2
    res = await up.upload(str(f), "/apps/t/big.bin")
    assert res.errors and not res.files
    assert pan.parts_uploaded == 2

    pan.fail_part_after = None
    res = await up.upload(str(f), "/apps/t/big.bin")
    assert res.to_dict()["ok"], res.errors
    assert res.files[0]["resumed"] is True
    assert pan.parts_uploaded == 5  # 只补传剩下的 3 片
    assert pan.files["/apps/t/big.bin"] == data


async def test_rapid_upload(tmp_path, pan, client, settings):
    data = b"same content" * 100
    pan.put("/existing.bin", data)
    f = tmp_path / "copy.bin"
    f.write_bytes(data)
    res = await Uploader(client, settings).upload(str(f), "/apps/t/copy.bin")
    assert res.files[0]["rapid_upload"] is True
    assert pan.parts_uploaded == 0


async def test_directory_roundtrip(tmp_path, pan, client, settings):
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "x.txt").write_bytes(b"hello x")
    (src / "sub" / "y.txt").write_bytes(os.urandom(2100))
    (src / "empty.txt").write_bytes(b"")
    res = await Uploader(client, settings).upload(str(src), "/backup")
    assert res.to_dict()["ok"], res.errors
    assert set(pan.files) >= {"/backup/proj/x.txt", "/backup/proj/sub/y.txt", "/backup/proj/empty.txt"}

    out = tmp_path / "out"
    out.mkdir()
    res = await Downloader(client, settings).download("/backup/proj", str(out))
    assert res.to_dict()["ok"], res.errors
    for rel in ("x.txt", "sub/y.txt", "empty.txt"):
        assert (out / "proj" / rel).read_bytes() == (src / rel).read_bytes()

    # 再下一次：同大小文件全部跳过
    res = await Downloader(client, settings).download("/backup/proj", str(out))
    assert res.to_dict()["skipped"] == 3 and res.to_dict()["transferred"] == 0


async def test_download_resume_with_range(tmp_path, pan, client, settings):
    data = os.urandom(10000)
    pan.put("/movies/m.mkv", data)
    target = tmp_path / "m.mkv"
    (tmp_path / "m.mkv.baidumcp.part").write_bytes(data[:4000])
    res = await Downloader(client, settings).download("/movies/m.mkv", str(target))
    assert res.to_dict()["ok"], res.errors
    assert target.read_bytes() == data
    assert pan.range_requests == ["bytes=4000-"]
    assert not (tmp_path / "m.mkv.baidumcp.part").exists()


async def test_download_into_dir_and_conflict(tmp_path, pan, client, settings):
    pan.put("/docs/r.pdf", b"v2-content")
    (tmp_path / "r.pdf").write_bytes(b"old")
    res = await Downloader(client, settings).download("/docs/r.pdf", str(tmp_path) + "/")
    assert res.errors and "已存在" in res.errors[0]["error"]
    res = await Downloader(client, settings).download("/docs/r.pdf", str(tmp_path), overwrite=True)
    assert res.to_dict()["ok"]
    assert (tmp_path / "r.pdf").read_bytes() == b"v2-content"


async def test_token_auto_refresh_on_auth_error(tmp_path, pan, client, settings):
    pan.valid_token = "tok-server-rotated"  # 本地令牌被服务端判定失效
    pan.refresh_token = "rt-1"
    pan.put("/a.txt", b"abc")
    res = await Downloader(client, settings).download("/a.txt", str(tmp_path / "a.txt"))
    assert res.to_dict()["ok"], res.errors
    assert pan.refresh_count == 1
    saved = json.loads(token_path().read_text())
    assert saved["access_token"] == "tok-2" and saved["refresh_token"] == "rt-2"
    assert oct(token_path().stat().st_mode & 0o777) == "0o600"


async def test_proactive_refresh_before_expiry(pan, client):
    client.tokens.save(Token(access_token="tok-1", refresh_token="rt-1", expires_at=time.time() + 3600))
    assert await client.tokens.get() == "tok-2"
    # 再次获取不会重复刷新（refresh_token 只能用一次）
    assert await client.tokens.get() == "tok-2"
    assert pan.refresh_count == 1


async def test_allowed_dirs(tmp_path, client):
    s = Settings(app_key="ak", secret_key="sk", chunk_size=1024, allowed_dirs=[(tmp_path / "ok").resolve()])
    (tmp_path / "ok").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with pytest.raises(PermissionError):
        await Uploader(client, s).upload(str(secret), "/x/")
    with pytest.raises(PermissionError):
        await Uploader(client, s).upload(str(tmp_path / "ok" / ".." / "secret.txt"), "/x/")
