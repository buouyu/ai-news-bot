from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from src.models import GitHubSourceConfig
from src.scrapers.github import GitHubScraper


def test_github_push_event_fetches_commits_when_payload_is_empty() -> None:
    scraper = GitHubScraper([], AsyncMock())
    scraper._fetch_recent_commit_messages = AsyncMock(return_value="Fix scheduler race")
    scraper._fetch_repo_context = AsyncMock(return_value="Repository context")

    event = {
        "type": "PushEvent",
        "id": "evt1",
        "created_at": "2026-06-26T00:00:00Z",
        "repo": {"name": "example/project"},
        "payload": {"commits": []},
    }

    item = asyncio.run(scraper._parse_event(event, "alice"))

    assert item is not None
    assert item.content == "Fix scheduler race"
    assert item.metadata["repo_context_fetched"] is False


def test_github_release_fetches_repo_context_when_body_is_empty() -> None:
    release_response = MagicMock()
    release_response.raise_for_status.return_value = None
    release_response.json.return_value = [
        {
            "id": 1,
            "tag_name": "v1.0.0",
            "html_url": "https://github.com/example/project/releases/tag/v1.0.0",
            "body": "",
            "published_at": "2026-06-26T00:00:00Z",
            "author": {"login": "maintainer"},
            "prerelease": False,
        }
    ]
    client = AsyncMock()
    client.get.return_value = release_response

    scraper = GitHubScraper(
        [GitHubSourceConfig(type="repo_releases", owner="example", repo="project")],
        client,
    )
    scraper._fetch_repo_context = AsyncMock(return_value="README excerpt")

    items = asyncio.run(
        scraper._fetch_repo_releases(
            "example",
            "project",
            datetime(2026, 6, 25, tzinfo=timezone.utc),
        )
    )

    assert len(items) == 1
    assert items[0].content == "README excerpt"
    assert items[0].metadata["repo_context_fetched"] is True
