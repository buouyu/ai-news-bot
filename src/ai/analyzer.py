"""Content analysis using AI."""

import asyncio
import json
import re
from typing import List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from .client import AIClient
from .prompts import CONTENT_ANALYSIS_SYSTEM, CONTENT_ANALYSIS_USER
from .utils import parse_json_response
from ..models import ContentItem

DEFAULT_THROTTLE_SEC = 0.0

NON_TECHNICAL_NEWS_PATTERNS = [
    r"\bdied\b",
    r"\bobituary\b",
    r"\bpassed away\b",
    r"\bmemorial\b",
    r"\btribute\b",
    r"\bfunding\b",
    r"\braised \$",
    r"\bacquir(?:e|ed|es|ing)\b",
    r"\bipo\b",
    r"\bstock\b",
    r"\bearnings\b",
    r"\blayoffs?\b",
    r"\blawsuit\b",
    r"\bregulation\b",
    r"\bpolicy\b",
    r"\bprivacy\b",
    r"\bmarket\b",
    r"\bceo\b",
    r"人物",
    r"融资",
    r"收购",
    r"上市",
    r"财报",
    r"裁员",
    r"诉讼",
    r"监管",
    r"政策",
    r"隐私",
]

TECHNICAL_SIGNAL_PATTERNS = [
    r"\balgorithm\b",
    r"\barchitecture\b",
    r"\bbenchmark\b",
    r"\bapi\b",
    r"\bprotocol\b",
    r"\bcompiler\b",
    r"\bruntime\b",
    r"\brelease\b",
    r"\bversion\b",
    r"\bcommit\b",
    r"\bpatch\b",
    r"\bcode\b",
    r"\bopen source\b",
    r"\bgithub\b",
    r"\bsecurity\b",
    r"\bvulnerability\b",
    r"\bcve\b",
    r"\bmodel\b",
    r"\btraining\b",
    r"\binference\b",
    r"\bdataset\b",
    r"\btransformer\b",
    r"\bllm\b",
    r"\bcuda\b",
    r"\bpython\b",
    r"\brust\b",
    r"\btypescript\b",
    r"\bcli\b",
    r"\bframework\b",
    r"\bdebugg(?:er|ing)\b",
    r"性能",
    r"架构",
    r"算法",
    r"模型",
    r"协议",
    r"接口",
    r"开源",
    r"漏洞",
    r"基准",
    r"版本",
    r"发布",
    r"工程",
    r"实现",
]


class ContentAnalyzer:
    """Analyzes content items using AI to determine importance."""

    def __init__(self, ai_client: AIClient):
        self.client = ai_client

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_concurrency(self) -> int:
        """Return the configured analysis concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "analysis_concurrency", 1)
        return max(concurrency, 1)

    def _get_language_instruction(self) -> str:
        """Return output-language guidance for fields generated during scoring."""
        config = getattr(self.client, "config", None)
        languages = getattr(config, "languages", None) or ["en"]
        if languages == ["zh"]:
            return (
                "\nLanguage requirement: Write reason, summary, and tags in Simplified Chinese. "
                "Keep only widely-used technical terms, acronyms, product names, and proper nouns in English."
            )
        if languages and languages[0] == "zh":
            return (
                "\nLanguage requirement: Prefer Simplified Chinese for reason, summary, and tags. "
                "Keep widely-used technical terms, acronyms, product names, and proper nouns in English."
            )
        return ""

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        concurrency = self._get_concurrency()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ContentItem, index: int, progress_task) -> ContentItem:
            async with semaphore:
                try:
                    await self._analyze_item(item)
                except Exception as e:
                    print(f"Error analyzing item {item.id}: {e}")
                    item.ai_score = 0.0
                    item.ai_reason = "Analysis failed"
                    item.ai_summary = item.title
                if throttle_sec > 0 and index < len(items) - 1:
                    await asyncio.sleep(throttle_sec)
            progress.advance(progress_task)
            return item

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Analyzing", total=len(items))
            coros = [
                _process(item, i, task) for i, item in enumerate(items)
            ]
            analyzed_items = await asyncio.gather(*coros)

        return analyzed_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Analyze a single content item.

        Args:
            item: Content item to analyze (modified in-place)
        """
        # Prepare content section
        content_section = ""
        if item.content:
            # Split off comments if present
            content_text = item.content
            if "--- Top Comments ---" in content_text:
                main, comments_part = content_text.split("--- Top Comments ---", 1)
                content_section = f"Content: {main.strip()[:800]}"
            else:
                content_section = f"Content: {content_text[:1000]}"

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        if item.content and "--- Top Comments ---" in item.content:
            comments_part = item.content.split("--- Top Comments ---", 1)[1]
            discussion_parts.append(f"Community Comments:\n{comments_part[:1500]}")

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        # Generate user prompt
        user_prompt = CONTENT_ANALYSIS_USER.format(
            title=item.title,
            source=f"{item.source_type.value}",
            author=item.author or "Unknown",
            url=str(item.url),
            content_section=content_section,
            discussion_section=discussion_section,
            language_instruction=self._get_language_instruction(),
        )

        # Get AI completion
        response = await self.client.complete(
            system=CONTENT_ANALYSIS_SYSTEM,
            user=user_prompt,
        )

        # Parse JSON response with robust fallback
        result = self._parse_json_response(response)
        if result is None:
            print(f"Warning: could not parse analysis response for {item.id}, using defaults")
            item.ai_score = 0.0
            item.ai_reason = "Analysis response parse failed"
            item.ai_summary = item.title
            item.ai_tags = []
            return

        # Update item with analysis results
        item.ai_score = float(result.get("score", 0))
        item.ai_reason = result.get("reason", "")
        item.ai_summary = result.get("summary", item.title)
        item.ai_tags = result.get("tags", [])
        self._apply_technical_focus_caps(item)

    def _apply_technical_focus_caps(self, item: ContentItem) -> None:
        """Keep general informational news out of a technical digest."""
        if item.ai_score is None:
            return

        haystack = " ".join(
            [
                item.title or "",
                item.content or "",
                item.ai_summary or "",
                " ".join(item.ai_tags or []),
            ]
        )
        if not self._matches_any(haystack, NON_TECHNICAL_NEWS_PATTERNS):
            return
        if self._matches_any(haystack, TECHNICAL_SIGNAL_PATTERNS):
            return

        original_score = item.ai_score
        item.ai_score = min(item.ai_score, 4.0)
        note = (
            f"Technical-focus cap applied: informational/general-news item "
            f"without concrete technical detail (original score {original_score:g})."
        )
        item.ai_reason = f"{item.ai_reason} {note}".strip()

    @staticmethod
    def _matches_any(text: str, patterns: List[str]) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)
