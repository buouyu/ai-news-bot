from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.models import ATAConfig, SourceType
from src.scrapers.ata import ATAScraper


def _response(payload=None, text="", content_type="application/json"):
    response = MagicMock()
    response.headers = {"content-type": content_type}
    response.text = text
    response.raise_for_status.return_value = None
    if payload is not None:
        response.json.return_value = payload
    return response


def test_ata_fetches_hot_articles_and_detail(monkeypatch) -> None:
    monkeypatch.setenv("ATA_COOKIE", "XSRF-TOKEN=token-1; ata_USER_COOKIE_V3=session")

    hot_response = _response(
        {
            "success": True,
            "content": [
                {"articleId": 12020684003, "title": "Hot ATA Article"},
            ],
        }
    )
    detail_response = _response(
        text="""
        <html>
          <head>
            <meta name="description" content="Short description">
            <meta name="author" content="Alice">
            <meta property="article:published_time" content="2026-06-25T08:00:00+00:00">
          </head>
          <body>
            <article>
              <h1>Hot ATA Article Detail</h1>
              <p>This is a detailed ATA article body with enough text to be selected.</p>
              <p>It should be stored in the unified content field.</p>
            </article>
          </body>
        </html>
        """,
        content_type="text/html",
    )
    client = AsyncMock()
    client.get.side_effect = [hot_response, detail_response]

    scraper = ATAScraper(ATAConfig(enabled=True, cookie_source="env"), client)
    since = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
    items = asyncio.run(scraper.fetch(since))

    assert len(items) == 1
    item = items[0]
    assert item.id == "ata:article:12020684003"
    assert item.source_type == SourceType.ATA
    assert item.title == "Hot ATA Article Detail"
    assert str(item.url) == "https://ata.atatech.org/articles/12020684003"
    assert item.author == "Alice"
    assert item.metadata["article_id"] == "12020684003"
    assert item.metadata["source"] == "dayHotArticle"
    assert "unified content field" in item.content

    first_headers = client.get.call_args_list[0].kwargs["headers"]
    assert first_headers["x-xsrf-token"] == "token-1"


def test_ata_skips_without_cookie(monkeypatch) -> None:
    monkeypatch.delenv("ATA_COOKIE", raising=False)
    client = AsyncMock()
    scraper = ATAScraper(ATAConfig(enabled=True, cookie_source="env"), client)

    items = asyncio.run(scraper.fetch(datetime.now(timezone.utc)))

    assert items == []
    client.get.assert_not_called()


def test_ata_can_build_cookie_header_from_chrome(monkeypatch) -> None:
    captured = {}

    def fake_chrome(**kwargs):
        captured.update(kwargs)
        return [
            types.SimpleNamespace(name="ata_USER_COOKIE_V3", value="session"),
            types.SimpleNamespace(name="XSRF-TOKEN", value="token-1"),
        ]

    fake_module = types.SimpleNamespace(
        chrome=fake_chrome
    )
    monkeypatch.setitem(sys.modules, "browser_cookie3", fake_module)

    scraper = ATAScraper(
        ATAConfig(
            enabled=True,
            cookie_source="chrome",
            chrome_cookie_file="~/Library/Application Support/Google/Chrome/Profile 1/Network/Cookies",
        ),
        AsyncMock(),
    )

    assert scraper._get_cookie_header() == "ata_USER_COOKIE_V3=session; XSRF-TOKEN=token-1"
    assert captured["domain_name"] == "ata.atatech.org"
    assert captured["cookie_file"].endswith("/Google/Chrome/Profile 1/Network/Cookies")


def test_ata_scans_chrome_profiles_until_auth_cookie(monkeypatch, tmp_path) -> None:
    chrome_root = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    default_cookie = chrome_root / "Default" / "Network" / "Cookies"
    profile_cookie = chrome_root / "Profile 1" / "Cookies"
    default_cookie.parent.mkdir(parents=True)
    profile_cookie.parent.mkdir(parents=True)
    default_cookie.write_text("", encoding="utf-8")
    profile_cookie.write_text("", encoding="utf-8")

    calls = []

    def fake_chrome(cookie_file=None, domain_name=""):
        calls.append(cookie_file)
        if cookie_file and cookie_file.endswith("Profile 1/Cookies"):
            return [types.SimpleNamespace(name="ata_USER_COOKIE_V3", value="session")]
        return [types.SimpleNamespace(name="XSRF-TOKEN", value="token-1")]

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=fake_chrome))

    scraper = ATAScraper(ATAConfig(enabled=True, cookie_source="chrome"), AsyncMock())

    assert scraper._get_cookie_header() == "ata_USER_COOKIE_V3=session"
    assert calls[0] is None
    assert any(call and call.endswith("Profile 1/Cookies") for call in calls)
