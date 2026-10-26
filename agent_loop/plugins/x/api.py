"""只读的 X 搜索。本机进程读 ~/.permanent/.env，网页不走这里的密钥。"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import quote

import aiohttp

_API = "https://api.x.com"
_POST_FIELDS = "created_at,public_metrics,author_id"
_USER_FIELDS = "name,username,description,public_metrics,verified"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_posts",
        "description": "Search recent X posts from the last 7 days. Pass operators inside query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
                "next_token": {"type": "string"},
                "archive": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_users",
        "description": "Search X accounts by name or keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_user",
        "description": "Look up one X account by username.",
        "inputSchema": {
            "type": "object",
            "properties": {"username": {"type": "string"}},
            "required": ["username"],
        },
    },
]


def clamp_count(value: Any, default: int = 10) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(1, min(20, count))


def _users_by_id(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    users = ((payload.get("includes") or {}).get("users") or [])
    return {str(user.get("id") or ""): user for user in users if isinstance(user, dict)}


def format_posts(payload: Dict[str, Any], limit: int) -> str:
    users = _users_by_id(payload)
    lines: List[str] = []
    for post in (payload.get("data") or [])[:limit]:
        if not isinstance(post, dict):
            continue
        user = users.get(str(post.get("author_id") or ""), {})
        username = str(user.get("username") or "")
        post_id = str(post.get("id") or "")
        url = f"https://x.com/{username}/status/{post_id}" if username and post_id else ""
        metrics = post.get("public_metrics") or {}
        likes = metrics.get("like_count", 0)
        reposts = metrics.get("retweet_count", 0)
        lines.append(
            "\n".join(
                part
                for part in (
                    f"@{username} {post.get('created_at') or ''}".strip(),
                    str(post.get("text") or "").strip(),
                    url,
                    f"likes {likes} reposts {reposts}",
                )
                if part
            )
        )
    if not lines:
        return "没有找到帖子"
    nxt = str(((payload.get("meta") or {}).get("next_token") or "")).strip()
    body = "\n\n".join(lines)
    if nxt:
        body += f"\n\nnext_token: {nxt}"
    return body


def format_users(payload: Dict[str, Any], limit: int) -> str:
    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    lines: List[str] = []
    for user in rows[:limit]:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "")
        metrics = user.get("public_metrics") or {}
        lines.append(
            "\n".join(
                part
                for part in (
                    f"@{username} {user.get('name') or ''}".strip(),
                    str(user.get("description") or "").strip(),
                    f"https://x.com/{username}" if username else "",
                    f"followers {metrics.get('followers_count', 0)}",
                )
                if part
            )
        )
    return "\n\n".join(lines) if lines else "没有找到账号"


async def x_get(bearer: str, path: str, params: Dict[str, str]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Authorization": f"Bearer {bearer}", "User-Agent": "perm-x"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_API + path, headers=headers, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"X {response.status} {text[:240]}")
            data = json.loads(text) if text else {}
            return data if isinstance(data, dict) else {}


async def call_tool(name: str, args: Dict[str, Any], bearer: str) -> str:
    if not bearer.strip():
        return "没有配置 X_BEARER_TOKEN"
    if name == "search_posts":
        if args.get("archive"):
            return "全历史搜索还没打开，先搜最近 7 天"
        query = str(args.get("query") or "").strip()
        if not query:
            return "缺少 query"
        count = clamp_count(args.get("max_results"))
        params = {
            "query": query,
            "max_results": str(max(10, count)),
            "tweet.fields": _POST_FIELDS,
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        token = str(args.get("next_token") or "").strip()
        if token:
            params["next_token"] = token
        payload = await x_get(bearer, "/2/tweets/search/recent", params)
        return format_posts(payload, count)
    if name == "search_users":
        query = str(args.get("query") or "").strip()
        if not query:
            return "缺少 query"
        count = clamp_count(args.get("max_results"))
        payload = await x_get(
            bearer,
            "/2/users/search",
            {"query": query, "max_results": str(count), "user.fields": _USER_FIELDS},
        )
        return format_users(payload, count)
    if name == "lookup_user":
        username = str(args.get("username") or "").strip().lstrip("@")
        if not username or "/" in username:
            return "缺少 username"
        payload = await x_get(
            bearer,
            f"/2/users/by/username/{quote(username)}",
            {"user.fields": _USER_FIELDS},
        )
        return format_users(payload, 1)
    return f"unknown tool {name}"
