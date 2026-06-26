"""GitHub scraper implementation."""

import logging
import os
import base64
from datetime import datetime
from typing import List, Optional, Any
import httpx

from .base import BaseScraper
from ..models import ContentItem, SourceType, GitHubSourceConfig

logger = logging.getLogger(__name__)


class GitHubScraper(BaseScraper):
    """Scraper for GitHub events and releases."""

    def __init__(self, sources: List[GitHubSourceConfig], http_client: httpx.AsyncClient):
        """Initialize GitHub scraper.

        Args:
            sources: List of GitHub source configurations
            http_client: Shared async HTTP client
        """
        super().__init__({"sources": sources}, http_client)
        self.token = os.getenv("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"

    def _get_headers(self) -> dict:
        """Get request headers with optional authentication.

        Returns:
            dict: HTTP headers
        """
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Horizon-Aggregator"
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch GitHub content items.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        items = []
        sources = self.config["sources"]

        for source in sources:
            if not source.enabled:
                continue

            if source.type == "user_events" and source.username:
                user_items = await self._fetch_user_events(source.username, since)
                items.extend(user_items)
            elif source.type == "repo_releases" and source.owner and source.repo:
                release_items = await self._fetch_repo_releases(
                    source.owner, source.repo, since
                )
                items.extend(release_items)

        return items

    async def _fetch_user_events(
        self,
        username: str,
        since: datetime
    ) -> List[ContentItem]:
        """Fetch public events for a user.

        Args:
            username: GitHub username
            since: Only fetch events after this time

        Returns:
            List[ContentItem]: Event content items
        """
        url = f"{self.base_url}/users/{username}/events/public"
        items = []

        try:
            response = await self.client.get(url, headers=self._get_headers(), follow_redirects=True)
            response.raise_for_status()
            events = response.json()

            for event in events:
                created_at = datetime.fromisoformat(
                    event["created_at"].replace("Z", "+00:00")
                )

                if created_at < since:
                    continue

                # Filter interesting event types
                event_type = event["type"]
                if event_type not in [
                    "PushEvent", "CreateEvent", "ReleaseEvent",
                    "PublicEvent", "WatchEvent"
                ]:
                    continue

                item = await self._parse_event(event, username)
                if item:
                    items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching GitHub events for %s: %s", username, e)

        return items

    async def _parse_event(self, event: dict, username: str) -> Optional[ContentItem]:
        """Parse GitHub event into ContentItem.

        Args:
            event: GitHub event data
            username: GitHub username

        Returns:
            Optional[ContentItem]: Parsed content item or None
        """
        event_type = event["type"]
        event_id = event["id"]
        created_at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))

        repo_name = event["repo"]["name"]
        repo_url = f"https://github.com/{repo_name}"
        repo_context = ""

        # Generate title and content based on event type
        if event_type == "PushEvent":
            commits = event["payload"].get("commits", [])
            title = f"{username} pushed {len(commits)} commit(s) to {repo_name}"
            content = "\n".join([c.get("message", "") for c in commits[:3]])
            if not content:
                content = await self._fetch_recent_commit_messages(repo_name)
        elif event_type == "CreateEvent":
            ref_type = event["payload"].get("ref_type", "repository")
            title = f"{username} created {ref_type} in {repo_name}"
            content = event["payload"].get("description", "")
        elif event_type == "ReleaseEvent":
            release = event["payload"].get("release", {})
            title = f"{username} released {release.get('tag_name', '')} in {repo_name}"
            content = release.get("body", "")
            repo_url = release.get("html_url", repo_url)
        elif event_type == "PublicEvent":
            title = f"{username} made {repo_name} public"
            content = ""
        elif event_type == "WatchEvent":
            title = f"{username} starred {repo_name}"
            content = ""
        else:
            return None

        if not content:
            repo_context = await self._fetch_repo_context(repo_name)
            content = repo_context

        return ContentItem(
            id=self._generate_id("github", "event", event_id),
            source_type=SourceType.GITHUB,
            title=title,
            url=repo_url,
            content=content,
            author=username,
            published_at=created_at,
            metadata={
                "event_type": event_type,
                "repo": repo_name,
                "repo_context_fetched": bool(repo_context),
            }
        )

    async def _fetch_repo_releases(
        self,
        owner: str,
        repo: str,
        since: datetime
    ) -> List[ContentItem]:
        """Fetch releases for a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            since: Only fetch releases after this time

        Returns:
            List[ContentItem]: Release content items
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/releases"
        items = []

        try:
            response = await self.client.get(url, headers=self._get_headers(), follow_redirects=True)
            response.raise_for_status()
            releases = response.json()

            for release in releases:
                published_at = datetime.fromisoformat(
                    release["published_at"].replace("Z", "+00:00")
                )

                if published_at < since:
                    continue

                content = release.get("body", "")
                repo_context = ""
                if not content:
                    repo_context = await self._fetch_repo_context(f"{owner}/{repo}")
                    content = repo_context

                item = ContentItem(
                    id=self._generate_id("github", "release", str(release["id"])),
                    source_type=SourceType.GITHUB,
                    title=f"{owner}/{repo} released {release['tag_name']}",
                    url=release["html_url"],
                    content=content,
                    author=release["author"]["login"],
                    published_at=published_at,
                    metadata={
                        "repo": f"{owner}/{repo}",
                        "tag": release["tag_name"],
                        "prerelease": release.get("prerelease", False),
                        "repo_context_fetched": bool(repo_context),
                    }
                )
                items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching releases for %s/%s: %s", owner, repo, e)

        return items

    async def _fetch_recent_commit_messages(self, repo_name: str, limit: int = 3) -> str:
        url = f"{self.base_url}/repos/{repo_name}/commits"
        try:
            response = await self.client.get(
                url,
                params={"per_page": limit},
                headers=self._get_headers(),
                follow_redirects=True,
            )
            response.raise_for_status()
            commits = response.json()
        except httpx.HTTPError as e:
            logger.warning("Error fetching commits for %s: %s", repo_name, e)
            return ""

        messages = []
        for commit in commits[:limit]:
            message = (
                commit.get("commit", {})
                .get("message", "")
                .strip()
            )
            if message:
                messages.append(message)
        return "\n".join(messages)

    async def _fetch_repo_context(self, repo_name: str) -> str:
        repo = await self._fetch_repo_metadata(repo_name)
        readme = await self._fetch_repo_readme(repo_name)

        parts = []
        if repo:
            description = repo.get("description") or ""
            language = repo.get("language") or "unknown"
            stars = repo.get("stargazers_count")
            topics = repo.get("topics") or []
            parts.append(f"Repository: {repo_name}")
            if description:
                parts.append(f"Description: {description}")
            parts.append(f"Language: {language}")
            if stars is not None:
                parts.append(f"Stars: {stars}")
            if topics:
                parts.append(f"Topics: {', '.join(topics[:8])}")
        if readme:
            parts.append("")
            parts.append("README excerpt:")
            parts.append(readme[:2000])
        return "\n".join(parts).strip()

    async def _fetch_repo_metadata(self, repo_name: str) -> dict[str, Any]:
        url = f"{self.base_url}/repos/{repo_name}"
        try:
            response = await self.client.get(
                url,
                headers={**self._get_headers(), "Accept": "application/vnd.github+json"},
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.warning("Error fetching repo metadata for %s: %s", repo_name, e)
            return {}

    async def _fetch_repo_readme(self, repo_name: str) -> str:
        url = f"{self.base_url}/repos/{repo_name}/readme"
        try:
            response = await self.client.get(
                url,
                headers=self._get_headers(),
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as e:
            logger.warning("Error fetching README for %s: %s", repo_name, e)
            return ""

        content = payload.get("content") or ""
        if not content:
            return ""
        try:
            decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return ""
        return decoded.strip()
