"""把网页版家目录和 R2 对齐。容器盘是临时的，会话要另外留下。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List

import aiohttp

from agent_loop.paths import list_sessions, spark_home

_SKIP = {".env", "permanent.log", "browser-use-mcp.log", "computer-use-mcp.log"}
_MAX_FILE = 2_000_000
_MAX_ARCHIVE = 20_000_000


def _origin(proxy: str) -> str:
    suffix = "/perm-llm"
    if proxy.endswith(suffix):
        return proxy[: -len(suffix)]
    return proxy.rstrip("/")


def _transcript(log: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        with open(log, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("kind") != "entry" or item.get("type") not in ("user", "assistant"):
                    continue
                text = str(item.get("content") or "").strip()
                if not text:
                    continue
                if len(text) > 8000:
                    text = text[:8000]
                rows.append({"role": item["type"], "text": text})
    except OSError:
        return []
    return rows[-40:]


def _index() -> Dict[str, Any]:
    sessions = []
    transcripts: Dict[str, List[Dict[str, str]]] = {}
    for item in list_sessions(Path.cwd() if not os.environ.get("PERM_WORKSPACE") else os.environ["PERM_WORKSPACE"], spark_home()):
        sid = str(item.get("id") or "")
        title = str(item.get("title") or sid)
        sessions.append({"id": sid, "title": title, "updated": item.get("updated") or 0})
        log = item.get("path")
        if isinstance(log, Path):
            transcripts[sid] = _transcript(log)
    return {"sessions": sessions[:40], "transcripts": transcripts}


def _archive() -> bytes:
    root = spark_home()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if not root.is_dir():
            return b""
        for path in root.rglob("*"):
            if not path.is_file() or path.name in _SKIP:
                continue
            try:
                if path.stat().st_size > _MAX_FILE:
                    continue
            except OSError:
                continue
            tar.add(path, arcname=path.relative_to(root).as_posix())
    data = buf.getvalue()
    if len(data) > _MAX_ARCHIVE:
        logging.warning("[disk] archive %s bytes is over the limit", len(data))
        return b""
    return data


def _extract(data: bytes) -> None:
    root = spark_home()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                continue
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            target.write_bytes(extracted.read(_MAX_FILE))


async def restore_home(proxy: str, token: str) -> None:
    root = spark_home()
    if (root / "projects").is_dir():
        return
    url = _origin(proxy) + "/perm-disk/home"
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status == 404:
                return
            if resp.status >= 400:
                logging.warning("[disk] restore failed HTTP %s", resp.status)
                return
            data = await resp.read()
    if data:
        _extract(data)


def _tool_line(name: str, args: Dict[str, Any]) -> str:
    args = args or {}
    if name in ("web_search", "grep"):
        query = str(args.get("query") or args.get("pattern") or "")
        return f'Search  "{query[:180]}"' if query else "Search"
    if name == "web_fetch":
        return "Fetch  " + str(args.get("url") or "")[:180]
    if name == "bash":
        cmd = str(args.get("cmd") or args.get("command") or "").replace("\n", " ").strip()
        return ("Bash  " + cmd[:180]) if cmd else "Bash"
    if name == "read":
        return "Read  " + str(args.get("path") or "").rsplit("/", 1)[-1]
    if name == "write":
        return "Write  " + str(args.get("path") or "").rsplit("/", 1)[-1]
    return name or "tool"


class LiveLog:
    """把这一轮已经走出的步骤写到服务器，刷新后还能画出来。"""

    def __init__(self, proxy: str, token: str, session_id: str, query: str) -> None:
        now = int(time.time() * 1000)
        self.proxy = proxy
        self.token = token
        self.session_id = session_id
        self.snap: Dict[str, Any] = {
            "query": query,
            "started": now,
            "steps": [{"kind": "thought", "t0": now, "live": True, "text": ""}],
            "answer": "",
        }
        self._http: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._saw_reason = False

    async def open(self) -> None:
        if not self.proxy or not self.token:
            return
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        await self.flush()

    def event(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "assistant_delta" and event.get("channel") == "reasoning":
            self._thought()
            if str(event.get("text") or "").strip():
                self._saw_reason = True
        elif kind == "assistant_delta":
            self._close_thought()
            self._close_tool()
            if (event.get("channel") or "content") == "content":
                self.snap["answer"] += str(event.get("text") or "")
        elif kind == "tool_start":
            self._close_thought()
            self._close_tool()
            self.snap["steps"].append({
                "kind": "tool",
                "t0": int(time.time() * 1000),
                "live": True,
                "text": _tool_line(str(event.get("name") or ""), event.get("args") if isinstance(event.get("args"), dict) else {}),
            })
        elif kind == "tool_result":
            self._close_tool()
        elif kind == "assistant":
            self._close_thought()
            self._close_tool()
            if event.get("text"):
                self.snap["answer"] = str(event.get("text"))
        elif kind == "error":
            self._close_thought()
            self._close_tool()
            self.snap["steps"].append({
                "kind": "tool",
                "t0": int(time.time() * 1000),
                "live": False,
                "text": str(event.get("text") or "出错了")[:300],
            })
        self._schedule(force=kind in ("assistant", "error", "tool_start"))

    async def close(self) -> None:
        self._close_thought()
        self._close_tool()
        if self._task and not self._task.done():
            self._task.cancel()
        await self.flush()
        if self._http:
            await self._http.close()
            self._http = None

    def _thought(self) -> None:
        steps = self.snap["steps"]
        if steps and steps[-1].get("kind") == "thought" and steps[-1].get("live"):
            return
        steps.append({"kind": "thought", "t0": int(time.time() * 1000), "live": True, "text": ""})

    def _close_thought(self) -> None:
        steps = self.snap["steps"]
        if not steps or steps[-1].get("kind") != "thought" or not steps[-1].get("live"):
            return
        if not self._saw_reason and int(time.time() * 1000) - int(steps[-1]["t0"]) < 1000:
            steps.pop()
        else:
            steps[-1]["live"] = False
            steps[-1]["t1"] = int(time.time() * 1000)
        self._saw_reason = False

    def _close_tool(self) -> None:
        steps = self.snap["steps"]
        if steps and steps[-1].get("kind") == "tool" and steps[-1].get("live"):
            steps[-1]["live"] = False
            steps[-1]["t1"] = int(time.time() * 1000)

    def _schedule(self, force: bool = False) -> None:
        if force:
            self._task = asyncio.create_task(self.flush())
            return
        if self._task and not self._task.done():
            return

        async def later() -> None:
            await asyncio.sleep(0.4)
            await self.flush()

        self._task = asyncio.create_task(later())

    async def flush(self) -> None:
        if not self._http:
            return
        body = json.dumps({"sessionId": self.session_id, "snap": self.snap}, ensure_ascii=False).encode()
        try:
            async with self._http.put(
                _origin(self.proxy) + "/perm-disk/live",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                data=body,
            ) as resp:
                if resp.status >= 400:
                    logging.warning("[disk] live failed HTTP %s", resp.status)
        except Exception:
            logging.warning("[disk] live upload failed", exc_info=True)


async def save_home(proxy: str, token: str) -> None:
    origin = _origin(proxy)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=60)
    index = _index()
    archive = _archive()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.put(origin + "/perm-disk/sessions", headers={**headers, "Content-Type": "application/json"}, data=json.dumps(index).encode()) as resp:
            if resp.status >= 400:
                logging.warning("[disk] session index failed HTTP %s", resp.status)
        if archive:
            async with session.put(origin + "/perm-disk/home", headers={**headers, "Content-Type": "application/gzip"}, data=archive) as resp:
                if resp.status >= 400:
                    logging.warning("[disk] home upload failed HTTP %s", resp.status)
