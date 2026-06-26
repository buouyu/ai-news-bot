#!/usr/bin/env bash
set -euo pipefail

uv run python - <<'PY'
import asyncio, json
from src.mcp.service import HorizonPipelineService

async def main():
    svc = HorizonPipelineService()
    result = await svc.fetch_items(
        hours=24,
        horizon_path=".",
        config_path="data/config.json",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
PY
