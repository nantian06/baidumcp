"""百度开放平台 OAuth：设备码 / 授权码授权，令牌持久化与自动刷新。

- access_token 有效期 30 天，refresh_token 10 年且只能使用一次。
- 多个 Agent 各自拉起 stdio 进程时可能同时刷新，所以刷新过程加文件锁，
  拿到锁后先重新读取磁盘上的令牌，避免用掉已失效的 refresh_token。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import OAUTH_BASE, Settings, home_dir, read_json, token_path, write_private_json

# 距离过期不足这么多秒时提前刷新
REFRESH_MARGIN = 24 * 3600
SCOPE = "basic,netdisk"


class AuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # unix 时间戳；None 表示未知
    scope: str | None = None

    @classmethod
    def from_oauth(cls, data: dict) -> "Token":
        if "access_token" not in data:
            raise AuthError(f"OAuth 返回异常：{data}")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + int(data.get("expires_in", 2592000)),
            scope=data.get("scope"),
        )

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    def needs_refresh(self, margin: float = REFRESH_MARGIN) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at - margin


@contextlib.contextmanager
def _file_lock():
    lock_file = home_dir() / "token.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # Windows：退化为无锁
            pass
        yield
    finally:
        os.close(fd)


class TokenStore:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self._http = http
        self._lock = asyncio.Lock()

    # ---------- 持久化 ----------
    def load(self) -> Token | None:
        data = read_json(token_path())
        if not data.get("access_token"):
            return None
        return Token(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            scope=data.get("scope"),
        )

    def save(self, token: Token) -> None:
        write_private_json(token_path(), token.to_dict())

    @property
    def can_refresh(self) -> bool:
        return bool(self.settings.app_key and self.settings.secret_key)

    # ---------- 对外 ----------
    async def get(self) -> str:
        """返回可用的 access_token，必要时自动刷新。"""
        if self.settings.static_token:
            return self.settings.static_token
        token = self.load()
        if token is None:
            raise AuthError("尚未授权：请先运行 `baidumcp auth`，或设置 BAIDU_NETDISK_ACCESS_TOKEN")
        if token.needs_refresh() and token.refresh_token and self.can_refresh:
            token = await self.refresh(stale=token.access_token)
        return token.access_token

    async def refresh(self, stale: str | None = None) -> Token:
        """刷新令牌。stale 为调用方手里已失效的 token；若磁盘上已是新 token 则直接返回。"""
        if self.settings.static_token:
            raise AuthError("使用的是静态 BAIDU_NETDISK_ACCESS_TOKEN，无法自动刷新，请更换令牌")
        if not self.can_refresh:
            raise AuthError("缺少 BAIDU_APP_KEY / BAIDU_SECRET_KEY，无法刷新令牌，请重新运行 `baidumcp auth`")
        async with self._lock:
            # 文件锁是阻塞调用，放到线程里拿
            lock_cm = _file_lock()
            await asyncio.to_thread(lock_cm.__enter__)
            try:
                current = self.load()
                if current is None or not current.refresh_token:
                    raise AuthError("没有可用的 refresh_token，请重新运行 `baidumcp auth`")
                if stale is not None and current.access_token != stale and not current.needs_refresh():
                    return current  # 其他进程已经刷新过
                data = await self._oauth_token(
                    grant_type="refresh_token", refresh_token=current.refresh_token
                )
                token = Token.from_oauth(data)
                self.save(token)
                return token
            finally:
                lock_cm.__exit__(None, None, None)

    # ---------- OAuth 流程 ----------
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def _oauth_token(self, **params) -> dict:
        http = await self._client()
        params.update(client_id=self.settings.app_key, client_secret=self.settings.secret_key)
        resp = await http.get(f"{OAUTH_BASE}/token", params=params)
        data = resp.json()
        if "error" in data and "access_token" not in data:
            raise AuthError(f"{data.get('error')}: {data.get('error_description', '')}")
        return data

    def authorize_url(self) -> str:
        q = httpx.QueryParams(
            response_type="code",
            client_id=self.settings.app_key,
            redirect_uri="oob",
            scope=SCOPE,
            display="popup",
        )
        return f"{OAUTH_BASE}/authorize?{q}"

    async def exchange_code(self, code: str) -> Token:
        data = await self._oauth_token(grant_type="authorization_code", code=code.strip(), redirect_uri="oob")
        token = Token.from_oauth(data)
        self.save(token)
        return token

    async def device_flow(self, notify: Callable[[dict], None], timeout: float | None = None) -> Token:
        """设备码授权：notify 收到 {verification_url, user_code, qrcode_url, expires_in}。"""
        http = await self._client()
        resp = await http.get(
            f"{OAUTH_BASE}/device/code",
            params={"response_type": "device_code", "client_id": self.settings.app_key, "scope": SCOPE},
        )
        info = resp.json()
        if "device_code" not in info:
            raise AuthError(f"获取设备码失败：{info}")
        notify(info)
        interval = max(int(info.get("interval", 5)), 1)
        deadline = time.time() + (timeout or int(info.get("expires_in", 300)))
        while time.time() < deadline:
            await asyncio.sleep(interval)
            params = {
                "grant_type": "device_token",
                "code": info["device_code"],
                "client_id": self.settings.app_key,
                "client_secret": self.settings.secret_key,
            }
            data = (await http.get(f"{OAUTH_BASE}/token", params=params)).json()
            if "access_token" in data:
                token = Token.from_oauth(data)
                self.save(token)
                return token
            err = data.get("error")
            if err == "slow_down":
                interval += 5
            elif err not in ("authorization_pending",):
                raise AuthError(f"{err}: {data.get('error_description', '')}")
        raise AuthError("设备码已过期，请重新运行 `baidumcp auth`")

    def save_manual(self, access_token: str, refresh_token: str | None = None, expires_in: int | None = None) -> Token:
        token = Token(
            access_token=access_token.strip(),
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in if expires_in else time.time() + 2592000,
        )
        self.save(token)
        return token
