"""AgentLoop —— 执行引擎的重写版，用来整体替换 react_agent.py 的 ReActAgent。

替换契约：
1. 构造签名与 ReActAgent 一致：(deps, event_emitter, plugin_manager)。
2. 唯一 public 方法 `async def reply(...) -> Post`，必须来自 post_proxy.end()，
   且 send_to == "User" 才会终止 PostScheduler。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import yaml
from typing import Any, Dict, Optional

from agent_loop.abort import Abort
from agent_loop.inbox import UserInbox
from agent_loop.llm import DeepSeekLLM
from agent_loop.recover import (
    apply_recovery,
    entries_to_messages,
    inspect_log,
    should_append_user,
    unfinished_assistant_id,
)
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools.records import new_result_id
from agent_loop.trace import log_llm_text, log_llm_tools, log_user
from agent_loop.deps import ExecutionDependencies

_DIR = os.path.dirname(__file__)
with open(os.path.join(_DIR, "tools", "tool_schemas.json"), encoding="utf-8") as _f:
    TOOL_SCHEMAS = json.load(_f)
with open(os.path.join(_DIR, "prompt", "system_prompt.yaml"), encoding="utf-8") as _f:
    SYSTEM_PROMPT = yaml.safe_load(_f)["system_prompt"]


class AgentLoop:
    """执行引擎：把一次请求跑成一个回复 Post。对外等价于 ReActAgent。"""

    def __init__(self, deps: ExecutionDependencies, event_emitter, plugin_manager):
        self.deps = deps
        self.event_emitter = event_emitter
        self.plugin_manager = plugin_manager

    async def reply(
        self,
        memory,
        user_action_data: Dict[str, Any],
        request_snapshot=None,
        execution_intent=None,
        route_decision=None,
    ):
        """跑完 loop 后交回 Post：send_to=User，PostScheduler 才会结束本轮。"""
        post_proxy = self.event_emitter.create_post_proxy("ReActAgent")
        post_proxy.update_send_to("User")
        user_query = memory.conversation.rounds[-1].user_query
        try:
            answer = await self._run_loop(user_query, user_action_data)
            post_proxy.post.message = answer or ""
        except Exception as exc:
            logging.exception("[AgentLoop] reply 出错")
            user_action_data["_round_failed_with_exception"] = True
            error_detail = str(exc) or type(exc).__name__
            post_proxy.post.message = f"执行出错：{error_detail}"
        return post_proxy.end()


class ReactAgentLoop(AgentLoop):
    """最小 ReAct 循环：LLM 每步只允许 bash 工具调用或 end_turn。"""

    def __init__(self, deps: ExecutionDependencies, event_emitter, plugin_manager):
        super().__init__(deps, event_emitter, plugin_manager)
        self._llm: Optional[DeepSeekLLM] = None
        self.runtime = ToolRuntime()
        self._log: Optional[SessionLog] = None
        self.inbox = UserInbox()

    @property
    def hooks(self):
        return self.runtime.hooks

    @property
    def tool_started_records(self):
        return self.runtime.tool_started_records

    @property
    def tool_result_records(self):
        return self.runtime.tool_result_records

    def _get_llm(self) -> DeepSeekLLM:
        if self._llm is None:
            self._llm = DeepSeekLLM()
        return self._llm

    async def _run_loop(self, user_query: str, user_action_data: Dict[str, Any]) -> str:
        self._log = SessionLog(session_log_path(user_action_data))
        self.runtime.attach_log(self._log)
        plan = inspect_log(self._log.read_all())
        if plan.action not in ("idle", "empty"):
            logging.info("[recover] action=%s unfinished=%s", plan.action, len(plan.unfinished))
        await apply_recovery(self.runtime, plan)
        plan = inspect_log(self._log.read_all())

        if plan.action == "continue_llm":
            self.inbox.push_steer(user_query)
        elif should_append_user(plan, user_query):
            self._log.append_entry("user", content=user_query)
            log_user(user_query)
        else:
            log_user(user_query)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(entries_to_messages(self._log.read_all()))
        llm = self._get_llm()
        abort = Abort()
        stop_listening = _listen_sigint(abort)
        try:
            return await self._run_outer_inner(messages, llm, abort)
        finally:
            stop_listening()

    async def _run_outer_inner(self, messages: list, llm, abort: Abort) -> str:
        """外层：内层停稳后才消费 follow_up。内层：每步（含 tool result）之后插入 steer。"""
        final_text = ""
        while True:
            has_more_tools = True
            while has_more_tools or self.inbox.has_steer():
                self._inject_steer(messages)
                assistant_id = self._begin_llm_step()
                response = await llm.call(messages, tools=TOOL_SCHEMAS, abort=abort)
                if response.stop_reason == "end_turn" or not response.tool_calls:
                    log_llm_text(response.text or "")
                    self._log.append_entry(
                        "assistant",
                        id=assistant_id,
                        content=response.text or "",
                    )
                    final_text = response.text or ""
                    has_more_tools = False
                else:
                    log_llm_tools(response.tool_calls)
                    await self._handle_tool_calls(messages, response, assistant_id)
                    has_more_tools = True
            follow = self.inbox.drain_follow_up()
            if not follow:
                return final_text
            for text in follow:
                self.inbox.push_steer(text)

    def _inject_steer(self, messages: list) -> None:
        for text in self.inbox.drain_steer():
            self._log.append_entry("user", content=text)
            messages.append({"role": "user", "content": text})
            log_user(text)

    def _begin_llm_step(self) -> str:
        """llm.call 之前盖章。若上次 attempt 没关，复用同一个 assistant id。"""
        open_id = unfinished_assistant_id(self._log.read_all())
        if open_id:
            logging.info("[ReactAgentLoop] 复用未关闭的 step_attempt id=%s", open_id)
            return open_id
        assistant_id = new_result_id()
        self._log.append_record(
            "step_attempt",
            step="assistant",
            attempt=1,
            result_entry_id=assistant_id,
        )
        return assistant_id

    async def _handle_tool_calls(
        self, messages: list, response, assistant_id: str
    ) -> None:
        """把 assistant 的 tool_calls 跑完，结果追加进 messages。"""
        messages.append(
            {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": response.tool_calls,
            }
        )
        self._log.append_entry(
            "assistant",
            id=assistant_id,
            content=response.text or "",
            tool_calls=response.tool_calls,
        )
        for tool_call in response.tool_calls:
            result = await self.runtime.call_tool(tool_call)
            messages.append(result.to_message())
            if result.terminate:
                break


def _listen_sigint(abort: Abort):
    """Ctrl+C 只 abort 重试等待，不立刻杀掉进程。"""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, abort.abort)
        return lambda: loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        previous = signal.getsignal(signal.SIGINT)

        def _on_sigint(*_args):
            abort.abort()

        signal.signal(signal.SIGINT, _on_sigint)
        return lambda: signal.signal(signal.SIGINT, previous)
