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

`uv run perm -s wy1 "随便写一段 python 并运行一下"` 是一次性 CLI。不传 query 开 TUI。不要设 `PYTHONPATH`。

会话日志在 `~/.permanent/projects/<项目>-<hash>/chats/<id>/session.jsonl`。可用 `PERMANENT_HOME` 改家目录。把 `perm` 装到任意目录都能敲，是下一步安装器的事。

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
