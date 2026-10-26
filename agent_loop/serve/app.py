"""POST /chat 把一轮 perm 的过程按事件推回去。

本机只听 127.0.0.1。容器里 PERM_WEB=1，听 0.0.0.0，不读 .env。
循环仍是 ReactAgentLoop._run_loop，和 cli/app.py 相同。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict

from aiohttp import web

from agent_loop import events
from agent_loop.envfile import load_dotenv
from agent_loop.file_logging import configure_file_logging
from agent_loop.loop import ReactAgentLoop
from agent_loop.paths import session_dir, session_log_path_for, spark_home
from agent_loop.prompt_images import media_from_b64_items
from agent_loop.prompt.skills import ensure_user_skills
from agent_loop.plugins import PluginHost
from agent_loop.plugins.config import load_config
from agent_loop.plugins.pack import ensure_user_plugins
from agent_loop.relay import clear_relay, set_relay
from agent_loop.serve.disk import LiveLog, restore_home, save_home

WEB = os.environ.get("PERM_WEB") == "1"
HOST = os.environ.get("PERM_HOST") or ("0.0.0.0" if WEB else "127.0.0.1")
PORT = int(os.environ.get("PERM_PORT") or ("8080" if WEB else "8788"))
WORKSPACE = os.environ.get("PERM_WORKSPACE") or ("/workspace" if WEB else "")
_KINDS = {"assistant_delta", "tool_start", "tool_result", "assistant", "error"}
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_busy = asyncio.Lock()
_active: asyncio.Task | None = None


class _Deps:
    async def ensure_sandbox_async(self):
        return None


def _sse(event: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _web_plugins() -> None:
    """网页版关掉浏览器和电脑操作。只写这个家目录的 config.json。"""
    data = load_config()
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    plugins["browser-use"] = False
    plugins["computer-use"] = False
    data["plugins"] = plugins
    path = spark_home() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run(
    query: str,
    session_id: str,
    queue: asyncio.Queue,
    proxy: str,
    token: str,
    reference: str = "",
    images: object = None,
) -> None:
    if proxy and token:
        set_relay(token, proxy)
        try:
            await restore_home(proxy, token)
        except Exception:
            logging.warning("[disk] restore failed", exc_info=True)
    else:
        clear_relay()
    if WEB:
        try:
            ensure_user_skills()
        except Exception:
            logging.warning("[skills] copy failed", exc_info=True)
        try:
            ensure_user_plugins()
        except Exception:
            logging.warning("[plugins] copy failed", exc_info=True)
    host = PluginHost()
    loop = ReactAgentLoop(_Deps(), None, host)
    user_action_data = {
        "sessionId": session_id,
        "workspace": WORKSPACE or os.getcwd(),
        "install_sigint": False,
    }
    media = media_from_b64_items(images, session_dir(WORKSPACE or os.getcwd(), session_id))
    if media:
        user_action_data["media"] = media
    log_path = session_log_path_for(WORKSPACE or os.getcwd(), session_id)
    configure_file_logging(log_path.parent)
    host.set_session_dir(log_path.parent)

    live = LiveLog(proxy, token, session_id, query) if proxy and token else None
    if live:
        try:
            await live.open()
        except Exception:
            logging.warning("[disk] live open failed", exc_info=True)

    def on_event(event: Dict[str, Any]) -> None:
        if event.get("kind") in _KINDS:
            queue.put_nowait(dict(event))
            if live:
                live.event(event)

    unsubscribe = events.subscribe(on_event)
    try:
        await loop._run_loop(query, user_action_data, reference=reference)
        if proxy and token:
            try:
                await save_home(proxy, token)
            except Exception:
                logging.warning("[disk] save failed", exc_info=True)
    except Exception as exc:
        queue.put_nowait({"kind": "error", "text": str(exc) or type(exc).__name__})
        raise
    finally:
        if live:
            try:
                await live.close()
            except Exception:
                logging.warning("[disk] live close failed", exc_info=True)
        clear_relay()
        unsubscribe()
        await host.stop()


async def chat(request: web.Request) -> web.StreamResponse:
    global _active
    if _busy.locked():
        return web.json_response({"error": "上一轮还在跑"}, status=409)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "需要 JSON"}, status=400)
    query = str(body.get("query") or "").strip()
    reference = str(body.get("reference") or "").strip()
    session_id = str(body.get("sessionId") or "serve").strip()
    images = body.get("images")
    if not query and isinstance(images, list) and images:
        query = "看看这张图"
    if not query:
        return web.json_response({"error": "缺少 query"}, status=400)
    if not _SESSION.fullmatch(session_id):
        return web.json_response({"error": "sessionId 只能用字母、数字、点、下划线和短横线"}, status=400)

    proxy = str(request.headers.get("X-Perm-Proxy") or "").strip()
    token = str(request.headers.get("X-Perm-Token") or "").strip()
    await _busy.acquire()
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_run(query, session_id, queue, proxy, token, reference, images))
    _active = task
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "http://127.0.0.1:8787",
        },
    )
    await response.prepare(request)
    try:
        while True:
            if task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await response.write(_sse(event))
            except Exception:
                logging.info("[chat] browser left, run keeps going")
                break
        if task.done() and not task.cancelled() and task.exception() is not None:
            task.exception()
    finally:
        try:
            await response.write_eof()
        except Exception:
            pass
        if task.done():
            if _active is task:
                _active = None
            if _busy.locked():
                _busy.release()
        else:
            asyncio.create_task(_finish_after_disconnect(task))
    return response


async def _finish_after_disconnect(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.warning("[chat] run ended after the browser left", exc_info=True)
    finally:
        global _active
        if _active is task:
            _active = None
        if _busy.locked():
            _busy.release()


async def stop_chat(_request: web.Request) -> web.Response:
    task = _active
    if task and not task.done():
        task.cancel()
    return web.json_response({"ok": True})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def preflight(_request: web.Request) -> web.Response:
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": "http://127.0.0.1:8787",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


@web.middleware
async def cors(request: web.Request, handler):
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1:8787"
    return response


def main() -> None:
    global WORKSPACE
    if WEB:
        WORKSPACE = os.environ.get("PERM_WORKSPACE") or "/workspace"
        os.makedirs(WORKSPACE, exist_ok=True)
        os.chdir(WORKSPACE)
        _web_plugins()
    else:
        WORKSPACE = os.getcwd()
        load_dotenv()
    events.print_to_stdout = False
    app = web.Application(middlewares=[cors])
    app.router.add_get("/health", health)
    app.router.add_route("OPTIONS", "/chat", preflight)
    app.router.add_post("/chat", chat)
    app.router.add_post("/chat/stop", stop_chat)
    web.run_app(app, host=HOST, port=PORT, print=lambda *_: None)


if __name__ == "__main__":
    main()
