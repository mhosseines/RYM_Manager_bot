"""
manage_sources.py
─────────────────
A simple command-line tool to add, list, or disable RSS sources.
Run this separately from bot.py (bot doesn't need to be running).

Usage examples:
  python manage_sources.py list
  python manage_sources.py add "Zoomit" "https://www.zoomit.ir/feed/"
  python manage_sources.py add "TechCrunch" "https://techcrunch.com/feed/"
  python manage_sources.py disable 2
  python manage_sources.py enable 2
"""

import asyncio
import sys
import aiosqlite
import database as db


async def cmd_list():
    await db.init_db()
    async with aiosqlite.connect(db.DB_NAME) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, name, url, active, priority FROM sources ORDER BY priority, id"
        )
        rows = await cursor.fetchall()

    if not rows:
        print("No sources added yet.")
        print('Add one with:  python manage_sources.py add "Name" "https://feed-url"')
        return

    print(f"\n{'ID':<4} {'Active':<8} {'Priority':<10} {'Name':<20} URL")
    print("─" * 80)
    for r in rows:
        active = "✅ yes" if r["active"] else "❌ no"
        print(f"{r['id']:<4} {active:<8} {r['priority']:<10} {r['name']:<20} {r['url']}")
    print()


async def cmd_add(name: str, url: str, priority: int = 5):
    await db.init_db()
    source_id = await db.add_source(name=name, url=url, priority=priority)
    if source_id:
        print(f"✅ Added source #{source_id}: {name}")
        print(f"   URL: {url}")
        print(f"   Priority: {priority}")
    else:
        print(f"⚠️  This URL already exists in the database.")


async def cmd_set_active(source_id: int, active: bool):
    await db.init_db()
    await db.set_source_active(source_id, active)
    state = "enabled" if active else "disabled"
    print(f"✅ Source #{source_id} has been {state}.")


def print_help():
    print(__doc__)


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print_help()

    elif args[0] == "list":
        asyncio.run(cmd_list())

    elif args[0] == "add":
        if len(args) < 3:
            print('Usage: python manage_sources.py add "Source Name" "https://feed-url"')
            print('Optional: add a priority number at the end (default 5, lower = higher priority)')
            print('Example: python manage_sources.py add "Zoomit" "https://zoomit.ir/feed/" 1')
        else:
            name     = args[1]
            url      = args[2]
            priority = int(args[3]) if len(args) > 3 else 5
            asyncio.run(cmd_add(name, url, priority))

    elif args[0] == "disable":
        if len(args) < 2:
            print("Usage: python manage_sources.py disable <id>")
        else:
            asyncio.run(cmd_set_active(int(args[1]), False))

    elif args[0] == "enable":
        if len(args) < 2:
            print("Usage: python manage_sources.py enable <id>")
        else:
            asyncio.run(cmd_set_active(int(args[1]), True))

    else:
        print(f"Unknown command: {args[0]}")
        print_help()