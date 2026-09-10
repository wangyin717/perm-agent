# agent-loop

从 SwiftAgent 拆出的独立 ReAct loop（jsonl 对账、工具 hook、崩溃恢复、LLM 重试）。

SwiftAgent 仓库里的代码没有改。这里只换了包名，并加了一个本地 CLI。

## 跑起来

入口：`python -m agent_loop`（`agent_loop/cli/app.py`）。细节见 `docs/harness.md`。

```bash
cd /Users/wangyin/agent_loop
source .venv/bin/activate
PYTHONPATH=. python -m agent_loop -s wy1 "随便写一段 python 并运行一下"
```

`-s / --session` 指定会话，机器日志在 `.agent/sessions/<id>/session.jsonl`。不传则每次新建 `cli-时间戳`。

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

和原来 `swiftagent/agent_loop` 相同。入口是 `python -m agent_loop`，内部调 `_run_loop`，不经过 SA 的 Post/SSE。
