import json
import os
import time

import httpx
import pytest

from baidumcp.api import PanClient
from baidumcp.auth import Token, TokenStore
from baidumcp.config import Settings, state_dir, token_path
from baidumcp.api import PanError
from baidumcp.config import write_private_json
from baidumcp.transfer import Downloader, Uploader, part_identity, resume_meta_path

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
    entry = pan._entry("/movies/m.mkv")
    write_private_json(resume_meta_path(target.resolve()), part_identity(entry))
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


@pytest.fixture
def no_sleep(monkeypatch):
    async def _noop(_):
        return None

    monkeypatch.setattr("baidumcp.api.asyncio.sleep", _noop)
    monkeypatch.setattr("baidumcp.transfer.asyncio.sleep", _noop)


async def test_upload_dir_rejects_symlink_outside_allowed(tmp_path, pan, client):
    allowed = tmp_path / "ok"
    (allowed / "proj").mkdir(parents=True)
    (allowed / "proj" / "a.txt").write_text("fine")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (allowed / "proj" / "leak.txt").symlink_to(secret)
    s = Settings(app_key="ak", secret_key="sk", chunk_size=1024, allowed_dirs=[allowed.resolve()])
    res = await Uploader(client, s).upload(str(allowed / "proj"), "/up")
    assert "/up/proj/a.txt" in pan.files
    assert "/up/proj/leak.txt" not in pan.files
    assert not any(v == b"TOP SECRET" for v in pan.files.values())
    assert any("leak.txt" in e["local"] for e in res.errors)


async def test_upload_source_vanishing_is_isolated(tmp_path, pan, client, settings, monkeypatch):
    src = tmp_path / "d"
    src.mkdir()
    for n in ("a", "b", "c"):
        (src / f"{n}.txt").write_bytes(os.urandom(100) + n.encode())
    up = Uploader(client, settings)
    orig = up.upload_file

    async def flaky(local, remote, rtype, progress=None):
        if local.name == "b.txt":
            local.unlink()
        return await orig(local, remote, rtype, progress)

    monkeypatch.setattr(up, "upload_file", flaky)
    res = await up.upload(str(src), "/x")
    d = res.to_dict()
    assert d["transferred"] == 2 and d["failed"] == 1
    assert {"/x/d/a.txt", "/x/d/c.txt"} <= set(pan.files)


async def test_download_part_symlink_not_followed(tmp_path, pan, client, settings):
    data = os.urandom(3000)
    pan.put("/f.bin", data)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"protected")
    work = tmp_path / "work"
    work.mkdir()
    (work / "f.bin.baidumcp.part").symlink_to(outside)
    write_private_json(resume_meta_path((work / "f.bin").resolve()), part_identity(pan._entry("/f.bin")))
    res = await Downloader(client, settings).download("/f.bin", str(work / "f.bin"))
    assert res.to_dict()["ok"], res.errors
    assert outside.read_bytes() == b"protected"
    assert (work / "f.bin").read_bytes() == data and not (work / "f.bin").is_symlink()


async def test_download_discards_stale_partial(tmp_path, pan, client, settings):
    old, new = os.urandom(5000), os.urandom(5000)
    pan.put("/doc.bin", new)
    (tmp_path / "doc.bin.baidumcp.part").write_bytes(old[:2000])
    stale = part_identity({**pan._entry("/doc.bin"), "fs_id": 1})  # 旧版本文件
    write_private_json(resume_meta_path((tmp_path / "doc.bin").resolve()), stale)
    res = await Downloader(client, settings).download("/doc.bin", str(tmp_path / "doc.bin"))
    assert res.to_dict()["ok"], res.errors
    assert (tmp_path / "doc.bin").read_bytes() == new
    assert pan.range_requests == []
    assert not resume_meta_path((tmp_path / "doc.bin").resolve()).exists()


async def test_download_partial_without_metadata_restarts(tmp_path, pan, client, settings):
    data = os.urandom(4000)
    pan.put("/z.bin", data)
    (tmp_path / "z.bin.baidumcp.part").write_bytes(b"junk" * 100)
    res = await Downloader(client, settings).download("/z.bin", str(tmp_path / "z.bin"))
    assert res.to_dict()["ok"]
    assert (tmp_path / "z.bin").read_bytes() == data and pan.range_requests == []


async def test_repeated_416_is_bounded(tmp_path, pan, client, settings, no_sleep):
    pan.put("/r.bin", os.urandom(100))
    real = pan.handle
    hits = []

    def always_416(req):
        if req.url.path.startswith("/file/"):
            hits.append(1)
            return httpx.Response(416)
        return real(req)

    pan.handle = always_416
    res = await Downloader(client, settings).download("/r.bin", str(tmp_path / "r.bin"))
    assert res.errors and "416" in res.errors[0]["error"]
    assert len(hits) == 6  # 1 次 + 5 次重试


async def test_http_error_without_errno_is_not_success(tmp_path, pan, client, settings, no_sleep):
    pan.put("/dir/a.txt", b"a")
    real = pan.handle
    calls = []

    def gateway_503(req):
        if req.url.params.get("method") == "listall":
            calls.append(1)
            return httpx.Response(503, json={"error": "Service Unavailable"})
        return real(req)

    pan.handle = gateway_503
    with pytest.raises(PanError, match="503"):
        await Downloader(client, settings).download("/dir", str(tmp_path))
    assert len(calls) == 4  # 1 次 + 3 次重试


async def test_download_leaves_unrelated_user_files_alone(tmp_path, pan, client, settings):
    pan.put("/a", b"remote-a")
    work = tmp_path / "work"
    work.mkdir()
    user_file = work / "a.baidumcp.part.json"
    user_file.write_text("my own notes")
    res = await Downloader(client, settings).download("/a", str(work / "a"))
    assert res.to_dict()["ok"], res.errors
    assert user_file.read_text() == "my own notes"
    assert sorted(p.name for p in work.iterdir()) == ["a", "a.baidumcp.part.json"]


def test_private_json_writer_ignores_planted_tmp_symlink(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("protected")
    d = tmp_path / "d"
    d.mkdir()
    for name in ("x.json.tmp", ".x.json.tmp"):
        (d / name).symlink_to(outside)
    write_private_json(d / "x.json", {"k": 1})
    assert outside.read_text() == "protected"
    assert json.loads((d / "x.json").read_text()) == {"k": 1}
    assert oct((d / "x.json").stat().st_mode & 0o777) == "0o600"


async def test_upload_dir_symlink_loop_is_isolated(tmp_path, pan, client, settings, monkeypatch):
    src = tmp_path / "loopdir"
    src.mkdir()
    (src / "good.txt").write_bytes(b"good")
    (src / "loop_a").symlink_to(src / "loop_b")
    (src / "loop_b").symlink_to(src / "loop_a")

    # 模拟 Python 3.10–3.12：resolve() 遇到符号链接循环时抛 RuntimeError
    real_resolve = type(src).resolve

    def resolve(self, strict=False):
        if self.name.startswith("loop_"):
            raise RuntimeError(f"Symlink loop from {self}")
        return real_resolve(self, strict)

    monkeypatch.setattr(type(src), "resolve", resolve)
    res = await Uploader(client, settings).upload(str(src), "/l")
    d = res.to_dict()
    assert "/l/loopdir/good.txt" in pan.files
    assert d["transferred"] == 1 and d["failed"] == 2
    assert all("符号链接循环" in e["error"] for e in d["errors"])
