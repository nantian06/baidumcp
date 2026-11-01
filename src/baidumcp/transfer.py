"""上传/下载：分片、秒传、断点续传、目录递归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .api import PanClient, PanError, normalize_remote
from .config import Settings, state_dir, write_private_json

ProgressFn = Callable[[int, int, str], Awaitable[None] | None]

RTYPE = {"fail": 0, "rename": 1, "overwrite": 3}
SLICE_MD5_SIZE = 256 * 1024
# 百度的 uploadid 有效期有限，超过这个时间的续传记录直接丢弃重来
UPLOAD_STATE_TTL = 20 * 3600


async def _emit(progress: ProgressFn | None, done: int, total: int, msg: str) -> None:
    if progress is None:
        return
    r = progress(done, total, msg)
    if asyncio.iscoroutine(r):
        await r


@dataclass
class TransferResult:
    files: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(int(f.get("size", 0)) for f in self.files)

    def to_dict(self) -> dict:
        return {
            "ok": not self.errors,
            "transferred": len(self.files),
            "skipped": len(self.skipped),
            "failed": len(self.errors),
            "bytes": self.bytes,
            "files": self.files,
            "skipped_files": self.skipped,
            "errors": self.errors,
        }


# ======================= 上传 =======================


def _hash_file(path: Path, chunk_size: int) -> tuple[list[str], str, str]:
    """返回 (每个分片的 md5 列表, 整个文件 md5, 前 256KB md5)。"""
    blocks: list[str] = []
    whole = hashlib.md5()
    slice_md5 = None
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            blocks.append(hashlib.md5(chunk).hexdigest())
            whole.update(chunk)
            if slice_md5 is None:
                slice_md5 = hashlib.md5(chunk[:SLICE_MD5_SIZE]).hexdigest()
    if not blocks:  # 空文件
        empty = hashlib.md5(b"").hexdigest()
        return [empty], empty, empty
    return blocks, whole.hexdigest(), slice_md5 or ""


class Uploader:
    def __init__(self, client: PanClient, settings: Settings):
        self.client = client
        self.settings = settings

    def _state_file(self, local: Path, remote: str, stat: os.stat_result) -> Path:
        key = f"{local}|{remote}|{stat.st_size}|{stat.st_mtime_ns}|{self.settings.chunk_size}"
        return state_dir() / "uploads" / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    async def upload(self, local_path: str, remote_path: str, on_conflict: str = "rename", progress: ProgressFn | None = None) -> TransferResult:
        """local_path 可以是文件或目录。

        remote_path 以 / 结尾、或指向已存在的网盘目录、或 local_path 是目录时，视为目标目录。
        """
        if on_conflict not in RTYPE:
            raise ValueError(f"on_conflict 只能是 {list(RTYPE)}")
        local = self.settings.check_local_path(local_path)
        if not local.exists():
            raise FileNotFoundError(f"本地路径不存在：{local}")
        remote = normalize_remote(remote_path)
        as_dir = remote_path.endswith("/") or local.is_dir()
        if not as_dir:
            try:
                as_dir = bool((await self.client.stat(remote)).get("isdir"))
            except PanError as e:
                if e.errno != -9:
                    raise

        if local.is_file():
            target = posixpath.join(remote, local.name) if as_dir else remote
            jobs = [(local, target)]
        else:
            base = posixpath.join(remote, local.name)
            jobs = [
                (p, posixpath.join(base, *p.relative_to(local).parts))
                for p in sorted(local.rglob("*"))
                if p.is_file()
            ]

        total = sum(p.stat().st_size for p, _ in jobs)
        result = TransferResult()
        done_before = 0
        for src, dst in jobs:
            async def file_progress(done: int, _total: int, _msg: str, _src=src, _base=done_before) -> None:
                await _emit(progress, _base + done, total, f"上传 {_src.name}")

            try:
                info = await self.upload_file(src, dst, RTYPE[on_conflict], file_progress)
                result.files.append(info)
            except Exception as e:  # noqa: BLE001 —— 单个文件失败不影响其余文件
                result.errors.append({"local": str(src), "remote": dst, "error": str(e)})
            done_before += src.stat().st_size
        await _emit(progress, total, total, "上传完成")
        return result

    async def upload_file(self, local: Path, remote: str, rtype: int, progress: ProgressFn | None = None) -> dict:
        st = local.stat()
        size = st.st_size
        chunk_size = self.settings.chunk_size
        blocks, content_md5, slice_md5 = await asyncio.to_thread(_hash_file, local, chunk_size)

        state_file = self._state_file(local, remote, st)
        state = {}
        try:
            state = json.loads(state_file.read_text())
            if time.time() - state.get("created", 0) > UPLOAD_STATE_TTL:
                state = {}
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        if state.get("uploadid"):
            uploadid = state["uploadid"]
            pending = [i for i in range(len(blocks)) if i not in set(state.get("done", []))]
            resumed = True
        else:
            pre = await self.client.precreate(remote, size, blocks, rtype, content_md5, slice_md5)
            if pre.get("return_type") == 2:  # 秒传命中
                info = pre.get("info") or {}
                await _emit(progress, size, size, "秒传")
                return {"local": str(local), "remote": info.get("path", remote), "size": size, "fs_id": info.get("fs_id"), "rapid_upload": True}
            uploadid = pre["uploadid"]
            pending = pre.get("block_list")
            if pending is None:
                pending = list(range(len(blocks)))
            state = {"uploadid": uploadid, "done": [], "created": time.time(), "local": str(local), "remote": remote}
            write_private_json(state_file, state)
            resumed = False

        done_set = set(state.get("done", []))
        uploaded = sum(min(chunk_size, size - i * chunk_size) for i in done_set)
        with local.open("rb") as f:
            for idx in pending:
                f.seek(idx * chunk_size)
                chunk = f.read(chunk_size)
                await self.client.upload_part(remote, uploadid, idx, chunk)
                done_set.add(idx)
                state["done"] = sorted(done_set)
                write_private_json(state_file, state)
                uploaded += len(chunk)
                await _emit(progress, uploaded, size, f"分片 {idx + 1}/{len(blocks)}")

        try:
            created = await self.client.create(remote, size, uploadid, blocks, rtype)
        except PanError:
            # uploadid 过期等情况：清掉续传记录，下次重新开始
            state_file.unlink(missing_ok=True)
            raise
        state_file.unlink(missing_ok=True)
        return {
            "local": str(local),
            "remote": created.get("path", remote),
            "size": int(created.get("size", size)),
            "fs_id": created.get("fs_id"),
            "md5": content_md5,
            "resumed": resumed,
        }


# ======================= 下载 =======================

PART_SUFFIX = ".baidumcp.part"


class Downloader:
    def __init__(self, client: PanClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def download(self, remote_path: str, local_path: str, overwrite: bool = False, progress: ProgressFn | None = None) -> TransferResult:
        """remote_path 可以是文件或目录。

        local_path 是已存在的目录或以路径分隔符结尾时，文件放到该目录下（保留原文件名）；
        下载网盘目录时，会在 local_path 下创建同名目录。
        """
        remote = normalize_remote(remote_path)
        dest = self.settings.check_local_path(local_path)
        into_dir = local_path.endswith(("/", os.sep)) or dest.is_dir()
        meta = await self.client.stat(remote)

        if meta.get("isdir"):
            root = dest / posixpath.basename(remote) if remote != "/" else dest
            entries = [e for e in await self.client.list_all(remote) if not e.get("isdir")]
            jobs = []
            for e in entries:
                rel = posixpath.relpath(e["path"], remote)
                jobs.append((e, self.settings.check_local_path(root.joinpath(*rel.split("/")))))
        else:
            target = dest / meta["server_filename"] if into_dir else dest
            jobs = [(meta, target)]

        total = sum(int(e.get("size", 0)) for e, _ in jobs)
        result = TransferResult()
        done_before = 0
        # 一次性批量取下载链接（dlink 有效期 8 小时）
        dlinks = {}
        need = [e["fs_id"] for e, t in jobs if not self._skip(e, t, overwrite)]
        if need:
            for m in await self.client.filemetas(need):
                dlinks[m["fs_id"]] = m.get("dlink")

        for entry, target in jobs:
            size = int(entry.get("size", 0))
            if self._skip(entry, target, overwrite):
                result.skipped.append({"remote": entry["path"], "local": str(target), "reason": "本地已存在同大小文件"})
                done_before += size
                continue
            if target.exists() and not overwrite:
                result.errors.append({"remote": entry["path"], "local": str(target), "error": "本地文件已存在且大小不同（可设置 overwrite=true）"})
                done_before += size
                continue

            async def file_progress(done: int, _t: int, _m: str, _e=entry, _base=done_before) -> None:
                await _emit(progress, _base + done, total, f"下载 {posixpath.basename(_e['path'])}")

            try:
                dlink = dlinks.get(entry["fs_id"])
                if not dlink:
                    raise PanError(-1, "未获取到下载链接")
                await self._fetch(dlink, target, size, file_progress)
                result.files.append({"remote": entry["path"], "local": str(target), "size": size})
            except Exception as e:  # noqa: BLE001
                result.errors.append({"remote": entry["path"], "local": str(target), "error": str(e)})
            done_before += size
        await _emit(progress, total, total, "下载完成")
        return result

    @staticmethod
    def _skip(entry: dict, target: Path, overwrite: bool) -> bool:
        return not overwrite and target.is_file() and target.stat().st_size == int(entry.get("size", -1))

    async def _fetch(self, dlink: str, target: Path, size: int, progress: ProgressFn | None, retries: int = 5) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + PART_SUFFIX)
        attempt = 0
        while True:
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            if have == size and size > 0:
                break
            token = await self.client.tokens.get()
            headers = {"Range": f"bytes={have}-"} if have else {}
            sep = "&" if "?" in dlink else "?"
            try:
                async with self.client.http.stream("GET", f"{dlink}{sep}access_token={token}", headers=headers, timeout=httpx.Timeout(60, read=120)) as resp:
                    if resp.status_code == 416:
                        part.unlink(missing_ok=True)
                        continue
                    if resp.status_code not in (200, 206):
                        body = (await resp.aread())[:300].decode("utf-8", "replace")
                        raise PanError(-1, f"HTTP {resp.status_code} {body}")
                    mode = "ab" if resp.status_code == 206 and have else "wb"
                    written = have if mode == "ab" else 0
                    with part.open(mode) as f:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                            await _emit(progress, written, size, "")
                break
            except (httpx.TransportError, PanError) as e:
                attempt += 1
                if attempt > retries:
                    raise PanError(-1, f"下载失败（已保留 {part.name}，可重试续传）：{e}") from e
                await asyncio.sleep(min(2**attempt, 30))

        actual = part.stat().st_size if part.exists() else 0
        if actual != size:
            raise PanError(-1, f"文件大小不一致：期望 {size}，实际 {actual}（已保留 {part.name}，可重试续传）")
        os.replace(part, target)
