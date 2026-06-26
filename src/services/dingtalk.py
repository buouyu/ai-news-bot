"""DingTalk internal-app robot notification service."""

import json
import logging
import os
from typing import Optional

import httpx
from rich.console import Console

from ..ai.markdown_utils import extract_summary_toc, extract_summary_title
from ..models import DingTalkConfig

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"


class DingTalkNotifier:
    """Send daily summary TOC to a DingTalk group via the internal-app robot API."""

    def __init__(self, config: DingTalkConfig, console: Optional[Console] = None):
        self.config = config
        self.console = console or Console()

    def _env(self, env_name: str) -> str:
        return (os.getenv(env_name) or "").strip()

    def _is_language_enabled(self, lang: str) -> bool:
        langs = self.config.languages
        if not langs:
            return True
        return lang in langs

    async def send_summary_overview(self, summary: str, lang: str) -> None:
        """Send the numbered TOC section of a generated daily summary to DingTalk."""
        if not self.config.enabled:
            return
        if not self._is_language_enabled(lang):
            return

        app_key = self._env(self.config.app_key_env)
        app_secret = self._env(self.config.app_secret_env)
        robot_code = self._env(self.config.robot_code_env)
        open_conversation_id = self._env(self.config.open_conversation_id_env)

        missing = [
            env_name
            for env_name, value in [
                (self.config.app_key_env, app_key),
                (self.config.app_secret_env, app_secret),
                (self.config.robot_code_env, robot_code),
                (self.config.open_conversation_id_env, open_conversation_id),
            ]
            if not value
        ]
        if missing:
            self.console.print(
                f"[yellow]DingTalk enabled but missing env vars: {', '.join(missing)}[/yellow]"
            )
            return

        toc = extract_summary_toc(summary)
        if not toc:
            self.console.print("[yellow]DingTalk: no TOC found in summary, skipping.[/yellow]")
            return

        title = extract_summary_title(summary) or self.config.message_title

        try:
            await self._send_group_message(
                app_key=app_key,
                app_secret=app_secret,
                robot_code=robot_code,
                open_conversation_id=open_conversation_id,
                title=title,
                text=toc,
            )
            self.console.print(f"📱 Sent {lang.upper()} summary overview to DingTalk group\n")
        except httpx.HTTPError as exc:
            self.console.print(f"[red]DingTalk notification failed: {exc}[/red]")
            logger.error("DingTalk notification failed: %s", exc)
        except RuntimeError as exc:
            self.console.print(f"[red]DingTalk notification failed: {exc}[/red]")
            logger.error("DingTalk notification failed: %s", exc)

    async def _get_access_token(
        self,
        client: httpx.AsyncClient,
        app_key: str,
        app_secret: str,
    ) -> str:
        response = await client.post(
            _TOKEN_URL,
            json={"appKey": app_key, "appSecret": app_secret},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"获取 accessToken 失败: {response.status_code} {response.text}")
        data = response.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"获取 accessToken 失败: missing accessToken in response")
        return token

    async def _send_group_message(
        self,
        *,
        app_key: str,
        app_secret: str,
        robot_code: str,
        open_conversation_id: str,
        title: str,
        text: str,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            access_token = await self._get_access_token(client, app_key, app_secret)
            body = {
                "robotCode": robot_code,
                "openConversationId": open_conversation_id,
                "msgKey": self.config.msg_key,
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
            }
            response = await client.post(
                _SEND_URL,
                json=body,
                headers={
                    "x-acs-dingtalk-access-token": access_token,
                    "Content-Type": "application/json",
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(f"发送群消息失败: {response.status_code} {response.text}")
            return response.json()
