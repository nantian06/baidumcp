"""模拟百度 OAuth + xpan + PCS 接口，供测试使用（httpx.MockTransport）。"""

from __future__ import annotations

import hashlib
import itertools
import json
import posixpath
from email.parser import BytesParser
from urllib.parse import parse_qs

import httpx


class FakePan:
    def __init__(self, token: str = "tok-1", refresh_token: str = "rt-1"):
        self.valid_token = token
        self.refresh_token = refresh_token
        self.files: dict[str, bytes] = {}
        self.fs_ids: dict[str, int] = {}
        self.dirs: set[str] = {"/"}
        self.uploads: dict[str, dict] = {}
        self._ids = itertools.count(1000)
        self.calls: list[tuple[str, dict]] = []
        self.fail_part_after: int | None = None  # 上传第 N 个分片后注入网络故障
        self.parts_uploaded = 0
        self.range_requests: list[str] = []
        self.refresh_count = 0

    # ---------- 辅助 ----------
    def put(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.fs_ids[path] = next(self._ids)
        d = posixpath.dirname(path)
        while d not in self.dirs:
            self.dirs.add(d)
            d = posixpath.dirname(d)

    def _entry(self, path: str) -> dict:
        if path in self.files:
            return {"path": path, "server_filename": posixpath.basename(path), "isdir": 0, "size": len(self.files[path]), "fs_id": self.fs_ids[path]}
        return {"path": path, "server_filename": posixpath.basename(path), "isdir": 1, "size": 0, "fs_id": abs(hash(path)) % 10**9}

    def _children(self, d: str) -> list[str]:
        names = {p for p in list(self.files) + list(self.dirs) if p != "/" and posixpath.dirname(p) == d}
        return sorted(names)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: self.handle(req))

    # ---------- 路由 ----------
    def handle(self, req: httpx.Request) -> httpx.Response:
        q = {k: v for k, v in req.url.params.items()}
        host, path = req.url.host, req.url.path
        self.calls.append((f"{host}{path}:{q.get('method', '')}", q))

        if host == "openapi.baidu.com" and path == "/oauth/2.0/token":
            if q.get("grant_type") == "refresh_token" and q.get("refresh_token") == self.refresh_token:
                self.refresh_count += 1
                self.valid_token = f"tok-{self.refresh_count + 1}"
                self.refresh_token = f"rt-{self.refresh_count + 1}"
                return httpx.Response(200, json={"access_token": self.valid_token, "refresh_token": self.refresh_token, "expires_in": 2592000, "scope": "basic netdisk"})
            return httpx.Response(200, json={"error": "invalid_grant", "error_description": "bad refresh token"})

        if q.get("access_token") != self.valid_token:
            if host == "d.pcs.baidu.com" and path.startswith("/file/"):
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, json={"errno": -6, "errmsg": "auth fail"})

        if path == "/rest/2.0/xpan/file":
            m = q.get("method")
            if m == "list":
                d = q["dir"]
                if d not in self.dirs:
                    return httpx.Response(200, json={"errno": -9})
                return httpx.Response(200, json={"errno": 0, "list": [self._entry(p) for p in self._children(d)]})
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if m == "precreate":
                content_md5 = form.get("content-md5")
                for p, data in self.files.items():
                    if content_md5 and hashlib.md5(data).hexdigest() == content_md5:
                        self.put(form["path"], data)
                        return httpx.Response(200, json={"errno": 0, "return_type": 2, "info": self._entry(form["path"])})
                uid = f"up-{next(self._ids)}"
                blocks = json.loads(form["block_list"])
                self.uploads[uid] = {"path": form["path"], "parts": {}, "blocks": blocks}
                return httpx.Response(200, json={"errno": 0, "return_type": 1, "uploadid": uid, "block_list": list(range(len(blocks)))})
            if m == "create":
                up = self.uploads.get(form["uploadid"])
                if up is None:
                    return httpx.Response(200, json={"errno": 31363, "errmsg": "uploadid invalid"})
                blocks = json.loads(form["block_list"])
                for i, md5 in enumerate(blocks):
                    if hashlib.md5(up["parts"].get(i, b"")).hexdigest() != md5:
                        return httpx.Response(200, json={"errno": 31363, "errmsg": f"block {i} mismatch"})
                data = b"".join(up["parts"][i] for i in range(len(blocks)))
                if len(data) != int(form["size"]):
                    return httpx.Response(200, json={"errno": 2, "errmsg": "size mismatch"})
                target = form["path"]
                if target in self.files and form.get("rtype") == "1":
                    root, ext = posixpath.splitext(target)
                    target = f"{root}(1){ext}"
                self.put(target, data)
                return httpx.Response(200, json={"errno": 0, **self._entry(target)})

        if host == "d.pcs.baidu.com" and path == "/rest/2.0/pcs/superfile2":
            if self.fail_part_after is not None and self.parts_uploaded >= self.fail_part_after:
                raise httpx.ConnectError("injected failure")
            ctype = req.headers["content-type"]
            msg = BytesParser().parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + req.content)
            chunk = msg.get_payload()[0].get_payload(decode=True)
            self.uploads[q["uploadid"]]["parts"][int(q["partseq"])] = chunk
            self.parts_uploaded += 1
            return httpx.Response(200, json={"md5": hashlib.md5(chunk).hexdigest()})

        if path == "/rest/2.0/xpan/multimedia":
            m = q.get("method")
            if m == "listall":
                root = q["path"]
                items = [self._entry(p) for p in sorted(list(self.files) + list(self.dirs)) if p.startswith(root.rstrip("/") + "/")]
                return httpx.Response(200, json={"errno": 0, "list": items, "has_more": 0})
            if m == "filemetas":
                ids = json.loads(q["fsids"])
                by_id = {v: k for k, v in self.fs_ids.items()}
                return httpx.Response(200, json={"errno": 0, "list": [{**self._entry(by_id[i]), "dlink": f"https://d.pcs.baidu.com/file/{i}?fid=x"} for i in ids if i in by_id]})

        if host == "d.pcs.baidu.com" and path.startswith("/file/"):
            if req.headers.get("user-agent") != "pan.baidu.com":
                return httpx.Response(403, text="bad ua")
            fs_id = int(path.rsplit("/", 1)[1])
            data = self.files[{v: k for k, v in self.fs_ids.items()}[fs_id]]
            rng = req.headers.get("range")
            if rng:
                self.range_requests.append(rng)
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(data):
                    return httpx.Response(416)
                return httpx.Response(206, content=data[start:])
            return httpx.Response(200, content=data)

        return httpx.Response(404, json={"errno": 404, "errmsg": f"unknown {host}{path}"})
