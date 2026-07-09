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
        - status          -> select      (e.g. "success" / "failed"), used for debugging
        - error_log       -> rich_text   (error message when status == "failed")
    """

    VALID_CATEGORIES = {"place", "event", "recipe", "tip", "other"}

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
        status: str = "success",
        error_message: Optional[str] = None,
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
            status: Pipeline outcome for this archive, e.g. "success" or
                "failed". Recorded so a failed run (e.g. crawl succeeded but
                deeplink generation failed) is still visible in Notion for
                debugging, instead of leaving no trace at all.
            error_message: Optional error detail to store when status is
                "failed". Ignored when status == "success".

        Returns:
            True if the page was created successfully, False otherwise.
        """
        deeplink_data = deeplink_data or {}

        try:
            properties = self._build_properties(
                crawl_data, analysis_data, deeplink_data, status, error_message
            )

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

    def find_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Look up an existing page in the database by source url.

        This is the piece that makes the "token saving" cache work: before
        the pipeline spends new LLM/deeplink tokens on an Instagram post,
        the server should call this first. If a matching row already
        exists, its saved map_deeplink / summary can be reused directly.

        Args:
            url: The Instagram post url to search for.

        Returns:
            A dict with the cached fields (page_id, url, map_deeplink,
            summary, status) if a match is found, otherwise None.
        """
        if not url:
            return None

        try:
            response = self.client.databases.query(
                database_id=self.database_id,
                filter={"property": "url", "url": {"equals": url}},
                page_size=1,
            )
        except APIResponseError as e:
            print("[ERROR] Notion API responded with an error while querying cache.")
            print(f"  - Reason: {e}")
            return None
        except Exception as e:
            print("[ERROR] Unexpected error while querying Notion cache.")
            print(f"  - Reason: {e}")
            return None

        results = response.get("results", [])
        if not results:
            return None

        page = results[0]
        props = page.get("properties", {})

        def _get_url(prop_name: str) -> Optional[str]:
            return props.get(prop_name, {}).get("url")

        def _get_rich_text(prop_name: str) -> str:
            blocks = props.get(prop_name, {}).get("rich_text", [])
            return "".join(block.get("plain_text", "") for block in blocks)

        def _get_select(prop_name: str) -> Optional[str]:
            select = props.get(prop_name, {}).get("select")
            return select.get("name") if select else None

        return {
            "page_id": page.get("id"),
            "url": _get_url("url"),
            "map_deeplink": _get_url("map_deeplink"),
            "summary": _get_rich_text("summary"),
            "status": _get_select("status"),
        }

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
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Convert the three input dictionaries into a Notion "properties"
        payload matching the database schema described in the module
        docstring.
        """
        category = analysis_data.get("category", "other")
        if category not in self.VALID_CATEGORIES:
            category = "other"

        tags = analysis_data.get("tags") or []
        if not isinstance(tags, list):
            tags = []

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
            "status": {
                "select": {"name": status}
            },
        }

        # Only record an error_log entry when there's actually an error
        # message, to avoid cluttering successful rows with an empty field.
        if status != "success" and error_message:
            properties["error_log"] = {
                "rich_text": [
                    {"text": {"content": str(error_message)[:2000]}}
                ]
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