"""工具并行：路径锁、bash 不加锁、准备阶段 block、terminate 投影截断、result_id 配对。"""
from __future__ import annotations

import asyncio
import json

from agent_loop.agent_loop import ReactAgentLoop
from agent_loop.compaction import ContextUsageTracker
from agent_loop.llm import LLMResponse
from agent_loop.recover import entries_to_messages, inspect_log
from agent_loop.session_log import SessionLog
from agent_loop.tool_concurrency import run_tool_calls, terminate_cutoff
from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools import bash_tool, read_tool, write_tool
from agent_loop.tools.records import ToolResultEntry


def _call(call_id, name, arguments):
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _write_call(call_id, path, content="x"):
    return _call(call_id, "write", {"path": path, "content": content})


def _read_call(call_id, path):
    return _call(call_id, "read", {"path": path})


def _bash_call(call_id, cmd):
    return _call(call_id, "bash", {"cmd": cmd})


def _patch_execute(module, inflight, max_inflight, delay=0.05):
    original = module.execute

    async def wrapped(args, sandbox=None, workspace=None):
        inflight.append(1)
        max_inflight[0] = max(max_inflight[0], sum(inflight))
        try:
            await asyncio.sleep(delay)
            return await original(args, sandbox, workspace)
        finally:
            inflight.pop()

    module.execute = wrapped
    return original


def test_same_path_writes_are_serialized(tmp_path, monkeypatch):
    inflight = []
    max_inflight = [0]
    original = _patch_execute(write_tool, inflight, max_inflight)
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        path = str(tmp_path / "same.txt")
        results = asyncio.run(
            run_tool_calls(
                runtime,
                [
                    _write_call("a", path, "one"),
                    _write_call("b", path, "two"),
                ],
            )
        )
    finally:
        write_tool.execute = original

    assert max_inflight[0] == 1
    assert [r.tool_call_id for r in results] == ["a", "b"]
    assert not any(r.is_error for r in results)
    assert (tmp_path / "same.txt").read_text(encoding="utf-8") in ("one", "two")


def test_different_path_writes_run_concurrently(tmp_path):
    inflight = []
    max_inflight = [0]
    original = _patch_execute(write_tool, inflight, max_inflight)
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        results = asyncio.run(
            run_tool_calls(
                runtime,
                [
                    _write_call("a", str(tmp_path / "a.txt"), "A"),
                    _write_call("b", str(tmp_path / "b.txt"), "B"),
                ],
            )
        )
    finally:
        write_tool.execute = original

    assert max_inflight[0] == 2
    assert [r.tool_call_id for r in results] == ["a", "b"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"


def test_read_waits_for_same_path_write(tmp_path):
    inflight = []
    max_inflight = [0]
    orig_write = _patch_execute(write_tool, inflight, max_inflight)
    orig_read = _patch_execute(read_tool, inflight, max_inflight)
    (tmp_path / "x.txt").write_text("old", encoding="utf-8")
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        path = str(tmp_path / "x.txt")
        asyncio.run(
            run_tool_calls(
                runtime,
                [
                    _write_call("w", path, "new"),
                    _read_call("r", path),
                ],
            )
        )
    finally:
        write_tool.execute = orig_write
        read_tool.execute = orig_read

    assert max_inflight[0] == 1


def test_bash_does_not_take_path_lock():
    inflight = []
    max_inflight = [0]
    original = _patch_execute(bash_tool, inflight, max_inflight)
    try:
        runtime = ToolRuntime()
        results = asyncio.run(
            run_tool_calls(
                runtime,
                [
                    _bash_call("a", "echo a"),
                    _bash_call("b", "echo b"),
                ],
            )
        )
    finally:
        bash_tool.execute = original

    assert max_inflight[0] == 2
    assert [r.tool_call_id for r in results] == ["a", "b"]
    assert not any(r.is_error for r in results)


def test_prepare_block_skips_execute(tmp_path):
    executed = []
    original = write_tool.execute

    async def wrapped(args, sandbox=None, workspace=None):
        executed.append(args.get("path"))
        return await original(args, sandbox, workspace)

    write_tool.execute = wrapped
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        results = asyncio.run(
            run_tool_calls(
                runtime,
                [
                    _write_call("blocked", "", "nope"),
                    _write_call("ok", str(tmp_path / "ok.txt"), "yes"),
                ],
            )
        )
    finally:
        write_tool.execute = original

    assert results[0].is_error is True
    assert "blocked" in results[0].content.lower() or "path" in results[0].content
    assert results[1].is_error is False
    assert executed == [str(tmp_path / "ok.txt")]
    assert runtime.tool_started_records[0].tool_call_id == "ok"


def test_terminate_runs_all_but_truncates_messages(tmp_path):
    executed = []
    original = write_tool.execute

    async def wrapped(args, sandbox=None, workspace=None):
        executed.append(args["path"])
        return await original(args, sandbox, workspace)

    write_tool.execute = wrapped
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        log = SessionLog(tmp_path / "s.jsonl")
        runtime.attach_log(log)

        def after_b(event):
            if event.get("tool_call_id") == "b":
                return {"terminate": True}
            return None

        runtime.hooks.add_after_tool("write", after_b)
        calls = [
            _write_call("a", str(tmp_path / "a.txt"), "A"),
            _write_call("b", str(tmp_path / "b.txt"), "B"),
            _write_call("c", str(tmp_path / "c.txt"), "C"),
        ]
        loop = ReactAgentLoop(None, None, None)
        loop._log = log
        loop.runtime = runtime
        tracker = ContextUsageTracker(context_window=100_000)
        response = LLMResponse(text="", tool_calls=calls, stop_reason="tool_use")
        messages = []
        asyncio.run(loop._handle_tool_calls(messages, response, "asst-1", tracker))
    finally:
        write_tool.execute = original

    assert len(executed) == 3
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]
    assert [c["id"] for c in messages[0]["tool_calls"]] == ["a", "b"]

    rows = log.read_all()
    disk_results = [r for r in rows if r.get("type") == "tool_result"]
    assert {r.get("tool_call_id") for r in disk_results} == {"a", "b", "c"}
    assistant_row = next(r for r in rows if r.get("type") == "assistant")
    assert [c["id"] for c in assistant_row["tool_calls"]] == ["a", "b", "c"]

    projected = entries_to_messages(rows)
    proj_tools = [m for m in projected if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in proj_tools] == ["a", "b"]
    assert [c["id"] for c in projected[0]["tool_calls"]] == ["a", "b"]

    assert inspect_log(rows).unfinished == []


def test_concurrent_results_pair_result_id_and_reload_order(tmp_path):
    inflight = []
    max_inflight = [0]
    original = _patch_execute(write_tool, inflight, max_inflight)
    try:
        runtime = ToolRuntime()
        runtime.workspace = str(tmp_path)
        log = SessionLog(tmp_path / "s.jsonl")
        runtime.attach_log(log)
        calls = [
            _write_call("late", str(tmp_path / "late.txt"), "L"),
            _write_call("early", str(tmp_path / "early.txt"), "E"),
        ]
        results = asyncio.run(run_tool_calls(runtime, calls))
    finally:
        write_tool.execute = original

    assert max_inflight[0] == 2
    assert [r.tool_call_id for r in results] == ["late", "early"]
    started_by_call = {s.tool_call_id: s.result_id for s in runtime.tool_started_records}
    for result in results:
        assert result.result_id == started_by_call[result.tool_call_id]

    rows = log.read_all()
    for line in (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines():
        json.loads(line)
    assert inspect_log(rows).unfinished == []

    log.append_entry(
        "assistant",
        id="asst-1",
        content="",
        tool_calls=calls,
    )
    # 上面先写了 tool_result 再补 assistant，投影会把孤 orphan result 原样放出；
    # 这里验证「assistant 在前、result 完成序乱」时按 tool_calls 重排。
    ordered_log = SessionLog(tmp_path / "ordered.jsonl")
    ordered_log.append_entry("assistant", id="asst-1", content="", tool_calls=calls)
    by_call = {r.tool_call_id: r for r in results}
    # 故意按完成可能出现的反序落盘
    for call_id in ("early", "late"):
        entry = by_call[call_id]
        ordered_log.append_entry(
            "tool_result",
            result_id=entry.result_id,
            tool_call_id=entry.tool_call_id,
            tool_name=entry.tool_name,
            content=entry.content,
            is_error=entry.is_error,
            terminate=entry.terminate,
        )
    projected = entries_to_messages(ordered_log.read_all())
    assert [m["tool_call_id"] for m in projected if m.get("role") == "tool"] == [
        "late",
        "early",
    ]


def test_terminate_cutoff_helper():
    a = ToolResultEntry("1", "a", "write", "A")
    b = ToolResultEntry("2", "b", "write", "B", terminate=True)
    c = ToolResultEntry("3", "c", "write", "C")
    assert terminate_cutoff([a, b, c]) == 2
    assert terminate_cutoff([a, c]) == 2
