"""Pure-protocol OAuth device mint using SSO cookie (no browser).

Proven flow (2026-07-11):
  1. POST auth.x.ai/oauth2/device/code
  2. Cookie sso/sso-rw on .x.ai / accounts.x.ai / auth.x.ai
  3. POST auth.x.ai/oauth2/device/verify  {user_code}
  4. POST auth.x.ai/oauth2/device/approve {user_code, action=allow}
  5. POST auth.x.ai/oauth2/token device_code grant → access/refresh
Typical wall time: ~1–3s.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from curl_cffi import requests

from .oauth_device import CLIENT_ID, SCOPE, TOKEN_URL, TokenResult
from .proxyutil import normalize_proxy_for_http, resolve_proxy

LogFn = Callable[[str], None]

DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
DEVICE_VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
DEVICE_APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"

# 全局限速：并发过高会 429 / slow_down
_device_code_lock = threading.Lock()
_device_code_next_at = 0.0
_DEVICE_CODE_MIN_INTERVAL = 0.45  # seconds between device-code requests
_token_poll_lock = threading.Lock()
_token_poll_next_at = 0.0
_TOKEN_POLL_MIN_INTERVAL = 0.55


def _noop(_: str) -> None:
    return None


def _proxies(proxy: str | None) -> dict | None:
    p = normalize_proxy_for_http(resolve_proxy(proxy))
    if not p:
        return None
    return {"http": p, "https": p}


def _session_with_sso(sso: str, *, proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    token = (sso or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    if not token:
        raise ValueError("empty sso")
    for name in ("sso", "sso-rw"):
        for domain in (
            ".x.ai",
            "accounts.x.ai",
            ".accounts.x.ai",
            "auth.x.ai",
            ".auth.x.ai",
            "x.ai",
        ):
            try:
                s.cookies.set(name, token, domain=domain, path="/")
            except Exception:
                pass
    return s


def mint_tokens_with_sso(
    sso: str,
    *,
    proxy: str | None = None,
    impersonate: str = "chrome131",
    log: LogFn | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange SSO cookie for access_token + refresh_token via device grant."""
    log = log or _noop
    t0 = time.time()
    proxies = _proxies(proxy)
    s = _session_with_sso(sso, proxy=proxy)

    body = None
    global _device_code_next_at
    for dc_try in range(1, 10):
        with _device_code_lock:
            now = time.time()
            wait_gate = _device_code_next_at - now
            if wait_gate > 0:
                time.sleep(wait_gate)
            r = s.post(
                DEVICE_CODE_URL,
                data={"client_id": CLIENT_ID, "scope": SCOPE},
                impersonate=impersonate,
                proxies=proxies,
                timeout=timeout,
            )
            # 默认短间隔；遇限流则拉长
            _device_code_next_at = time.time() + _DEVICE_CODE_MIN_INTERVAL
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("device_code"):
                break
            raise RuntimeError(f"device code bad body: {(r.text or '')[:200]}")
        err = ""
        try:
            err = str((r.json() or {}).get("error") or "")
        except Exception:
            pass
        if r.status_code == 429 or err == "slow_down":
            wait = min(4.0 * dc_try, 30.0)
            log(f"device code rate-limited ({r.status_code}/{err}), sleep {wait:.1f}s")
            with _device_code_lock:
                _device_code_next_at = time.time() + wait
            time.sleep(wait)
            continue
        raise RuntimeError(f"device code HTTP {r.status_code}: {(r.text or '')[:200]}")
    if not isinstance(body, dict):
        raise RuntimeError("device code failed after retries (rate limited)")
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise RuntimeError(f"device code missing fields: {body}")
    log(f"protocol oauth device user_code={user_code}")

    headers = {
        "Origin": "https://accounts.x.ai",
        "Referer": f"https://accounts.x.ai/oauth2/device?user_code={user_code}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml,application/json",
    }
    r2 = s.post(
        DEVICE_VERIFY_URL,
        data={"user_code": user_code},
        headers=headers,
        impersonate=impersonate,
        proxies=proxies,
        timeout=timeout,
        allow_redirects=True,
    )
    log(f"protocol oauth verify http={r2.status_code} url={str(r2.url)[:80]}")

    r3 = s.post(
        DEVICE_APPROVE_URL,
        data={"user_code": user_code, "action": "allow"},
        headers={
            **headers,
            "Referer": str(r2.url or headers["Referer"]),
        },
        impersonate=impersonate,
        proxies=proxies,
        timeout=timeout,
        allow_redirects=True,
    )
    final_url = str(r3.url or "")
    log(f"protocol oauth approve http={r3.status_code} url={final_url[:100]}")
    verify_url = str(r2.url or "")
    # 未登录/无效 sso：不要空 poll token（会制造大量 slow_down 噪音）
    if "auth-error" in verify_url or "sign-in" in verify_url or r3.status_code in (401, 403):
        raise RuntimeError(
            f"approve blocked (sso invalid or not logged in): verify={verify_url[:80]} "
            f"approve_http={r3.status_code}"
        )
    if "done" not in final_url and "authorized" not in (r3.text or "").lower():
        # 仍允许 poll：有时 approve 已生效但未跳 done
        log("protocol oauth approve page not done; still polling token")

    # token exchange — serialize polls globally to avoid "Polling too fast"
    last = None
    sleep_for = 1.0
    time.sleep(0.4)
    global _token_poll_next_at
    for i in range(30):
        try:
            with _token_poll_lock:
                now = time.time()
                gap = _token_poll_next_at - now
                if gap > 0:
                    time.sleep(gap)
                rt = s.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": CLIENT_ID,
                    },
                    impersonate=impersonate,
                    proxies=proxies,
                    timeout=timeout,
                )
                _token_poll_next_at = time.time() + _TOKEN_POLL_MIN_INTERVAL
        except Exception as e:
            last = ("net", str(e))
            time.sleep(min(2.0 + i * 0.5, 10.0))
            continue
        last = (rt.status_code, (rt.text or "")[:240])
        if rt.status_code == 200:
            data = rt.json()
            access = str(data.get("access_token") or "").strip()
            refresh = str(data.get("refresh_token") or "").strip()
            if access and refresh:
                log(f"protocol oauth ok in {time.time() - t0:.2f}s")
                return {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": (str(data["id_token"]).strip() if data.get("id_token") else None),
                    "token_type": str(data.get("token_type") or "Bearer"),
                    "expires_in": int(data.get("expires_in") or 21600),
                    "user_code": user_code,
                    "raw": data,
                    "elapsed": time.time() - t0,
                    "via": "protocol",
                }
        err = ""
        try:
            err = str((rt.json() or {}).get("error") or "")
        except Exception:
            pass
        if err == "slow_down":
            sleep_for = min(max(sleep_for * 1.5, 2.0), 10.0)
            log(f"token poll slow_down, sleep {sleep_for:.1f}s")
            with _token_poll_lock:
                _token_poll_next_at = time.time() + sleep_for
            time.sleep(sleep_for)
            continue
        if err == "authorization_pending":
            time.sleep(max(sleep_for, 1.0))
            continue
        if err in ("expired_token", "access_denied"):
            break
        time.sleep(min(1.0 + i * 0.3, 5.0))
    raise RuntimeError(f"token poll failed after approve: {last}")


def mint_and_export_protocol(
    *,
    email: str,
    sso: str,
    password: str = "",
    auth_dir: str,
    proxy: str | None = None,
    base_url: str | None = None,
    headers: dict | None = None,
    probe: bool = False,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Protocol mint + write CPA auth file."""
    from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
    from .writer import write_cpa_xai_auth

    log = log or _noop
    email = (email or "").strip()
    if not email or not sso:
        return {"ok": False, "email": email, "error": "missing email/sso"}
    try:
        tokens = mint_tokens_with_sso(sso, proxy=proxy, log=log)
    except Exception as e:
        log(f"protocol mint failed: {e}")
        return {"ok": False, "email": email, "error": str(e)}

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url or DEFAULT_BASE_URL,
        headers=headers,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")
    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "elapsed": tokens.get("elapsed"),
        "via": "protocol",
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens.get("id_token"),
        "expires_in": tokens.get("expires_in"),
    }
    if probe:
        try:
            from .probe import probe_models

            pr = probe_models(tokens["access_token"], base_url=base_url or DEFAULT_BASE_URL, proxy=proxy)
            result["probe_models"] = pr
        except Exception as e:
            result["probe_error"] = str(e)
    return result
