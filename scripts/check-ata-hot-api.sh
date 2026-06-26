#!/usr/bin/env bash
set -euo pipefail

uv run python - <<'PY'
import json

import httpx

from src.models import ATAConfig
from src.scrapers.ata import ATAScraper

scraper = ATAScraper(ATAConfig(enabled=True), httpx.AsyncClient())
cookies = scraper._get_cookie_headers()
print(f"cookie_headers={len(cookies)}")

for idx, cookie in enumerate(cookies, 1):
    headers = scraper._headers(cookie, referer="https://ata.atatech.org/articles?sort=latest")
    response = httpx.get(
        "https://ata.atatech.org/api/v1/recommend/dayHotArticle",
        headers=headers,
        follow_redirects=True,
        timeout=20,
    )
    print(f"\n[{idx}] status={response.status_code} final_url={response.url}")
    try:
        payload = response.json()
    except ValueError:
        print(response.text[:500])
        continue

    content = payload.get("content") if isinstance(payload, dict) else None
    summary = dict(payload) if isinstance(payload, dict) else {"payload_type": type(payload).__name__}
    if isinstance(content, list):
        summary["content"] = f"list(len={len(content)})"
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:1000])
PY
