"""Unit tests for DingTalk notification service."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.ai.markdown_utils import extract_summary_toc, extract_summary_title
from src.models import DingTalkConfig
from src.services.dingtalk import DingTalkNotifier

_SAMPLE_SUMMARY = """# Horizon 每日速递 - 2026-06-26

> 从 34 条内容中筛选出 2 条重要资讯。

---

1. [First item](#item-1) ⭐️ 9.0/10
2. [Second item](#item-2) ⭐️ 8.0/10

---

<a id="item-1"></a>
## First item
"""


class TestExtractSummaryParts:
    def test_extract_summary_toc(self):
        toc = extract_summary_toc(_SAMPLE_SUMMARY)
        assert toc == (
            "1. [First item](#item-1) ⭐️ 9.0/10\n"
            "2. [Second item](#item-2) ⭐️ 8.0/10"
        )

    def test_extract_summary_title(self):
        assert extract_summary_title(_SAMPLE_SUMMARY) == "Horizon 每日速递 - 2026-06-26"


class TestDingTalkNotifier:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_APP_KEY", "app-key")
        monkeypatch.setenv("DINGTALK_APP_SECRET", "app-secret")
        monkeypatch.setenv("DINGTALK_ROBOT_CODE", "robot-code")
        monkeypatch.setenv("DINGTALK_OPEN_CONVERSATION_ID", "cid-test")

    def test_send_summary_overview_posts_markdown_message(self):
        config = DingTalkConfig(enabled=True, languages=["zh"])
        notifier = DingTalkNotifier(config)

        token_response = httpx.Response(
            200,
            json={"accessToken": "token-123"},
            request=httpx.Request("POST", "https://api.dingtalk.com/v1.0/oauth2/accessToken"),
        )
        send_response = httpx.Response(
            200,
            json={"processQueryKey": "ok"},
            request=httpx.Request("POST", "https://api.dingtalk.com/v1.0/robot/groupMessages/send"),
        )

        with patch("src.services.dingtalk.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(side_effect=[token_response, send_response])
            mock_client_cls.return_value = mock_client

            asyncio.run(notifier.send_summary_overview(_SAMPLE_SUMMARY, "zh"))

        assert mock_client.post.await_count == 2
        token_call = mock_client.post.await_args_list[0]
        send_call = mock_client.post.await_args_list[1]

        assert token_call.args[0] == "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        assert token_call.kwargs["json"] == {"appKey": "app-key", "appSecret": "app-secret"}

        assert send_call.args[0] == "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        assert send_call.kwargs["headers"]["x-acs-dingtalk-access-token"] == "token-123"
        body = send_call.kwargs["json"]
        assert body["robotCode"] == "robot-code"
        assert body["openConversationId"] == "cid-test"
        assert body["msgKey"] == "sampleMarkdown"
        msg_param = json.loads(body["msgParam"])
        assert msg_param["title"] == "Horizon 每日速递 - 2026-06-26"
        assert "1. [First item](#item-1)" in msg_param["text"]

    def test_skips_when_language_not_configured(self):
        config = DingTalkConfig(enabled=True, languages=["en"])
        notifier = DingTalkNotifier(config)

        with patch("src.services.dingtalk.httpx.AsyncClient") as mock_client_cls:
            asyncio.run(notifier.send_summary_overview(_SAMPLE_SUMMARY, "zh"))
            mock_client_cls.assert_not_called()

    def test_skips_when_credentials_missing(self, monkeypatch):
        monkeypatch.delenv("DINGTALK_APP_KEY", raising=False)
        config = DingTalkConfig(enabled=True)
        notifier = DingTalkNotifier(config)

        with patch("src.services.dingtalk.httpx.AsyncClient") as mock_client_cls:
            asyncio.run(notifier.send_summary_overview(_SAMPLE_SUMMARY, "zh"))
            mock_client_cls.assert_not_called()
