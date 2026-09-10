"""本地 CLI：python -m agent_loop --session wy1 "随便写一段 python 并运行一下" """

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime

from agent_loop.loop import ReactAgentLoop
from agent_loop.envfile import load_dotenv
from agent_loop.cli.trace import log_session


class CliDeps:
    async def ensure_sandbox_async(self):
        return None


async def _amain(query: str, session_id: str) -> str:
    loop = ReactAgentLoop(CliDeps(), None, None)
    user_action_data = {
        "sessionId": session_id,
        "workspace": os.getcwd(),
    }
    log_session(session_id, f".agent/sessions/{session_id}/session.jsonl")
    return await loop._run_loop(query, user_action_data)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="独立 agent loop")
    parser.add_argument(
        "-s",
        "--session",
        default=None,
        help="会话 id，日志写到 .agent/sessions/<id>/session.jsonl；不传则每次新建",
    )
    parser.add_argument("query", nargs="*", help="用户问题")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    query = " ".join(args.query).strip()
    if not query:
        query = input("you> ").strip()
    if not query:
        parser.print_help()
        raise SystemExit(1)
    session_id = args.session or datetime.now().strftime("cli-%Y%m%d-%H%M%S")
    asyncio.run(_amain(query, session_id))


if __name__ == "__main__":
    main()
