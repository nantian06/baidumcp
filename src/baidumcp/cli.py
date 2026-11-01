"""命令行入口：baidumcp <auth|status|token|serve|upload|download|config>。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shlex
import sys
import time
from pathlib import Path

from . import __version__
from .agents import AGENTS, render
from .auth import AuthError, TokenStore
from .config import Settings, config_path, token_path


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _progress_printer():
    last = [0.0]

    def show(done: int, total: int, msg: str) -> None:
        now = time.monotonic()
        if now - last[0] < 0.3 and done < total:
            return
        last[0] = now
        pct = f"{done * 100 / total:5.1f}%" if total else "  -  "
        sys.stderr.write(f"\r{pct} {done / 1048576:9.1f}/{total / 1048576:.1f} MB  {msg[:40]:<40}")
        if done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    return show


# ---------------- auth ----------------


async def cmd_auth(args) -> int:
    settings = Settings.load()
    store = TokenStore(settings)

    if args.token:
        store.save_manual(args.token, args.refresh_token)
        _err(f"已保存令牌到 {token_path()}" + ("" if args.refresh_token else "（无 refresh_token，30 天后需重新授权）"))
        return 0

    app_key = args.app_key or settings.app_key
    secret_key = args.secret_key or settings.secret_key
    if not app_key:
        app_key = input("百度开放平台应用 AppKey: ").strip()
    if not secret_key:
        secret_key = getpass.getpass("SecretKey: ").strip()
    if not app_key or not secret_key:
        _err("需要 AppKey 和 SecretKey（在 https://pan.baidu.com/union/console 创建应用后获取）")
        return 2
    settings.save_app(app_key, secret_key)
    _err(f"应用信息已保存到 {config_path()}（权限 600）")

    if args.code:
        _err("\n1. 在浏览器打开下面的链接并登录授权：\n")
        print(store.authorize_url())
        code = input("\n2. 把页面上显示的授权码粘贴到这里: ").strip()
        token = await store.exchange_code(code)
    else:
        def notify(info: dict) -> None:
            _err("\n请用浏览器打开下面的地址（或用百度网盘 App 扫描二维码链接），输入用户码完成授权：")
            _err(f"  地址：{info.get('verification_url')}")
            _err(f"  用户码：{info.get('user_code')}")
            if info.get("qrcode_url"):
                _err(f"  二维码：{info['qrcode_url']}")
            _err("等待授权中…（Ctrl+C 取消；如设备码模式不可用，改用 `baidumcp auth --code`）")

        token = await store.device_flow(notify)
    days = (token.expires_at - time.time()) / 86400 if token.expires_at else 30
    _err(f"\n授权成功，令牌已保存到 {token_path()}，{days:.0f} 天内有效，到期前会自动刷新。")
    _err("下一步：运行 `baidumcp config <agent>` 获取接入配置。")
    return 0


async def cmd_status(args) -> int:
    settings = Settings.load()
    store = TokenStore(settings)
    info = {
        "config": str(config_path()),
        "app_key": (settings.app_key[:6] + "…") if settings.app_key else None,
        "chunk_size_mb": settings.chunk_size // 1048576,
        "allowed_dirs": [str(d) for d in settings.allowed_dirs] or "不限制",
    }
    if settings.static_token:
        info["token"] = "BAIDU_NETDISK_ACCESS_TOKEN（静态，不自动刷新）"
    else:
        t = store.load()
        if t is None:
            info["token"] = "未授权，请运行 `baidumcp auth`"
        else:
            info["token"] = str(token_path())
            if t.expires_at:
                info["expires_in_days"] = round((t.expires_at - time.time()) / 86400, 1)
            info["auto_refresh"] = bool(t.refresh_token and store.can_refresh)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


async def cmd_token(args) -> int:
    store = TokenStore(Settings.load())
    if args.refresh:
        print((await store.refresh()).access_token)
    else:
        print(await store.get())
    return 0


# ---------------- transfer ----------------


async def _with_client(fn):
    from .api import PanClient

    settings = Settings.load()
    client = PanClient(TokenStore(settings))
    try:
        return await fn(client, settings)
    finally:
        await client.aclose()


async def cmd_upload(args) -> int:
    from .transfer import Uploader

    async def run(client, settings):
        return await Uploader(client, settings).upload(args.local, args.remote, args.on_conflict, _progress_printer())

    result = await _with_client(run)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not result.errors else 1


async def cmd_download(args) -> int:
    from .transfer import Downloader

    async def run(client, settings):
        return await Downloader(client, settings).download(args.remote, args.local, args.overwrite, _progress_printer())

    result = await _with_client(run)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not result.errors else 1


# ---------------- config ----------------


async def cmd_config(args) -> int:
    if args.list or not args.agent:
        for k, v in AGENTS.items():
            print(f"  {k:<15} {v}")
        return 0
    command = shlex.split(args.command) if args.command else None
    sse_url = None
    if args.mode == "split":
        settings = Settings.load()
        try:
            token = await TokenStore(settings).get()
            sse_url = f"{settings.official_sse_url}?access_token={token}"
            _err("注意：split 模式的 SSE 地址里含有令牌，30 天内有效，过期后需重新生成；请勿提交到代码仓库。")
        except AuthError as e:
            _err(f"（{e}；下面的 SSE 地址使用占位符）")
    _err(f"# {AGENTS[args.agent]}")
    print(render(args.agent, args.mode, command, sse_url))
    return 0


# ---------------- serve ----------------


def cmd_serve(args) -> int:
    from .server import build_server

    settings = Settings.load()
    if args.allow_dir:
        settings.allowed_dirs = [Path(d).expanduser().resolve() for d in args.allow_dir]
    mcp = build_server(settings, official=not args.no_official)
    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            _err(f"警告：监听 {args.host} 会把你的网盘暴露给网络上的其他机器，请确认有额外的访问控制。")
        mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="baidumcp", description="百度网盘 MCP 整合层：本地上传/下载 + 代理官方 MCP")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="授权（默认设备码模式）")
    a.add_argument("--app-key")
    a.add_argument("--secret-key")
    a.add_argument("--code", action="store_true", help="使用授权码模式（浏览器授权后粘贴授权码）")
    a.add_argument("--token", help="直接保存一个已有的 access_token")
    a.add_argument("--refresh-token", help="与 --token 一起使用")

    sub.add_parser("status", help="查看配置与授权状态")

    t = sub.add_parser("token", help="输出当前可用的 access_token（必要时自动刷新）")
    t.add_argument("--refresh", action="store_true", help="强制刷新")

    s = sub.add_parser("serve", help="启动 MCP 服务")
    s.add_argument("--no-official", action="store_true", help="不代理官方 MCP，只提供本地传输工具")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-dir", action="append", help="限制可读写的本地目录，可多次指定")

    u = sub.add_parser("upload", help="上传本地文件/目录")
    u.add_argument("local")
    u.add_argument("remote")
    u.add_argument("--on-conflict", choices=["rename", "overwrite", "fail"], default="rename")

    d = sub.add_parser("download", help="下载网盘文件/目录")
    d.add_argument("remote")
    d.add_argument("local")
    d.add_argument("--overwrite", action="store_true")

    c = sub.add_parser("config", help="生成各 Agent 的 MCP 配置")
    c.add_argument("agent", nargs="?", choices=list(AGENTS))
    c.add_argument("--mode", choices=["unified", "split"], default="unified", help="unified：单一入口（推荐）；split：官方 SSE + 本地传输")
    c.add_argument("--command", help='启动命令，默认自动探测，例如 "uvx baidumcp"')
    c.add_argument("--list", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        sys.exit(cmd_serve(args))
    handler = {
        "auth": cmd_auth,
        "status": cmd_status,
        "token": cmd_token,
        "upload": cmd_upload,
        "download": cmd_download,
        "config": cmd_config,
    }[args.cmd]
    try:
        sys.exit(asyncio.run(handler(args)))
    except AuthError as e:
        _err(f"授权错误：{e}")
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        _err(f"错误：{e}")
        sys.exit(1)
