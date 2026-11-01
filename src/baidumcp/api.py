"""百度网盘开放平台（xpan）最小客户端：只覆盖传输所需的接口。"""

from __future__ import annotations

import asyncio
import json
import posixpath
from typing import Any

import httpx

from .auth import TokenStore
from .config import PAN_API_BASE, PCS_UPLOAD_BASE, USER_AGENT

# 令牌失效相关的错误码（xpan errno / openapi error_code）
TOKEN_ERRORS = {-6, 110, 111}

ERRNO_HINTS = {
    -6: "身份验证失败（令牌无效或已过期）",
    -7: "文件或目录名错误，或无权访问",
    -8: "文件或目录已存在",
    -9: "文件或目录不存在",
    2: "参数错误",
    31034: "接口请求过于频繁，请稍后再试",
    42000: "访问过于频繁",
}


class PanError(RuntimeError):
    def __init__(self, errno: int, message: str = "", data: Any = None):
        hint = ERRNO_HINTS.get(errno, "")
        super().__init__(f"百度网盘接口错误 errno={errno} {hint} {message}".strip())
        self.errno = errno
        self.data = data


def _errno(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    for key in ("errno", "error_code"):
        if key in data and data[key] not in (0, "0", None):
            try:
                return int(data[key])
            except (TypeError, ValueError):
                return -1
    return 0


class PanClient:
    def __init__(self, tokens: TokenStore, http: httpx.AsyncClient | None = None):
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def call(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        retries: int = 3,
    ) -> dict:
        """带 access_token 的 JSON 接口调用：令牌失效自动刷新一次，网络错误/频控退避重试。"""
        refreshed = False
        attempt = 0
        while True:
            token = await self.tokens.get()
            q = dict(params or {})
            q["access_token"] = token
            try:
                resp = await self.http.request(method, url, params=q, data=data, files=files)
                body = resp.json()
            except (httpx.TransportError, json.JSONDecodeError, ValueError) as e:
                attempt += 1
                if attempt > retries:
                    raise PanError(-1, f"请求失败：{e!r}") from e
                await asyncio.sleep(2**attempt)
                continue
            code = _errno(body)
            if code == 0 and resp.status_code >= 400:
                # HTTP 错误但 JSON 里没有 errno（如网关 503）：不能当成功，否则空列表会被当成空目录
                if (resp.status_code >= 500 or resp.status_code == 429) and attempt < retries:
                    attempt += 1
                    await asyncio.sleep(2**attempt)
                    continue
                raise PanError(-resp.status_code, f"HTTP {resp.status_code} {str(body)[:200]}", body)
            if code == 0:
                return body
            if code in TOKEN_ERRORS and not refreshed and self.tokens.can_refresh:
                refreshed = True
                await self.tokens.refresh(stale=token)
                continue
            if code in (31034, 42000) and attempt < retries:
                attempt += 1
                await asyncio.sleep(2**attempt)
                continue
            raise PanError(code, str(body.get("errmsg") or body.get("error_msg") or ""), body)

    # ---------- 查询 ----------
    async def list_dir(self, dir_path: str) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            body = await self.call(
                "GET",
                f"{PAN_API_BASE}/rest/2.0/xpan/file",
                params={"method": "list", "dir": dir_path, "start": start, "limit": 1000, "web": 0},
            )
            batch = body.get("list") or []
            items.extend(batch)
            if len(batch) < 1000:
                return items
            start += len(batch)

    async def stat(self, path: str) -> dict:
        """按路径取文件信息（通过列父目录实现）。"""
        path = normalize_remote(path)
        if path == "/":
            return {"path": "/", "isdir": 1, "server_filename": ""}
        parent, name = posixpath.split(path)
        for item in await self.list_dir(parent or "/"):
            if item.get("server_filename") == name or item.get("path") == path:
                return item
        raise PanError(-9, path)

    async def list_all(self, path: str) -> list[dict]:
        """递归列出目录下所有条目。"""
        items: list[dict] = []
        start = 0
        while True:
            body = await self.call(
                "GET",
                f"{PAN_API_BASE}/rest/2.0/xpan/multimedia",
                params={"method": "listall", "path": path, "recursion": 1, "start": start, "limit": 1000},
            )
            items.extend(body.get("list") or [])
            if not body.get("has_more"):
                return items
            start = int(body.get("cursor", start + 1000))

    async def filemetas(self, fs_ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(fs_ids), 100):
            body = await self.call(
                "GET",
                f"{PAN_API_BASE}/rest/2.0/xpan/multimedia",
                params={"method": "filemetas", "fsids": json.dumps(fs_ids[i : i + 100]), "dlink": 1},
            )
            out.extend(body.get("list") or [])
        return out

    # ---------- 上传三步 ----------
    async def precreate(self, path: str, size: int, block_list: list[str], rtype: int, content_md5: str | None = None, slice_md5: str | None = None) -> dict:
        data = {
            "path": path,
            "size": str(size),
            "isdir": "0",
            "autoinit": "1",
            "rtype": str(rtype),
            "block_list": json.dumps(block_list),
        }
        if content_md5 and slice_md5:
            data["content-md5"] = content_md5
            data["slice-md5"] = slice_md5
        return await self.call(
            "POST", f"{PAN_API_BASE}/rest/2.0/xpan/file", params={"method": "precreate"}, data=data
        )

    async def upload_part(self, path: str, uploadid: str, partseq: int, chunk: bytes) -> dict:
        return await self.call(
            "POST",
            f"{PCS_UPLOAD_BASE}/rest/2.0/pcs/superfile2",
            params={"method": "upload", "type": "tmpfile", "path": path, "uploadid": uploadid, "partseq": partseq},
            files={"file": ("blob", chunk, "application/octet-stream")},
        )

    async def create(self, path: str, size: int, uploadid: str, block_list: list[str], rtype: int) -> dict:
        data = {
            "path": path,
            "size": str(size),
            "isdir": "0",
            "rtype": str(rtype),
            "uploadid": uploadid,
            "block_list": json.dumps(block_list),
        }
        return await self.call("POST", f"{PAN_API_BASE}/rest/2.0/xpan/file", params={"method": "create"}, data=data)


def normalize_remote(path: str) -> str:
    path = "/" + path.strip().lstrip("/")
    norm = posixpath.normpath(path)
    return "/" if norm in (".", "//") else norm
