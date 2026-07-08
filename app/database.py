"""
app/database.py

Backend module for [Agent-Pick] project.
Responsible for taking crawled + analyzed content and persisting it
into a Notion database using the notion-client SDK.

All comments, docstrings, and log messages are written in English only,
to avoid encoding issues (SyntaxError: Non-UTF-8 code...) when the
script is executed on Windows terminals.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from notion_client import Client
from notion_client.errors import APIResponseError


class NotionDatabaseSaver:
    """
    Saves analyzed archive data (place / event / recipe / tip / other)
    into a target Notion database.

    The Notion database is expected to have the following properties
    (property name -> Notion property type):
        - title          -> title
        - category        -> select
        - tags            -> multi_select
        - url             -> url
        - summary         -> rich_text
        - address         -> rich_text   (only for category == "place")
        - map_deeplink    -> url         (only for category == "place")
        - event_date      -> date        (only for category == "event")
    """

    def __init__(self, notion_token: str, database_id: str):
        """
        Initialize the Notion client and store the target database id.

        Args:
            notion_token: Notion integration token (starts with "secret_" or "ntn_").
            database_id: 32-character Notion database id (dashes optional).
        """
        if not notion_token:
            raise ValueError("notion_token must not be empty.")
        if not database_id:
            raise ValueError("database_id must not be empty.")

        self.client = Client(auth=notion_token)
        self.database_id = database_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def save_archive(
        self,
        crawl_data: Dict[str, Any],
        analysis_data: Dict[str, Any],
        deeplink_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Build a Notion page properties payload from the three input
        dictionaries and create a new page (row) in the target database.

        Args:
            crawl_data: Result from the crawler module. Expected keys: "url".
            analysis_data: Result from the LLM analysis module. Expected keys:
                "category", "summary", "place_name", "address",
                "event_title", "event_date", "tags".
            deeplink_data: Result from the deeplink generator module.
                Expected keys: "map_deeplink". Optional overall, and only
                meaningful when category == "place".

        Returns:
            True if the page was created successfully, False otherwise.
        """
        deeplink_data = deeplink_data or {}

        try:
            properties = self._build_properties(crawl_data, analysis_data, deeplink_data)

            response = self.client.pages.create(
                parent={"database_id": self.database_id},
                properties=properties,
            )

            page_id = response.get("id", "unknown")
            page_title = self._resolve_title(analysis_data)

            print("[SUCCESS] Archive saved to Notion database.")
            print(f"  - Page ID   : {page_id}")
            print(f"  - Title     : {page_title}")
            print(f"  - Category  : {analysis_data.get('category')}")
            print(f"  - Source URL: {crawl_data.get('url')}")
            return True

        except APIResponseError as e:
            print("[ERROR] Notion API responded with an error.")
            print(f"  - Reason: {e}")
            return False

        except ValueError as e:
            print("[ERROR] Invalid input data.")
            print(f"  - Reason: {e}")
            return False

        except Exception as e:
            print("[ERROR] Unexpected error while saving archive to Notion.")
            print(f"  - Reason: {e}")
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_title(self, analysis_data: Dict[str, Any]) -> str:
        """
        Resolve the page title following the priority:
        place_name -> event_title -> "No Name".
        """
        place_name = analysis_data.get("place_name")
        event_title = analysis_data.get("event_title")

        if place_name:
            return place_name
        if event_title:
            return event_title
        return "No Name"

    def _build_properties(
        self,
        crawl_data: Dict[str, Any],
        analysis_data: Dict[str, Any],
        deeplink_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert the three input dictionaries into a Notion "properties"
        payload matching the database schema described in the module
        docstring.
        """
        category = analysis_data.get("category", "other")
        tags = analysis_data.get("tags") or []
        url = crawl_data.get("url", "")
        summary = analysis_data.get("summary", "")

        title_text = self._resolve_title(analysis_data)

        properties: Dict[str, Any] = {
            "title": {
                "title": [
                    {"text": {"content": title_text}}
                ]
            },
            "category": {
                "select": {"name": category}
            },
            "tags": {
                "multi_select": [{"name": tag} for tag in tags]
            },
            "url": {
                "url": url or None
            },
            "summary": {
                "rich_text": [
                    {"text": {"content": summary}}
                ]
            },
        }

        # Conditional properties: only attached when relevant to the category.
        if category == "place":
            address = analysis_data.get("address")
            map_deeplink = deeplink_data.get("map_deeplink")

            if address:
                properties["address"] = {
                    "rich_text": [
                        {"text": {"content": address}}
                    ]
                }

            if map_deeplink:
                properties["map_deeplink"] = {
                    "url": map_deeplink
                }

        elif category == "event":
            event_date = analysis_data.get("event_date")

            if event_date:
                properties["event_date"] = {
                    "date": {"start": event_date}
                }

        return properties


# ----------------------------------------------------------------------
# Manual test run: python app/database.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # TODO: Fill in your own Notion integration token here.
    NOTION_TOKEN = "your_notion_integration_token_here"

    # TODO: Fill in your own 32-character Notion database id here.
    DATABASE_ID = "your_32_char_database_id_here"

    # Sample virtual data simulating a "place" category archive.
    test_crawl = {
        "url": "https://www.instagram.com/p/sample_post_id/"
    }

    test_analysis = {
        "category": "place",
        "summary": "A cozy hidden pasta restaurant loved by locals in Seongsu-dong.",
        "place_name": "Pasta Factory Seongsu",
        "address": "Seoul, Seongdong-gu, Seongsu-dong 2-ga, 123-45",
        "event_title": None,
        "event_date": None,
        "tags": ["Seongsu", "Pasta", "HiddenGem", "DatePlace"],
    }

    test_deeplink = {
        "map_deeplink": "https://map.naver.com/p/entry/place/sample_place_id"
    }

    saver = NotionDatabaseSaver(notion_token=NOTION_TOKEN, database_id=DATABASE_ID)
    result = saver.save_archive(
        crawl_data=test_crawl,
        analysis_data=test_analysis,
        deeplink_data=test_deeplink,
    )

    if result:
        print("[TEST] save_archive() returned True. Check your Notion database.")
    else:
        print("[TEST] save_archive() returned False. See the error log above.")