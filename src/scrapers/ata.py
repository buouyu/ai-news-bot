"""ATA hot article scraper implementation."""

import asyncio
import logging
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import BaseScraper
from ..models import ATAConfig, ContentItem, SourceType

logger = logging.getLogger(__name__)

asyncio_to_thread = asyncio.to_thread
asyncio_sleep = asyncio.sleep

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class ATAScraper(BaseScraper):
    """Scraper for ATA daily hot articles."""

    def __init__(self, config: ATAConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.ata_config = config
        self.base_url = str(config.base_url).rstrip("/")

    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.ata_config.enabled:
            return []

        cookie_source = (self.ata_config.cookie_source or "").lower()
        if cookie_source == "applescript":
            return await self._fetch_with_chrome_applescript()
        if cookie_source == "playwright":
            return await self._fetch_with_playwright()

        cookies = self._get_cookie_headers()
        if not cookies:
            logger.warning(
                "ATA cookie is unavailable. Log in with Chrome or set %s; skipping ATA hot articles.",
                self.ata_config.cookie_env,
            )
            return []

        hot_articles: List[dict[str, Any]] = []
        selected_cookie = ""
        for cookie in cookies:
            if not self._has_auth_cookie(cookie):
                logger.warning(
                    "ATA cookie was loaded, but no ATA auth cookie was found. "
                    "If Chrome uses a non-default profile, set sources.ata.chrome_cookie_file."
                )
            hot_articles = await self._fetch_hot_articles(cookie)
            if hot_articles:
                selected_cookie = cookie
                break

        if not hot_articles or not selected_cookie:
            return []

        if self.ata_config.fetch_limit:
            hot_articles = hot_articles[: self.ata_config.fetch_limit]

        items: List[ContentItem] = []
        for article in hot_articles:
            item = await self._article_to_item(article, selected_cookie)
            if item:
                items.append(item)
        return items

    async def _fetch_with_chrome_applescript(self) -> List[ContentItem]:
        hot_url = urljoin(f"{self.base_url}/", self.ata_config.hot_path.lstrip("/"))
        tab_id = ""
        try:
            tab_id = await asyncio_to_thread(
                self._run_osascript,
                f'''
                tell application "Google Chrome"
                    if (count of windows) = 0 then make new window
                    set targetWindow to front window
                    set tempTab to make new tab at end of tabs of targetWindow with properties {{URL:"{self.base_url}/articles?sort=latest"}}
                    return id of tempTab
                end tell
                ''',
            )
            await self._wait_for_chrome_page(tab_id)
            payload_text = await self._chrome_fetch_text(tab_id, hot_url)
            payload = json.loads(payload_text)

            hot_articles = self._parse_hot_payload(payload)
            if self.ata_config.fetch_limit:
                hot_articles = hot_articles[: self.ata_config.fetch_limit]

            items: List[ContentItem] = []
            for article in hot_articles:
                item = await self._applescript_article_to_item(article, tab_id)
                if item:
                    items.append(item)
            return items
        except subprocess.CalledProcessError as e:
            logger.warning(
                "ATA Chrome AppleScript fetch failed: %s. Enable Chrome menu "
                "View > Developer > Allow JavaScript from Apple Events.",
                (e.stderr or e.stdout or str(e)).strip(),
            )
            return []
        except Exception as e:
            logger.warning("ATA Chrome AppleScript fetch failed: %s", e)
            return []
        finally:
            if tab_id:
                try:
                    await asyncio_to_thread(self._close_chrome_tab, tab_id)
                except Exception:
                    pass

    async def _applescript_article_to_item(
        self, article: dict[str, Any], tab_id: str
    ) -> Optional[ContentItem]:
        article_id = str(article.get("articleId") or "").strip()
        title = str(article.get("title") or "").strip()
        if not article_id or not title:
            return None

        article_url = f"{self.base_url}/articles/{article_id}"
        detail = {}
        if self.ata_config.fetch_detail:
            try:
                html = await self._chrome_page_html(article_url, tab_id)
                detail = self._parse_article_html(html)
            except Exception as e:
                logger.warning("ATA Chrome article detail failed for %s: %s", article_url, e)

        published_at = detail.get("published_at") or datetime.now(timezone.utc)
        return ContentItem(
            id=self._generate_id(SourceType.ATA.value, "article", article_id),
            source_type=SourceType.ATA,
            title=detail.get("title") or title,
            url=article_url,
            content=detail.get("content") or detail.get("description") or title,
            author=detail.get("author"),
            published_at=published_at,
            metadata={
                "article_id": article_id,
                "category": self.ata_config.category,
                "source": "dayHotArticle",
                "auth_source": "applescript",
                "description": detail.get("description"),
            },
        )

    async def _chrome_fetch_text(self, tab_id: str, url: str) -> str:
        key = "data-horizon-ata-result"
        error_key = "data-horizon-ata-error"
        js = f"""
        (() => {{
            const root = document.documentElement;
            root.removeAttribute({json.dumps(key)});
            root.removeAttribute({json.dumps(error_key)});
            fetch({json.dumps(url)}, {{
                credentials: 'include',
                headers: {{
                    accept: 'application/json, text/plain, */*',
                    'cache-control': 'no-cache',
                    pragma: 'no-cache'
                }}
            }})
            .then(response => response.text())
            .then(text => root.setAttribute({json.dumps(key)}, text))
            .catch(error => root.setAttribute({json.dumps(error_key)}, String(error)));
            return 'started';
        }})()
        """
        await asyncio_to_thread(self._execute_chrome_javascript, js, tab_id)

        for _ in range(60):
            result = await asyncio_to_thread(
                self._execute_chrome_javascript,
                f"document.documentElement.getAttribute({json.dumps(key)}) || ''",
                tab_id,
            )
            if result:
                return result
            error = await asyncio_to_thread(
                self._execute_chrome_javascript,
                f"document.documentElement.getAttribute({json.dumps(error_key)}) || ''",
                tab_id,
            )
            if error:
                raise RuntimeError(error)
            await asyncio_sleep(0.5)
        raise TimeoutError("Timed out waiting for Chrome fetch result.")

    async def _chrome_page_html(self, url: str, tab_id: str) -> str:
        await asyncio_to_thread(
            self._run_osascript,
            f'''
            tell application "Google Chrome"
                set targetTab to my horizonFindTab("{tab_id}")
                set URL of targetTab to "{url}"
            end tell
            on horizonFindTab(tabId)
                tell application "Google Chrome"
                    repeat with w in windows
                        repeat with t in tabs of w
                            if (id of t as text) is tabId then return t
                        end repeat
                    end repeat
                end tell
                error "Horizon temporary Chrome tab was not found."
            end horizonFindTab
            ''',
        )
        await self._wait_for_chrome_page(tab_id)
        return await asyncio_to_thread(
            self._execute_chrome_javascript,
            "document.documentElement.outerHTML",
            tab_id,
        )

    async def _wait_for_chrome_page(self, tab_id: str) -> None:
        last_error = None
        for _ in range(60):
            try:
                state = await asyncio_to_thread(
                    self._execute_chrome_javascript,
                    "document.readyState",
                    tab_id,
                )
                if state in ("interactive", "complete"):
                    return
            except subprocess.CalledProcessError as e:
                last_error = (e.stderr or e.stdout or str(e)).strip()
                if "执行 JavaScript 的功能已关闭" in last_error or "Allow JavaScript" in last_error:
                    raise RuntimeError(last_error) from e
            await asyncio_sleep(0.5)
        if last_error:
            raise TimeoutError(f"Timed out waiting for Chrome page load. Last AppleScript error: {last_error}")
        raise TimeoutError("Timed out waiting for Chrome page load.")

    def _execute_chrome_javascript(self, javascript: str, tab_id: str) -> str:
        script = f'''
        tell application "Google Chrome"
            set targetTab to my horizonFindTab("{tab_id}")
            execute targetTab javascript {json.dumps(javascript)}
        end tell
        on horizonFindTab(tabId)
            tell application "Google Chrome"
                repeat with w in windows
                    repeat with t in tabs of w
                        if (id of t as text) is tabId then return t
                    end repeat
                end repeat
            end tell
            error "Horizon temporary Chrome tab was not found."
        end horizonFindTab
        '''
        return self._run_osascript(script)

    def _close_chrome_tab(self, tab_id: str) -> None:
        script = f'''
        tell application "Google Chrome"
            repeat with w in windows
                repeat with t in tabs of w
                    if (id of t as text) is "{tab_id}" then
                        close t
                        return
                    end if
                end repeat
            end repeat
        end tell
        '''
        self._run_osascript(script)

    @staticmethod
    def _run_osascript(script: str) -> str:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    async def _fetch_with_playwright(self) -> List[ContentItem]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "playwright is not installed; install it or set sources.ata.cookie_source to chrome/env."
            )
            return []

        user_data_dir = str(Path(self.ata_config.chrome_user_data_dir).expanduser())
        profile_directory = self.ata_config.chrome_profile_directory
        hot_url = urljoin(f"{self.base_url}/", self.ata_config.hot_path.lstrip("/"))

        try:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel="chrome",
                    headless=self.ata_config.headless,
                    args=[f"--profile-directory={profile_directory}"],
                )
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(f"{self.base_url}/articles?sort=latest", wait_until="domcontentloaded")
                    payload = await self._playwright_fetch_json(page, hot_url)
                    hot_articles = self._parse_hot_payload(payload)
                    if self.ata_config.fetch_limit:
                        hot_articles = hot_articles[: self.ata_config.fetch_limit]

                    items: List[ContentItem] = []
                    for article in hot_articles:
                        item = await self._playwright_article_to_item(page, article)
                        if item:
                            items.append(item)
                    return items
                finally:
                    await context.close()
        except Exception as e:
            logger.warning(
                "ATA Playwright Chrome fetch failed: %s. If Chrome is already open, close it "
                "and retry, or set sources.ata.chrome_profile_directory to the logged-in profile.",
                e,
            )
            return []

    async def _playwright_fetch_json(self, page, url: str) -> dict[str, Any]:
        result = await page.evaluate(
            """async (url) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers: {
                        accept: 'application/json, text/plain, */*',
                        'cache-control': 'no-cache',
                        pragma: 'no-cache'
                    }
                });
                return {
                    status: response.status,
                    url: response.url,
                    text: await response.text()
                };
            }""",
            url,
        )
        text = result.get("text") or ""
        try:
            payload = json.loads(text)
        except ValueError:
            logger.warning(
                "ATA Playwright hot article response is not JSON: status=%s url=%s body=%s",
                result.get("status"),
                result.get("url"),
                text[:300],
            )
            return {}
        return payload

    async def _playwright_article_to_item(self, page, article: dict[str, Any]) -> Optional[ContentItem]:
        article_id = str(article.get("articleId") or "").strip()
        title = str(article.get("title") or "").strip()
        if not article_id or not title:
            return None

        article_url = f"{self.base_url}/articles/{article_id}"
        detail = {}
        if self.ata_config.fetch_detail:
            try:
                await page.goto(article_url, wait_until="domcontentloaded")
                detail = self._parse_article_html(await page.content())
            except Exception as e:
                logger.warning("ATA Playwright article detail failed for %s: %s", article_url, e)

        published_at = detail.get("published_at") or datetime.now(timezone.utc)
        return ContentItem(
            id=self._generate_id(SourceType.ATA.value, "article", article_id),
            source_type=SourceType.ATA,
            title=detail.get("title") or title,
            url=article_url,
            content=detail.get("content") or detail.get("description") or title,
            author=detail.get("author"),
            published_at=published_at,
            metadata={
                "article_id": article_id,
                "category": self.ata_config.category,
                "source": "dayHotArticle",
                "auth_source": "playwright",
                "description": detail.get("description"),
            },
        )

    async def _fetch_hot_articles(self, cookie: str) -> List[dict[str, Any]]:
        url = urljoin(f"{self.base_url}/", self.ata_config.hot_path.lstrip("/"))
        try:
            response = await self.client.get(
                url,
                headers=self._headers(cookie, referer=f"{self.base_url}/articles?sort=latest"),
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("ATA hot article request failed: %s", e)
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.warning("ATA hot article response is not JSON.")
            return []

        return self._parse_hot_payload(payload)

    def _parse_hot_payload(self, payload: dict[str, Any]) -> List[dict[str, Any]]:
        raw_content = payload.get("content")
        content = raw_content if isinstance(raw_content, list) else []
        if payload.get("hasError"):
            logger.warning(
                "ATA hot article response has errors: %s",
                payload.get("errors"),
            )
            return []
        if not payload.get("success"):
            logger.warning(
                "ATA hot article response failed: success=%r errorCode=%r errorMsg=%r "
                "errorLevel=%r content=%s",
                payload.get("success"),
                payload.get("errorCode"),
                payload.get("errorMsg"),
                payload.get("errorLevel"),
                self._summarize_content(content),
            )
            if payload.get("success") is None and not content:
                logger.warning(
                    "ATA returned an empty response envelope. This usually means the Chrome "
                    "cookie is not from the ATA-authenticated profile, or the ATA login expired."
                )
            if not content:
                return []
        return [item for item in content if item.get("articleId") and item.get("title")]

    async def _article_to_item(
        self, article: dict[str, Any], cookie: str
    ) -> Optional[ContentItem]:
        article_id = str(article.get("articleId") or "").strip()
        title = str(article.get("title") or "").strip()
        if not article_id or not title:
            return None

        article_url = f"{self.base_url}/articles/{article_id}"
        detail = {}
        if self.ata_config.fetch_detail:
            detail = await self._fetch_article_detail(article_url, cookie)

        published_at = detail.get("published_at") or datetime.now(timezone.utc)
        return ContentItem(
            id=self._generate_id(SourceType.ATA.value, "article", article_id),
            source_type=SourceType.ATA,
            title=detail.get("title") or title,
            url=article_url,
            content=detail.get("content") or detail.get("description") or title,
            author=detail.get("author"),
            published_at=published_at,
            metadata={
                "article_id": article_id,
                "category": self.ata_config.category,
                "source": "dayHotArticle",
                "description": detail.get("description"),
            },
        )

    async def _fetch_article_detail(self, article_url: str, cookie: str) -> dict[str, Any]:
        try:
            response = await self.client.get(
                article_url,
                headers=self._headers(cookie, referer=f"{self.base_url}/articles?sort=latest"),
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("ATA article detail request failed for %s: %s", article_url, e)
            return {}

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "<html" not in response.text[:500].lower():
            return {}

        return self._parse_article_html(response.text)

    def _headers(self, cookie: str, referer: str) -> dict[str, str]:
        xsrf_token = os.getenv(self.ata_config.xsrf_token_env, "").strip()
        if not xsrf_token:
            xsrf_token = self._extract_cookie_value(cookie, "XSRF-TOKEN")

        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "cookie": cookie,
            "pragma": "no-cache",
            "referer": referer,
            "user-agent": USER_AGENT,
        }
        if xsrf_token:
            headers["x-xsrf-token"] = xsrf_token
        return headers

    def _get_cookie_header(self) -> str:
        headers = self._get_cookie_headers()
        return headers[0] if headers else ""

    def _get_cookie_headers(self) -> list[str]:
        cookie_source = (self.ata_config.cookie_source or "chrome").lower()
        env_cookie = os.getenv(self.ata_config.cookie_env, "").strip()
        headers: list[str] = []
        if cookie_source in ("chrome", "auto"):
            headers.extend(self._load_chrome_cookie_headers())
            if env_cookie and env_cookie not in headers:
                headers.append(env_cookie)
            return headers
        if env_cookie:
            headers.append(env_cookie)
        return headers

    def _load_chrome_cookie_header(self) -> str:
        headers = self._load_chrome_cookie_headers()
        return headers[0] if headers else ""

    def _load_chrome_cookie_headers(self) -> list[str]:
        try:
            import browser_cookie3
        except ImportError:
            logger.warning(
                "browser-cookie3 is not installed; cannot read Chrome cookies for ATA."
            )
            return []

        auth_cookies: list[str] = []
        fallback_cookies: list[str] = []
        for cookie_file in self._chrome_cookie_files():
            try:
                cookie_header = self._load_chrome_cookie_header_for_file(
                    browser_cookie3,
                    cookie_file,
                )
            except Exception as e:
                source = cookie_file or "default Chrome profile"
                logger.warning("Failed to read Chrome cookies for ATA from %s: %s", source, e)
                continue

            if self._has_auth_cookie(cookie_header):
                if cookie_file:
                    logger.info("Loaded ATA auth cookies from Chrome profile: %s", cookie_file)
                if cookie_header not in auth_cookies:
                    auth_cookies.append(cookie_header)
            elif cookie_header and cookie_header not in fallback_cookies:
                fallback_cookies.append(cookie_header)

        return auth_cookies + fallback_cookies

    def _load_chrome_cookie_header_for_file(self, browser_cookie3, cookie_file: Optional[str]) -> str:
        values_by_name: dict[str, str] = {}
        for domain in self._chrome_cookie_domains():
            cookie_jar = browser_cookie3.chrome(
                cookie_file=cookie_file,
                domain_name=domain,
            )
            for cookie in cookie_jar:
                if cookie.name and cookie.value:
                    values_by_name[cookie.name] = cookie.value
        return "; ".join(f"{name}={value}" for name, value in values_by_name.items())

    def _chrome_cookie_domains(self) -> list[str]:
        configured = self.ata_config.chrome_domain.strip()
        domains = []
        parts = configured.split(".")
        if len(parts) > 2:
            domains.append(".".join(parts[-2:]))
        domains.append(configured)
        return list(dict.fromkeys(domains))

    def _chrome_cookie_files(self) -> list[Optional[str]]:
        configured = self._chrome_cookie_file()
        if configured:
            return [configured]

        files: list[Optional[str]] = [None]
        chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        if not chrome_root.exists():
            return files

        for profile_dir in sorted(chrome_root.glob("*")):
            for cookie_file in (
                profile_dir / "Network" / "Cookies",
                profile_dir / "Cookies",
            ):
                if cookie_file.exists():
                    cookie_file_str = str(cookie_file)
                    if cookie_file_str not in files:
                        files.append(cookie_file_str)
        return files

    def _chrome_cookie_file(self) -> Optional[str]:
        if not self.ata_config.chrome_cookie_file:
            return None
        return str(Path(self.ata_config.chrome_cookie_file).expanduser())

    @staticmethod
    def _cookie_jar_to_header(cookie_jar) -> str:
        parts = []
        for cookie in cookie_jar:
            if cookie.name and cookie.value:
                parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)

    @staticmethod
    def _has_auth_cookie(cookie: str) -> bool:
        return any(
            name in cookie
            for name in (
                "ata_USER_COOKIE_V3=",
                "ata_SSO_TOKEN_V3=",
                "SSO_EMPID_HASH_V2=",
            )
        )

    @staticmethod
    def _summarize_content(content: Any) -> str:
        if isinstance(content, list):
            return f"list(len={len(content)})"
        if content is None:
            return "null"
        return type(content).__name__

    @staticmethod
    def _extract_cookie_value(cookie: str, name: str) -> str:
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _parse_article_html(self, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title = self._first_text(
            soup,
            [
                "h1",
                "[class*=title]",
                "meta[property='og:title']",
                "title",
            ],
        )
        description = self._meta_content(
            soup,
            ["meta[name='description']", "meta[property='og:description']"],
        )
        author = self._meta_content(
            soup,
            ["meta[name='author']", "meta[property='article:author']"],
        )
        published_at = self._parse_datetime(
            self._meta_content(
                soup,
                ["meta[property='article:published_time']", "meta[name='publishTime']"],
            )
            or self._attr(soup, "time[datetime]", "datetime")
        )
        content = self._extract_content(soup)

        return {
            "title": title,
            "description": description,
            "author": author,
            "published_at": published_at,
            "content": content,
        }

    def _extract_content(self, soup: BeautifulSoup) -> str:
        selectors = [
            "article",
            ".article-content",
            ".markdown-body",
            ".content",
            "#content",
            "main",
        ]
        candidates: list[str] = []
        for selector in selectors:
            for el in soup.select(selector):
                text = self._clean_text(el.get_text("\n", strip=True))
                if len(text) > 80:
                    candidates.append(text)
        if not candidates:
            body = soup.body
            if body:
                candidates.append(self._clean_text(body.get_text("\n", strip=True)))
        return max(candidates, key=len) if candidates else ""

    @staticmethod
    def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
        for selector in selectors:
            el = soup.select_one(selector)
            if not el:
                continue
            if el.name == "meta":
                value = str(el.get("content") or "").strip()
            else:
                value = el.get_text(" ", strip=True)
            if value:
                return value
        return ""

    @staticmethod
    def _meta_content(soup: BeautifulSoup, selectors: list[str]) -> str:
        for selector in selectors:
            value = ATAScraper._attr(soup, selector, "content")
            if value:
                return value
        return ""

    @staticmethod
    def _attr(soup: BeautifulSoup, selector: str, attr: str) -> str:
        el = soup.select_one(selector)
        return str(el.get(attr) or "").strip() if el else ""

    @staticmethod
    def _clean_text(text: str) -> str:
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        if not value:
            return None
        normalized = value.strip().replace("Z", "+00:00")
        if re.fullmatch(r"\d{10,13}", normalized):
            timestamp = int(normalized)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
