---
name: baidu-netdisk
description: 在本机与百度网盘之间上传/下载文件或目录（支持大文件分片、秒传、断点续传）。当用户要求把本地文件存到百度网盘、从百度网盘取文件到本地、备份目录到网盘时使用。
---

# 百度网盘传输

优先使用 MCP 工具（若已配置 `baidu-netdisk` MCP）：`upload_local_file`、`download_file_to_local`、`transfer_status`，以及官方工具 `file_list`、`file_keyword_search` 等。

没有 MCP 时，使用命令行 `baidumcp`（输出 JSON 到 stdout，进度在 stderr）：

```bash
# 检查授权；未授权时提示用户在终端运行 `baidumcp auth`（需要用户交互，不要替用户运行）
baidumcp status

# 上传：remote 以 / 结尾表示目标目录；本地是目录时会在 remote 下创建同名目录
baidumcp upload ./report.pdf /apps/myapp/
baidumcp upload ./photos /备份/ --on-conflict rename   # rename | overwrite | fail

# 下载：local 是已有目录时保存到其中；网盘目录会在 local 下创建同名目录
baidumcp download /apps/myapp/report.pdf ./
baidumcp download /备份/photos ./restore/ --overwrite
```

要点：
- 网盘路径以 `/` 开头。部分开放平台应用只能写入 `/apps/<应用名>/`。
- 传输中断后重新执行同一条命令即可续传（上传续传记录保留约 20 小时）。
- 退出码：0 成功，1 部分或全部失败（看 JSON 中的 `errors`），3 授权问题。
