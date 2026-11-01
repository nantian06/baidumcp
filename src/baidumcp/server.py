"""MCP 服务：本地传输工具 + （可选）挂载百度官方 MCP 的全部工具。

统一入口的好处：
- Agent 只需配置一个 stdio 命令，不用关心官方 SSE 地址里的 access_token 过期；
- 不支持 SSE 的 Agent（只支持 stdio / streamable HTTP）也能用官方工具。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from .api import PanClient
from .auth import AuthError, TokenStore
from .config import Settings
from .transfer import Downloader, Uploader

INSTRUCTIONS = """百度网盘工具。
- upload_local_file / download_file_to_local：在【本机】与网盘之间传输文件（支持目录、大文件分片、断点续传）。
- 大文件建议 background=true，然后用 transfer_status 轮询进度。
- 其余工具（file_list、file_keyword_search、file_move 等，若已启用）来自百度官方 MCP，用于浏览、搜索和网盘内管理。
- 网盘路径均以 / 开头。"""


class Jobs:
    """进程内的后台传输任务。进程退出后任务丢失，但上传/下载的续传记录保留在磁盘上。"""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.items: dict[str, dict] = {}

    def start(self, kind: str, coro_factory) -> str:
        job_id = f"{kind}-{next(self._ids)}"
        job = {"id": job_id, "kind": kind, "status": "running", "done": 0, "total": 0, "message": "", "started": time.time()}
        self.items[job_id] = job

        async def progress(done: int, total: int, msg: str) -> None:
            job.update(done=done, total=total, message=msg)

        async def runner() -> None:
            try:
                result = await coro_factory(progress)
                job["result"] = result.to_dict()
                job["status"] = "succeeded" if not result.errors else "partial_failed"
            except Exception as e:  # noqa: BLE001
                job.update(status="failed", error=str(e))
            job["finished"] = time.time()

        job["_task"] = asyncio.get_running_loop().create_task(runner())
        return job_id

    def view(self, job: dict) -> dict:
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        if job["total"]:
            out["percent"] = round(job["done"] * 100 / job["total"], 1)
        return out


def build_server(settings: Settings | None = None, official: bool = True, client: PanClient | None = None) -> FastMCP:
    settings = settings or Settings.load()
    client = client or PanClient(TokenStore(settings))
    tokens = client.tokens
    uploader = Uploader(client, settings)
    downloader = Downloader(client, settings)
    jobs = Jobs()

    mcp = FastMCP(name="baidu-netdisk", instructions=INSTRUCTIONS)

    def reporter(ctx: Context | None):
        if ctx is None:
            return None
        last = [0.0]

        async def report(done: int, total: int, msg: str) -> None:
            now = time.monotonic()
            if now - last[0] < 0.5 and done < total:
                return
            last[0] = now
            try:
                await ctx.report_progress(done, total, msg or None)
            except Exception:  # noqa: BLE001 —— 客户端不支持进度通知时忽略
                pass

        return report

    @mcp.tool
    async def upload_local_file(
        local_path: Annotated[str, Field(description="本机上的文件或目录路径（绝对路径最稳妥）")],
        remote_path: Annotated[str, Field(description="网盘目标路径。以 / 结尾或本地是目录时表示目标目录，例如 /apps/myapp/ 或 /我的资源/")],
        on_conflict: Annotated[Literal["rename", "overwrite", "fail"], Field(description="网盘已有同名文件时：rename 自动改名，overwrite 覆盖，fail 报错")] = "rename",
        background: Annotated[bool, Field(description="true 时立即返回任务 ID，用 transfer_status 查进度，适合大文件")] = False,
        ctx: Context | None = None,
    ) -> dict:
        """把本机文件/目录上传到百度网盘（自动分片、秒传、断点续传）。"""
        if background:
            job_id = jobs.start("upload", lambda p: uploader.upload(local_path, remote_path, on_conflict, p))
            return {"job_id": job_id, "status": "running"}
        return (await uploader.upload(local_path, remote_path, on_conflict, reporter(ctx))).to_dict()

    @mcp.tool
    async def download_file_to_local(
        remote_path: Annotated[str, Field(description="网盘上的文件或目录路径，以 / 开头")],
        local_path: Annotated[str, Field(description="本机保存位置。已存在的目录或以 / 结尾表示保存到该目录下；下载网盘目录时会在其中创建同名目录")],
        overwrite: Annotated[bool, Field(description="本地已有大小不同的同名文件时是否覆盖（同大小文件总是跳过）")] = False,
        background: Annotated[bool, Field(description="true 时立即返回任务 ID，用 transfer_status 查进度，适合大文件")] = False,
        ctx: Context | None = None,
    ) -> dict:
        """把百度网盘上的文件/目录下载到本机（支持断点续传，失败后重试会接着下）。"""
        if background:
            job_id = jobs.start("download", lambda p: downloader.download(remote_path, local_path, overwrite, p))
            return {"job_id": job_id, "status": "running"}
        return (await downloader.download(remote_path, local_path, overwrite, reporter(ctx))).to_dict()

    @mcp.tool
    async def transfer_status(
        job_id: Annotated[str | None, Field(description="任务 ID；留空返回本进程内的全部任务")] = None,
    ) -> dict:
        """查询后台上传/下载任务的进度和结果。"""
        if job_id:
            job = jobs.items.get(job_id)
            if job is None:
                return {"error": f"任务 {job_id} 不存在（服务重启后任务会丢失，重新发起即可断点续传）"}
            return jobs.view(job)
        return {"jobs": [jobs.view(j) for j in jobs.items.values()]}

    @mcp.tool
    async def auth_status() -> dict:
        """检查百度网盘授权状态（令牌是否可用、何时过期、能否自动刷新）。"""
        if settings.static_token:
            return {"authorized": True, "source": "BAIDU_NETDISK_ACCESS_TOKEN", "auto_refresh": False}
        token = tokens.load()
        if token is None:
            return {"authorized": False, "hint": "请在终端运行 `baidumcp auth` 完成授权"}
        try:
            await tokens.get()
            token = tokens.load() or token
        except AuthError as e:
            return {"authorized": False, "error": str(e)}
        days = round((token.expires_at - time.time()) / 86400, 1) if token.expires_at else None
        return {"authorized": True, "source": "token.json", "expires_in_days": days, "auto_refresh": tokens.can_refresh and bool(token.refresh_token)}

    if official:
        mount_official(mcp, settings, tokens)
    return mcp


def mount_official(mcp: FastMCP, settings: Settings, tokens: TokenStore) -> None:
    """把官方 SSE MCP 挂到本服务上；每次建立后端连接时都取最新令牌拼 URL。"""
    from fastmcp.client.transports import SSETransport
    from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient

    async def client_factory():
        token = await tokens.get()
        sep = "&" if "?" in settings.official_sse_url else "?"
        url = f"{settings.official_sse_url}{sep}access_token={token}"
        return ProxyClient(SSETransport(url), forward_incoming_headers=False)

    # 官方连不上时只告警，本地传输工具照常可用
    proxy = FastMCPProxy(client_factory=client_factory, name="baidu-official", provider_error_strategy="warn")
    mcp.mount(proxy)
