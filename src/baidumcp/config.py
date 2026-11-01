"""配置与路径。

优先级：命令行参数 > 环境变量 > 配置文件（~/.config/baidumcp/config.json）> 默认值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

OFFICIAL_SSE_URL = "https://mcp-pan.baidu.com/sse"
OAUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
PAN_API_BASE = "https://pan.baidu.com"
PCS_UPLOAD_BASE = "https://d.pcs.baidu.com"
USER_AGENT = "pan.baidu.com"

MB = 1024 * 1024


def home_dir() -> Path:
    env = os.environ.get("BAIDUMCP_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "baidumcp"


def config_path() -> Path:
    return home_dir() / "config.json"


def token_path() -> Path:
    return home_dir() / "token.json"


def state_dir() -> Path:
    return home_dir() / "state"


def write_private_json(path: Path, data: dict) -> None:
    """原子写入并把权限设为 600（含密钥/令牌）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _split_dirs(value: str | list | None) -> list[Path]:
    if not value:
        return []
    items = value if isinstance(value, list) else value.split(os.pathsep)
    return [Path(p).expanduser().resolve() for p in items if str(p).strip()]


@dataclass
class Settings:
    app_key: str | None = None
    secret_key: str | None = None
    # 静态令牌（不自动刷新）；与官方 MCP 使用同名环境变量
    static_token: str | None = None
    # 分片大小：普通用户 4MB，VIP 16MB，SVIP 32MB
    chunk_size: int = 4 * MB
    # 本地路径白名单；为空表示不限制
    allowed_dirs: list[Path] = field(default_factory=list)
    official_sse_url: str = OFFICIAL_SSE_URL

    @classmethod
    def load(cls) -> "Settings":
        cfg = read_json(config_path())
        env = os.environ
        chunk_mb = env.get("BAIDUMCP_CHUNK_MB") or cfg.get("chunk_mb") or 4
        return cls(
            app_key=env.get("BAIDU_APP_KEY") or cfg.get("app_key"),
            secret_key=env.get("BAIDU_SECRET_KEY") or cfg.get("secret_key"),
            static_token=env.get("BAIDU_NETDISK_ACCESS_TOKEN") or None,
            chunk_size=int(chunk_mb) * MB,
            allowed_dirs=_split_dirs(env.get("BAIDUMCP_ALLOWED_DIRS") or cfg.get("allowed_dirs")),
            official_sse_url=env.get("BAIDUMCP_OFFICIAL_SSE_URL")
            or cfg.get("official_sse_url")
            or OFFICIAL_SSE_URL,
        )

    def save_app(self, app_key: str, secret_key: str) -> None:
        cfg = read_json(config_path())
        cfg.update({"app_key": app_key, "secret_key": secret_key})
        write_private_json(config_path(), cfg)
        self.app_key, self.secret_key = app_key, secret_key

    def check_local_path(self, path: str | Path) -> Path:
        """解析本地路径并按白名单校验，防止 Agent 读写任意文件。"""
        p = Path(path).expanduser().resolve()
        if self.allowed_dirs and not any(p == d or d in p.parents for d in self.allowed_dirs):
            allowed = ", ".join(str(d) for d in self.allowed_dirs)
            raise PermissionError(f"本地路径 {p} 不在允许的目录内（BAIDUMCP_ALLOWED_DIRS={allowed}）")
        return p
