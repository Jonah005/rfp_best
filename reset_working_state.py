"""Fresh start for the working state: every chat, every response draft, every drafted position.

Run from D:\rfpproject with the app STOPPED:

    python reset_working_state.py          (shows what would be removed, removes nothing)
    python reset_working_state.py --yes    (asks once more, then removes it)

Removed: chat messages and their summaries, the chat threads themselves, response drafts
(with their versions, statements and pictures), drafted positions for requirements with no
evidence, and the write-up finalisation records.

Kept: tenders, requirements, tasks, evidence, missing parts and the assumptions recorded for
them, questions to other departments and their replies, users and roles.
"""

from __future__ import annotations

import sys

import run_local
from sqlalchemy import create_engine, text

# Order matters: children before parents.
TABLES = [
    ("chat messages", "chat_message"),
    ("chat summaries", "conversation_summary"),
    ("chat threads", "task_conversation"),
    ("drafted positions", "requirement_position"),
    ("write-up finalisations", "writeup_finalization"),
    ("draft hold marks", "draft_hold"),
    ("response drafts", "response_artifact"),  # versions, statements, pictures go with it
]


def main() -> int:
    url = run_local.remembered_database_url()
    if not url:
        url = run_local.ask_for_database_url()
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, future=True)
    apply = "--yes" in sys.argv
    with engine.begin() as conn:
        print("Currently stored:")
        for label, table in TABLES:
            n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            print(f"  {label:<24} {n}")
        if not apply:
            print("\nNothing was removed. Run again with --yes to clear all of the above.")
            return 0
        if input("\nType CLEAR to remove all of it: ").strip() != "CLEAR":
            print("Cancelled; nothing was removed.")
            return 1
        for label, table in TABLES:
            n = conn.execute(text(f"DELETE FROM {table}")).rowcount
            print(f"  removed {n} {label}")
    print("\nDone. Start run_local.py again and press Ctrl+F5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
