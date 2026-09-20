# Permanent

本地 coding agent。命令是 `perm`，仓库是 [perm-agent](https://github.com/wangyin717/perm-agent)。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
perm
```

装到最新的 `vX.Y.Z` tag。指定版本：`bash -s -- --ref v0.1.0`。更新：`perm update`。

数据在 `~/.permanent`（可用 `PERMANENT_HOME` 改）。代码和 venv 在 `~/.permanent/src`，命令在 `~/.local/bin/perm`。托管安装启动时若有新 tag 会提示 `perm update`。

## 开发

```bash
uv sync
uv run perm
```

`uv run perm -s wy1 "随便写一段 python 并运行一下"` 是一次性 CLI。省略问题则开 TUI。不要设 `PYTHONPATH`。密钥放仓库根 `.env` 或 `~/.permanent/.env`（`DEEPSEEK_API_KEY`）。

循环、工具、会话布局见 `docs/harness.md`。
