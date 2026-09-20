# agent-loop

从 SwiftAgent 拆出的独立 ReAct loop（jsonl 对账、工具 hook、崩溃恢复、LLM 重试）。

SwiftAgent 仓库里的代码没有改。这里只换了包名，并加了一个本地 CLI。

## 跑起来

产品名 Permanent。开发入口：`uv run perm`（`agent_loop/cli/app.py`）。细节见 `docs/harness.md`。

```bash
cd /Users/wangyin/agent_loop
uv sync
uv run perm
```

任意机器安装（钉最新 `vX.Y.Z` tag；没有 tag 时加 `--ref main`）：

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/spark-agent/main/install.sh | bash
perm
perm update
```

`uv run perm -s wy1 "随便写一段 python 并运行一下"` 是一次性 CLI。不传 query 开 TUI。不要设 `PYTHONPATH`。

会话日志在 `~/.permanent/projects/<项目>-<hash>/chats/<id>/session.jsonl`。可用 `PERMANENT_HOME` 改家目录。安装器把命令写到 `~/.local/bin/perm`，代码和 venv 在 `~/.permanent/src`。开发树里的 `uv run perm` 不查远程 tag；装好的 `perm` 启动时若有新 tag 会提示 `perm update`。

终端过程按对话分段打印：

```text
## User

随便写一段 python 并运行一下

## Assistant

先看目录，再写文件。

## Tools

- Grep: def main (*.py)
- Read: hello.py (1-40)
- Edit: hello.py
- Bash: python hello.py

## Assistant

已经写好并跑通。
```

## 布局

和原来 `swiftagent/agent_loop` 相同。入口是 `uv run perm`，内部调 `_run_loop`，不经过 SA 的 Post/SSE。
