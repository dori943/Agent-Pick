"""
app/setup_notion_db.py

One-time setup script: creates a Notion database with the exact schema
that database.py's NotionDatabaseSaver expects, so you don't have to
click through adding 9 properties by hand.

Prerequisites (the one manual step Notion's permission model requires):
  1. In Notion, create any empty page (e.g. "Agent-Pick").
  2. Open it -> "..." menu -> Connections -> connect your integration.
  3. Copy that page's id from its URL:
     https://www.notion.so/Agent-Pick-<PAGE_ID>
     (the 32-char hex string, dashes optional)

Then set in your .env:
    NOTION_TOKEN=ntn_...
    NOTION_PARENT_PAGE_ID=<the page id from step 3>

Run:
    python app/setup_notion_db.py

It prints the new database_id and, if NOTION_DATABASE_ID isn't already
set in .env, appends it for you automatically.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError

load_dotenv()

ENV_PATH = Path(".env")

DATABASE_TITLE = "Agent-Pick Archive"

# Mirrors the schema documented in database.py's NotionDatabaseSaver docstring.
PROPERTIES_SCHEMA = {
    "title": {"title": {}},
    "category": {
        "select": {
            "options": [
                {"name": "place"},
                {"name": "event"},
                {"name": "recipe"},
                {"name": "tip"},
                {"name": "other"},
            ]
        }
    },
    "tags": {"multi_select": {}},
    "url": {"url": {}},
    "summary": {"rich_text": {}},
    "address": {"rich_text": {}},
    "map_deeplink": {"url": {}},
    "event_date": {"date": {}},
    "status": {
        "select": {
            "options": [
                {"name": "success"},
                {"name": "failed"},
            ]
        }
    },
    "error_log": {"rich_text": {}},
}


def _normalize_page_id(raw_id: str) -> str:
    """Strip dashes/whitespace; Notion accepts ids with or without dashes."""
    return raw_id.strip()


def create_database(client: Client, parent_page_id: str) -> str:
    response = client.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": DATABASE_TITLE}}],
        properties=PROPERTIES_SCHEMA,
    )
    return response["id"]


def update_env_file(database_id: str) -> None:
    """
    Append NOTION_DATABASE_ID to .env if it's not already set there.
    Never overwrites an existing value automatically -- if one exists,
    just prints it so you can decide whether to replace it by hand.
    """
    if not ENV_PATH.exists():
        print(f"[WARN] .env not found at {ENV_PATH.resolve()} -- skipping auto-write.")
        print(f"        Add this line yourself: NOTION_DATABASE_ID={database_id}")
        return

    content = ENV_PATH.read_text(encoding="utf-8")

    if re.search(r"^NOTION_DATABASE_ID=", content, flags=re.MULTILINE):
        print("[INFO] NOTION_DATABASE_ID already present in .env -- not overwriting.")
        print(f"        New database_id if you want to switch: {database_id}")
        return

    with ENV_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\nNOTION_DATABASE_ID={database_id}\n")
    print("[INFO] Appended NOTION_DATABASE_ID to .env.")


def main() -> None:
    notion_token = os.environ.get("NOTION_TOKEN")
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")

    if not notion_token:
        raise SystemExit("[ERROR] NOTION_TOKEN is not set in .env.")
    if not parent_page_id:
        raise SystemExit(
            "[ERROR] NOTION_PARENT_PAGE_ID is not set in .env.\n"
            "Create an empty Notion page, connect your integration to it, "
            "and put that page's id here."
        )

    client = Client(auth=notion_token)
    parent_page_id = _normalize_page_id(parent_page_id)

    try:
        database_id = create_database(client, parent_page_id)
    except APIResponseError as e:
        print("[ERROR] Notion API rejected the database creation request.")
        print(f"  - Reason: {e}")
        print(
            "  - Common cause: the integration hasn't been connected to "
            "the parent page yet, or NOTION_PARENT_PAGE_ID is wrong."
        )
        raise SystemExit(1) from e

    print("[SUCCESS] Database created.")
    print(f"  - database_id: {database_id}")
    update_env_file(database_id)


if __name__ == "__main__":
    main()