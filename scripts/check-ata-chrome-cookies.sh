#!/usr/bin/env bash
set -euo pipefail

uv run python - <<'PY'
from pathlib import Path

import browser_cookie3

domain = "ata.atatech.org"
chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
cookie_files = [None]
if chrome_root.exists():
    for profile_dir in sorted(chrome_root.glob("*")):
        for cookie_file in (profile_dir / "Network" / "Cookies", profile_dir / "Cookies"):
            if cookie_file.exists():
                cookie_files.append(str(cookie_file))

auth_names = {"ata_USER_COOKIE_V3", "ata_SSO_TOKEN_V3", "SSO_EMPID_HASH_V2"}

for cookie_file in cookie_files:
    label = cookie_file or "default Chrome profile"
    try:
        jar = browser_cookie3.chrome(cookie_file=cookie_file, domain_name=domain)
    except Exception as exc:
        print(f"{label}: ERROR {type(exc).__name__}: {exc}")
        continue

    names = sorted({cookie.name for cookie in jar if cookie.name})
    auth_found = sorted(name for name in names if name in auth_names)
    print(f"{label}: cookies={len(names)} auth={auth_found or 'none'}")
PY
