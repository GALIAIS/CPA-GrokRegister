#!/usr/bin/env python3
import json
from pathlib import Path

from curl_cffi import requests

c = json.loads(Path("/opt/grok-auto-register/config.json").read_text())
base = c["shiromail_api_base"].rstrip("/")
key = c["shiromail_api_key"]
h = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
print("base", base)
print("key_prefix", key[:12] if key else None)
r = requests.get(base + "/api/v1/domains", headers=h, timeout=15, proxies={})
print("domains", r.status_code, r.text[:300])
domain_id = 1
try:
    data = r.json()
    items = data if isinstance(data, list) else data.get("data") or data.get("items") or []
    if items:
        domain_id = items[0].get("id") or items[0].get("domainId") or 1
except Exception as e:
    print("parse domains err", e)
r2 = requests.post(
    base + "/api/v1/mailboxes",
    headers={**h, "Content-Type": "application/json"},
    json={"domainId": int(domain_id), "expiresInHours": 24},
    timeout=15,
    proxies={},
)
print("create", r2.status_code, r2.text[:400])
