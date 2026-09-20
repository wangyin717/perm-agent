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

from typing import Any, Dict, Optional

from agent_loop.abort import Abort
from agent_loop.compaction import ContextUsageTracker, maybe_compact
from agent_loop.envfile import load_dotenv
from agent_loop.inbox import UserInbox
from agent_loop.llm import DeepSeekLLM
from agent_loop.recover import (
    apply_recovery,
    entries_to_messages,
    inspect_log,
    should_append_user,
    tool_result_api_messages,
    unfinished_assistant_id,
)
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.runtime.tool_concurrency import run_tool_calls, terminate_cutoff
from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.runtime.records import new_result_id
from agent_loop.cli.trace import log_context, log_llm_text, log_llm_tools, log_user
from agent_loop.session_title import ensure_auto_title
from agent_loop.events import emit as emit_event
from agent_loop.deps import ExecutionDependencies
from agent_loop.prompt import build_system_prompt
from agent_loop.memory import maybe_dream, maybe_flush

_DIR = os.path.dirname(__file__)
with open(os.path.join(_DIR, "tools", "tool_schemas.json"), encoding="utf-8") as _f:
    TOOL_SCHEMAS = json.load(_f)


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
            # 这里兜底的是"当前进程内"的异常（比如网络问题、代码 bug）；不是崩溃恢复——
            # 那种情况走的是下次 _run_loop 开头的 inspect_log/apply_recovery。
            logging.exception("[AgentLoop] reply 出错")
            user_action_data["_round_failed_with_exception"] = True
            error_detail = str(exc) or type(exc).__name__
            post_proxy.post.message = f"执行出错：{error_detail}"
        # 必须调用 end()：契约里唯一能让 PostScheduler 结束本轮的路径。
        return post_proxy.end()


class ReactAgentLoop(AgentLoop):
    """最小 ReAct 循环：LLM 每步只允许 bash 工具调用或 end_turn。"""

    def __init__(self, deps: ExecutionDependencies, event_emitter, plugin_manager):
        super().__init__(deps, event_emitter, plugin_manager)
        self._llm: Optional[DeepSeekLLM] = None
        self.runtime = ToolRuntime()
        self._log: Optional[SessionLog] = None
        self.inbox = UserInbox()
        self._abort: Optional[Abort] = None

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
            load_dotenv()
            self._llm = DeepSeekLLM()
        return self._llm

    def _seed_title(self, user_action_data: Dict[str, Any], hint: str = "") -> None:
        workspace = str(user_action_data.get("workspace") or os.getcwd())
        session_id = str(user_action_data.get("sessionId") or "default")
        ensure_auto_title(
            workspace,
            session_id,
            hint,
            user_action_data.get("sparkHome"),
        )

    async def _run_loop(self, user_query: str, user_action_data: Dict[str, Any]) -> str:
        self._log = SessionLog(session_log_path(user_action_data))
        self.runtime.attach_log(self._log)
        self.runtime.workspace = str(user_action_data.get("workspace") or os.getcwd())
        # 每次进程启动都可能是接着上次崩溃的现场：先看盘上停在哪一步。
        plan = inspect_log(self._log.read_all())
        if plan.action not in ("idle", "empty"):
            logging.info("[recover] action=%s unfinished=%s", plan.action, len(plan.unfinished))
        # 关掉未完成的工具工单（重放或标 interrupted）、补跑漏掉的 tool_calls。
        await apply_recovery(self.runtime, plan)
        # apply_recovery 可能新增了 tool_result entry，必须重新 inspect 才能拿到最新 action。
        plan = inspect_log(self._log.read_all())

        if plan.action == "continue_llm":
            # 上一轮已经有 tool_result 在等 LLM 收尾，这句新问题不能夹在中间打断，
            # 先当 steer 排队，等这一轮说完再喂给模型。
            self.inbox.push_steer(user_query, user_action_data.get("media"))
        elif should_append_user(plan, user_query):
            self._append_user(user_query, user_action_data.get("media"))
            log_user(user_query)
        else:
            # start_llm 且盘上最后一条 user 就是这句问题：说明是崩溃后原样重跑，不重复写。
            log_user(user_query)

        # messages 是每次运行时从 jsonl 现算的投影，不是持久状态；jsonl 才是唯一权威。
        # entries_to_messages 会应用之前落盘的压缩记录（redaction / summary），
        # 所以这里拿到的已经是"压缩后应该发给模型"的样子，不是原始全量历史。
        workspace = user_action_data.get("workspace") or os.getcwd()
        system = build_system_prompt(workspace)
        llm = self._get_llm()
        messages = [{"role": "system", "content": system}]
        messages.extend(
            entries_to_messages(
                self._log.read_all(),
                supports_images=getattr(llm, "supports_images", False),
            )
        )
        tracker = ContextUsageTracker(context_window=getattr(llm, "context_window", 0) or 0)
        # bootstrap 是本地估算，不发请求；第一次真实 llm.call 拿到 usage 后会被校准掉。
        tracker.bootstrap(messages)
        logging.info(
            "[AgentLoop] session=%s loaded %d messages, ratio=%.3f (window=%d)",
            self._log.path.stem,
            len(messages),
            tracker.usage_ratio(),
            tracker.context_window,
        )
        abort = Abort()
        self._abort = abort
        stop_listening = (
            _listen_sigint(abort) if user_action_data.get("install_sigint", True) else (lambda: None)
        )
        session_id = str(user_action_data.get("sessionId") or "default")
        try:
            result = await self._run_outer_inner(
                messages, llm, abort, tracker, system_content=system
            )
            self._seed_title(user_action_data, user_query)
            if not abort.aborted:
                await maybe_flush(
                    self._log,
                    llm,
                    workspace,
                    session_id,
                    abort,
                    user_query=user_query,
                )
                await maybe_dream(llm, workspace, abort, user_query=user_query, session_id=session_id)
            return result
        finally:
            stop_listening()
            if self._abort is abort:
                self._abort = None

    async def _run_outer_inner(
        self,
        messages: list,
        llm,
        abort: Abort,
        tracker: ContextUsageTracker,
        *,
        system_content: str,
    ) -> str:
        """外层：内层停稳后才消费 follow_up。内层：每步（含 tool result）之后插入 steer。

        压缩：回合中途只在下一枪会超窗时压；内层停稳（或 end_turn 后改吃 steer）按 80% 压。
        """
        final_text = ""
        empty_retries = 0
        while True:
            has_more_tools = True
            # has_more_tools 撑着内层至少跑一次；has_steer 让新插队的话有机会再挨一轮 LLM，
            # 即便上一步已经是 end_turn（has_more_tools=False）。
            while has_more_tools or self.inbox.has_steer():
                # 上一枪已经 end_turn、接下来要吃 steer：那是新回合，先按 80% 压。
                if not has_more_tools and self.inbox.has_steer():
                    await maybe_compact(
                        self._log,
                        messages,
                        tracker,
                        llm,
                        abort=abort,
                        system_content=system_content,
                    )
                self._inject_steer(messages, tracker)
                assistant_id = self._begin_llm_step()
                # 回合中途只在下一枪会超窗时才压，好让当前回合继续吃 KV cache。
                await maybe_compact(
                    self._log,
                    messages,
                    tracker,
                    llm,
                    abort=abort,
                    overflow_only=True,
                    system_content=system_content,
                )
                estimated_tokens = tracker.known_tokens
                response = await llm.call(
                    messages,
                    tools=TOOL_SCHEMAS,
                    abort=abort,
                    on_delta=lambda text, channel="content": emit_event(
                        "assistant_delta", text=text, channel=channel
                    ),
                )
                # 真实 usage 到手，覆盖掉本地估算——两次真实调用之间的误差不会累积。
                tracker.update_from_response(response)
                if response.prompt_tokens:
                    logging.debug(
                        "[AgentLoop] token estimate drift: local_estimate=%d real_prompt_tokens=%d",
                        estimated_tokens,
                        response.prompt_tokens,
                    )
                used = response.prompt_tokens + response.completion_tokens
                if used:
                    log_context(used, getattr(llm, "context_window", 0) or 0)
                if response.tool_calls:
                    empty_retries = 0
                    log_llm_tools(response.tool_calls)
                    await self._handle_tool_calls(
                        messages,
                        response,
                        assistant_id,
                        tracker,
                        supports_images=getattr(llm, "supports_images", False),
                    )
                    has_more_tools = True
                    continue
                text = response.text or ""
                if text.strip():
                    empty_retries = 0
                    log_llm_text(text)
                    self._log.append_entry("assistant", id=assistant_id, content=text)
                    final_text = text
                    has_more_tools = False
                    continue
                # 思维链流完了但既没有正文也没有工具：当成空完成，不是成功 end_turn。
                empty_retries += 1
                if abort.aborted or empty_retries > 1:
                    note = "model returned no text"
                    if getattr(response, "finish_reason", ""):
                        note += f" ({response.finish_reason})"
                    emit_event("error", text=note)
                    self._log.append_entry("assistant", id=assistant_id, content="")
                    final_text = ""
                    has_more_tools = False
                else:
                    logging.warning("[AgentLoop] empty completion, retrying")
                    has_more_tools = True
            # 内层停稳 = 一个回合结束。按 80% 压，再决定要不要吃 follow_up。
            await maybe_compact(
                self._log,
                messages,
                tracker,
                llm,
                abort=abort,
                system_content=system_content,
            )
            follow = self.inbox.drain_follow_up()
            if not follow:
                return final_text
            for text in follow:
                self.inbox.push_steer(text)

    def _append_user(self, text: str, media: Any = None) -> None:
        fields: Dict[str, Any] = {"content": text}
        if media:
            fields["media"] = media
        self._log.append_entry("user", **fields)
        if isinstance(media, list):
            from pathlib import Path as _Path

            for item in media:
                if isinstance(item, dict) and item.get("kind") == "image":
                    path = str(item.get("path") or "")
                    logging.info("[user] attached image: %s", _Path(path).name if path else "")

    def _inject_steer(self, messages: list, tracker: ContextUsageTracker) -> None:
        """把用户在这一轮跑着的时候插的话，当成新的 user 消息塞进当前 messages。"""
        from agent_loop.recover import user_api_message

        llm = self._get_llm()
        for text, media in self.inbox.drain_steer():
            self._append_user(text, media or None)
            row = {"content": text, "media": media or []}
            msg = user_api_message(
                row, supports_images=getattr(llm, "supports_images", False)
            )
            messages.append(msg)
            tracker.add_estimate({"role": "user", "content": text})
            log_user(text)

    def _begin_llm_step(self) -> str:
        """llm.call 之前盖章。若上次 attempt 没关，复用同一个 assistant id。

        "盖章"指先写一条 step_attempt record（不进模型上下文），预分配好这次 LLM
        回复要用的 assistant id。这样如果进程在 llm.call 里崩溃，下次重启用
        unfinished_assistant_id 能认出"这个 id 已经许诺过、还没写 assistant entry"，
        接着用同一个 id 重试，而不是产生一个孤儿 id。
        """
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
        self,
        messages: list,
        response,
        assistant_id: str,
        tracker: ContextUsageTracker,
        supports_images: bool = False,
    ) -> None:
        """把 assistant 的 tool_calls 跑完，结果追加进 messages。

        一批全部执行（路径锁并发）。terminate 只截断喂给模型的 messages，
        jsonl 照实保留所有已执行的 tool_result。assistant.tool_calls 在内存里
        裁到与保留的 result 对齐，避免下一枪缺 tool message。
        """
        tool_calls = list(response.tool_calls or [])
        assistant_msg = {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": list(tool_calls),
        }
        # 先落盘 assistant 消息本身，再跑工具——即便工具执行中途进程崩溃，
        # jsonl 里已经有这条 assistant+tool_calls，recover.py 能认出该走 resume_tools。
        messages.append(assistant_msg)
        # assistant 已经在上一枪的 completion_tokens 里，不要再 add_estimate。
        self._log.append_entry(
            "assistant",
            id=assistant_id,
            content=response.text or "",
            tool_calls=tool_calls,
        )
        results = await run_tool_calls(self.runtime, tool_calls)
        cut = terminate_cutoff(results)
        assistant_msg["tool_calls"] = tool_calls[:cut]
        if not assistant_msg["tool_calls"]:
            assistant_msg.pop("tool_calls", None)
        for result in results[:cut]:
            row = {
                "tool_call_id": result.tool_call_id,
                "tool_name": result.tool_name,
                "content": result.content,
                "media": result.media,
                "result_id": result.result_id,
            }
            api_msgs = tool_result_api_messages(
                row,
                result.content,
                supports_images=supports_images,
                attach_media=True,
            )
            for msg in api_msgs:
                messages.append(msg)
                if msg.get("role") == "tool":
                    tracker.add_estimate(msg)


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
