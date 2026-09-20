"""本地 CLI：uv run perm --session wy1 "随便写一段 python 并运行一下" """

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from agent_loop.loop import ReactAgentLoop
from agent_loop.envfile import load_dotenv
from agent_loop.paths import session_log_path_for
from agent_loop.cli.trace import log_session
from agent_loop.update import peek_update, run_update


class CliDeps:
    async def ensure_sandbox_async(self):
        return None


async def _amain(query: str, session_id: str) -> str:
    loop = ReactAgentLoop(CliDeps(), None, None)
    user_action_data = {
        "sessionId": session_id,
        "workspace": os.getcwd(),
    }
    log_path = session_log_path_for(os.getcwd(), session_id)
    log_session(session_id, str(log_path))
    return await loop._run_loop(query, user_action_data)


def _run_update_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="perm update", description="Update a managed Permanent install")
    parser.add_argument(
        "--version",
        "--ref",
        dest="ref",
        default=None,
        help="tag to install (default: newest vX.Y.Z)",
    )
    args = parser.parse_args(argv)
    return run_update(ref=args.ref)


def main() -> None:
    load_dotenv()
    rest = sys.argv[1:]
    if rest and rest[0] == "update" and (len(rest) == 1 or rest[1].startswith("-")):
        raise SystemExit(_run_update_cli(rest[1:]))
    parser = argparse.ArgumentParser(description="Permanent")
    parser.add_argument(
        "-s",
        "--session",
        default=None,
        help="会话 id；不传则每次新建 cli-时间戳",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="打开最小 TUI（没有 query 时默认也走 TUI）",
    )
    parser.add_argument("query", nargs="*", help="用户问题；省略则进入 TUI")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    session_id = args.session or datetime.now().strftime("cli-%Y%m%d-%H%M%S")
    query = " ".join(args.query).strip()
    if args.tui or not query:
        from agent_loop.cli.tui import run_tui

        run_tui(args.session)
        return
    notice = peek_update()
    if notice:
        print(notice, file=sys.stderr)
    asyncio.run(_amain(query, session_id))


if __name__ == "__main__":
    main()
