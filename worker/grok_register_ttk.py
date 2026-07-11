#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - CLI 版本
无 GUI，仅终端运行：start / loop / cli
"""

import threading
import datetime
import time
import os
import sys
import gc
import queue
import secrets
import struct
import random
import re
import string
import json

try:
    from DrissionPage import Chromium, ChromiumOptions
    from DrissionPage.errors import PageDisconnectedError as _DrissionPageDisconnectedError
except ImportError:  # pragma: no cover
    Chromium = None  # type: ignore
    ChromiumOptions = None  # type: ignore
    _DrissionPageDisconnectedError = None  # type: ignore

from curl_cffi import requests

from cloak_browser import PageDisconnectedError as _CloakPageDisconnectedError
from cloak_browser import start_cloak_browser

# Prefer shared name used across the registration flow
PageDisconnectedError = _CloakPageDisconnectedError
if _DrissionPageDisconnectedError is not None:
    # Catch either backend disconnect
    class PageDisconnectedError(_CloakPageDisconnectedError, _DrissionPageDisconnectedError):  # type: ignore
        pass


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
MEMORY_CLEANUP_INTERVAL = 5

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "shiromail_api_base": "https://shiromail.galiais.com",
    "shiromail_api_key": "",
    "shiromail_domain": "galiais.online",
    "shiromail_expires_in_hours": 24,
    "proxy": "",
    "proxy_bypass": "localhost,127.0.0.1,galiais.com,galiais.online,shiromail.galiais.com,cpa.galiais.com",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    # 所有注册产物统一目录（账号 / sso / 邮件 / OAuth auths）
    "output_dir": "register_output",
    "cpa_export_enabled": True,
    "cpa_protocol_mint": True,
    "cpa_auth_dir": "register_output/auths",
    "cpa_proxy": "",
    "cpa_headless": True,
    "cpa_probe_after_write": False,
    "cpa_mint_timeout_sec": 240,
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
    "cpa_force_standalone": False,
    "cpa_mint_cookie_inject": True,
    "cpa_mint_browser_reuse": True,
    "cpa_mint_browser_recycle_every": 15,
    # 本地 CLIProxyAPI auths 热加载目录（可选，文件复制方式）
    "cpa_hotload_dir": "",
    "cpa_copy_to_hotload": False,
    # Management API 上传（推荐）：POST /v0/management/auth-files
    # 密钥对应 CPA config remote-management.secret-key 或环境变量 MANAGEMENT_PASSWORD
    "cpa_api_base": "http://127.0.0.1:8317",
    "cpa_api_key": "",
    "cpa_server_host": "",
    "cpa_server_user": "root",
    "cpa_server_password": "",
    "cpa_server_auth_dir": "",

    "token_only_file": "register_output/tokens.txt",
    "concurrent_count": 1,
    "browser_restart_every": 10,
    "cpa_mint_async": True,
    "browser_use_custom_ua": False,
    "browser_engine": "cloak",
    "browser_headless": True,
    "cloak_humanize": True,
    "cloak_human_preset": "default",
    "cloak_timezone": "America/New_York",
    "cloak_locale": "en-US",
    "browser_restart_mode": "soft",
    "speed_mode": True,
    "turnstile_timeout_sec": 28,
    "email_poll_interval_sec": 0.5,
    "email_resend_after_sec": 12,
    "email_resend_interval_sec": 22,
    "email_resend_max": 3,
    "parallel_email_create": True,
    "parallel_code_prefetch": True,
    "mail_pool_size": 3,
    "block_heavy_assets": True,
    "register_mode": "hybrid",
    # true: 取信超时/无 UI resend 时用协议 CreateEmail 补发（不在提交邮箱后立刻双发）
    "protocol_early_otp": True,
    "protocol_mail_fallback_sec": 8,
    # false: 直接 UI 点「完成注册」（已验证可拿 sso）；true: 先 page-fetch server-action（实验）
    "protocol_create_user": False,
    "protocol_next_action": "",
    "log_level": "info",
    "speed_log_interval_sec": 60,
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_cf_domain_lock = threading.Lock()
_io_lock = threading.Lock()
_stats_lock = threading.Lock()
_cpa_threads_lock = threading.Lock()

# ShiroMail domainId cache + pre-created mailbox pool
_shiromail_domain_cache = {"id": None, "name": "", "ts": 0.0}
_shiromail_domain_lock = threading.Lock()
_mail_pool = queue.Queue(maxsize=16)
_mail_pool_lock = threading.Lock()
_mail_pool_stop = threading.Event()
_mail_pool_thread = None

# Hybrid protocol capture (castle / next-action from live browser)
_protocol_ctx = {
    "castle": "",
    "next_action": "",
    "router_tree": "",
    "capture_installed": False,
    "create_email_seen": 0,
}
_protocol_ctx_lock = threading.Lock()

_LOG_LEVEL_RANK = {
    "quiet": 10,
    "info": 20,
    "debug": 30,
}


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


def get_log_level():
    raw = str(config.get("log_level", "info") or "info").strip().lower()
    return raw if raw in _LOG_LEVEL_RANK else "info"


def message_log_rank(message):
    """根据消息内容推断日志级别。"""
    text = str(message or "")
    if "[Debug]" in text:
        return _LOG_LEVEL_RANK["debug"]
    # quiet 仅保留关键进度/结果/警告
    if text.startswith("--- "):
        return _LOG_LEVEL_RANK["info"]
    quiet_prefixes = ("[+]", "[-]", "[!]")
    if text.lstrip().startswith(quiet_prefixes) or any(
        f" {p}" in text[:12] for p in quiet_prefixes
    ):
        return _LOG_LEVEL_RANK["quiet"]
    if "[*] 速度统计" in text or text.lstrip().startswith("[*] 速度统计"):
        return _LOG_LEVEL_RANK["quiet"]
    if any(
        key in text
        for key in (
            "[*] 1.",
            "[*] 2.",
            "[*] 3.",
            "[*] 4.",
            "[*] 5.",
            "[*] 6.",
            "[*] 终端模式",
            "[*] 配置已保存",
            "[*] 任务结束",
            "[*] 注册成功",
            "[+] 注册成功",
            "Worker-",
            "浏览器已启动",
            "开始执行",
            "成功账号将实时保存",
            "按 Ctrl+C",
            "Cloudflare 拦截",
        )
    ):
        return _LOG_LEVEL_RANK["quiet"]
    return _LOG_LEVEL_RANK["info"]


def should_emit_log(message, level=None):
    configured = _LOG_LEVEL_RANK[get_log_level()]
    if level is not None:
        msg_rank = _LOG_LEVEL_RANK.get(str(level).lower(), _LOG_LEVEL_RANK["info"])
    else:
        msg_rank = message_log_rank(message)
    return msg_rank <= configured


def emit_log(log_callback, message, *, level=None):
    if not log_callback:
        return
    if not should_emit_log(message, level=level):
        return
    log_callback(message)


class RateMeter:
    """按固定间隔汇总创建速度（全局一条，避免每 worker 各打一条）。"""

    def __init__(self, interval_sec=60):
        # 允许测试用更短间隔；生产默认 60s
        self.interval_sec = max(float(interval_sec or 60), 1.0)
        self.t0 = time.time()
        self.last_tick = self.t0
        self.last_success = 0
        self._lock = threading.Lock()

    def format_line(self, success, fail=0, force=False):
        now = time.time()
        with self._lock:
            elapsed = now - self.last_tick
            if not force and elapsed < self.interval_sec:
                return None
            success = int(success or 0)
            fail = int(fail or 0)
            delta = max(success - self.last_success, 0)
            # 正常按实际窗口折算；极短窗口（force 收尾/刚启动）用 interval 估，避免天文数字
            if elapsed >= 1.0:
                window = elapsed
            else:
                window = self.interval_sec
            rate = delta * 60.0 / window
            total_sec = max(now - self.t0, 0.0)
            total_min = total_sec / 60.0
            # 运行不足 1s 时平均速度与窗口速率对齐，避免 540/min 这类瞬时噪声
            if total_sec >= 1.0:
                avg = success * 60.0 / total_sec
            else:
                avg = rate
            self.last_tick = now
            self.last_success = success
            return (
                f"[*] 速度统计: 成功 {rate:.0f}/min | 本分钟成功 {delta} "
                f"| 累计成功 {success} | 累计失败 {fail} | 运行 {total_min:.1f}min | 平均 {avg:.1f}/min"
            )

    def maybe_log(self, log_callback, success, fail=0, force=False):
        line = self.format_line(success, fail=fail, force=force)
        if line:
            emit_log(log_callback, line, level="quiet")


def start_speed_logger(get_counts, log_callback, stop_event, interval_sec=60):
    """后台每 interval 打印一次全局速度；stop 后打印最终摘要。"""

    meter = RateMeter(interval_sec=interval_sec)

    def _loop():
        while True:
            if stop_event.wait(timeout=meter.interval_sec):
                break
            try:
                success, fail = get_counts()
            except Exception:
                success, fail = 0, 0
            meter.maybe_log(log_callback, success, fail, force=True)
        try:
            success, fail = get_counts()
        except Exception:
            success, fail = 0, 0
        meter.maybe_log(log_callback, success, fail, force=True)

    thread = threading.Thread(target=_loop, name="speed-logger", daemon=True)
    thread.start()
    return thread, meter


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    # Only Windows has the LOCALAPPDATA multi-install layout we re-exec into.
    if os.name != "nt":
        return
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def _proxy_bypass_hosts():
    """Hosts that must NOT use config.proxy (local services / hairpin)."""
    raw = str(config.get("proxy_bypass") or "").strip()
    defaults = (
        "localhost",
        "127.0.0.1",
        "::1",
        "galiais.com",
        "galiais.online",
        "shiromail.galiais.com",
        "cpa.galiais.com",
    )
    hosts = {h.strip().lower() for h in defaults if h.strip()}
    if raw:
        for part in re.split(r"[\s,;]+", raw):
            p = part.strip().lower()
            if p:
                hosts.add(p)
    # also derive from known service bases
    for key in (
        "shiromail_api_base",
        "cpa_api_base",
        "cloudflare_api_base",
        "grok2api_remote_base",
    ):
        base = str(config.get(key) or "").strip()
        if not base:
            continue
        try:
            from urllib.parse import urlparse

            h = (urlparse(base).hostname or "").lower()
            if h:
                hosts.add(h)
        except Exception:
            pass
    return hosts


def _host_matches_bypass(host: str, bypass) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for b in bypass:
        b = (b or "").lower().strip(".")
        if not b:
            continue
        if host == b or host.endswith("." + b):
            return True
    return False


def _normalize_proxy_for_http(proxy: str) -> str:
    """Prefer socks5h (remote DNS) for HTTP clients on Linux WARP/MicroWARP."""
    p = str(proxy or "").strip()
    if not p:
        return ""
    low = p.lower()
    if low.startswith("socks5://") and not low.startswith("socks5h://"):
        return "socks5h://" + p[len("socks5://") :]
    return p


def get_proxies(url=None):
    """Return request proxies. Local/mail/CPA hosts bypass WARP SOCKS."""
    proxy = str(config.get("proxy", "") or "").strip()
    if not proxy:
        return {}
    if url:
        try:
            from urllib.parse import urlparse

            host = (urlparse(str(url)).hostname or "").lower()
            if _host_matches_bypass(host, _proxy_bypass_hosts()):
                return {}
        except Exception:
            pass
    proxy = _normalize_proxy_for_http(proxy)
    return {"http": proxy, "https": proxy}


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    with _cf_domain_lock:
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
        return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("items"), list):
            return data.get("items")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("domains"), list):
            return data.get("domains")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("mailboxes"), list):
            return data.get("mailboxes")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
            if isinstance(nested.get("items"), list):
                return nested.get("items")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    ua = str(config.get("user_agent") or "").strip()
    if ua:
        return ua
    if os.name == "posix":
        return (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return os.path.join(os.path.dirname(__file__), "token.json")


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    parent_dir = os.path.dirname(token_file)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with _io_lock:
        data = {}
        if os.path.exists(token_file):
            try:
                with open(token_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        pool = data.get(pool_name)
        if not isinstance(pool, list):
            pool = []
        existing = set()
        for item in pool:
            if isinstance(item, str):
                existing.add(_normalize_sso_token(item))
            elif isinstance(item, dict):
                existing.add(_normalize_sso_token(item.get("token", "")))
        if token in existing:
            if log_callback:
                log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
            return True
        entry = {"token": token, "tags": ["auto-register"], "note": email}
        pool.append(entry)
        data[pool_name] = pool
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def get_grok2api_remote_api_bases(base):
    """生成 grok2api 管理 API 候选根路径。

    参数:
      - base str: 用户配置的 grok2api 远端地址

    返回:
      - list[str]: 依次尝试的管理 API 根路径
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return []
    lower = normalized.lower()
    candidates = [normalized]
    if lower.endswith("/admin/api"):
        return candidates
    if lower.endswith("/admin"):
        candidates.append(f"{normalized}/api")
    else:
        candidates.append(f"{normalized}/admin/api")
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    api_bases = get_grok2api_remote_api_bases(base)
    add_errors = []
    # 优先使用 add 接口，避免全量覆盖远端池
    add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
    for api_base in api_bases:
        endpoint = f"{api_base}/tokens/add"
        try:
            resp_add = http_post(
                endpoint,
                headers=headers,
                params=query,
                json=add_payload,
                timeout=30,
                proxies={},
            )
            resp_add.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({endpoint})")
            return True
        except Exception as add_exc:
            add_errors.append(f"{endpoint}: {add_exc}")
    if log_callback:
        log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {'; '.join(add_errors)}")

    # 兜底：旧版全量保存接口
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20, proxies={})
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    save_errors = []
    save_bases = []
    for item in [fallback_base, *(api_bases or [base])]:
        if item and item not in save_bases:
            save_bases.append(item)
    for api_base in save_bases:
        try:
            resp2 = http_post(f"{api_base}/tokens", headers=headers, params=query, json=current, timeout=30, proxies={})
            resp2.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({api_base}/tokens)")
            return True
        except Exception as save_exc:
            save_errors.append(f"{api_base}/tokens: {save_exc}")
    raise RuntimeError(f"grok2api 远端 /tokens 全量模式写入失败: {'; '.join(save_errors)}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def add_token_to_token_only_file(raw_token, log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    try:
        ok = _upsert_token_line(token)
        if ok and log_callback:
            log_callback(f"[+] 已写入 token 文件: {get_tokens_file_path()}")
        return bool(ok)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 写入 token 文件失败: {exc}")
        return False


def upload_to_cpa_server(local_path, log_callback=None):
    host = str(config.get("cpa_server_host", "") or "").strip()
    user = str(config.get("cpa_server_user", "root") or "root").strip()
    password = str(config.get("cpa_server_password", "") or "").strip()
    remote_dir = str(config.get("cpa_server_auth_dir", "") or "").strip()
    if not host or not remote_dir:
        return False
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=15)
        sftp = ssh.open_sftp()
        filename = os.path.basename(local_path)
        remote_path = remote_dir.rstrip("/") + "/" + filename
        sftp.put(local_path, remote_path)
        try:
            sftp.chmod(remote_path, 0o600)
        except Exception:
            pass
        sftp.close()
        ssh.close()
        if log_callback:
            log_callback(f"[cpa] 已上传到服务器: {host}:{remote_path}")
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[cpa] 上传到服务器失败: {exc}")
        return False


def export_cpa_xai_for_account(email, password, sso=None, log_callback=None, page=None):
    if not config.get("cpa_export_enabled", True):
        if log_callback:
            log_callback("[cpa] CPA 导出已禁用，跳过")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    try:
        from cpa_export import export_cpa_xai_for_account as _export

        cfg = dict(config)
        cfg["cpa_auth_dir"] = get_cpa_auth_dir()
        return _export(
            email,
            password,
            sso=sso,
            page=page,
            config=cfg,
            log_callback=log_callback,
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[cpa] CPA xAI 导出失败: {exc}")
        return {"ok": False, "error": str(exc)}


def get_browser_engine():
    raw = str(config.get("browser_engine", "cloak") or "cloak").strip().lower()
    if raw in ("cloak", "cloakbrowser", "stealth"):
        return "cloak"
    if raw in ("drission", "drissionpage", "chrome", "chromium", "local"):
        return "drission"
    return "cloak"


def is_speed_mode():
    return bool(config.get("speed_mode", True))


def get_project_root():
    return os.path.dirname(os.path.abspath(__file__))


def get_output_dir():
    """统一注册产物目录：accounts / tokens / mail / OAuth json。"""
    raw = str(config.get("output_dir") or "register_output").strip() or "register_output"
    if os.path.isabs(raw):
        path = raw
    else:
        path = os.path.join(get_project_root(), raw)
    os.makedirs(path, exist_ok=True)
    for sub in ("auths", "full"):
        try:
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        except Exception:
            pass
    return path


def get_accounts_output_path(now=None):
    """固定输出到 register_output/accounts_all.txt（不再按批次拆文件）。"""
    # now 参数保留兼容旧调用，忽略
    return os.path.join(get_output_dir(), "accounts_all.txt")


def get_tokens_file_path():
    token_only_file = str(config.get("token_only_file", "") or "").strip()
    if token_only_file:
        if not os.path.isabs(token_only_file):
            token_only_file = os.path.join(get_project_root(), token_only_file)
        parent = os.path.dirname(token_only_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return token_only_file
    return os.path.join(get_output_dir(), "tokens.txt")


def get_mail_credentials_path():
    return os.path.join(get_output_dir(), "mail_credentials.txt")


def get_cpa_auth_dir():
    raw = str(config.get("cpa_auth_dir") or "").strip()
    if not raw:
        return os.path.join(get_output_dir(), "auths")
    if os.path.isabs(raw):
        path = raw
    else:
        path = os.path.join(get_project_root(), raw)
    os.makedirs(path, exist_ok=True)
    return path


def get_cpa_hotload_dir():
    """CLIProxyAPI 监视的 auths 目录（本地热加载）。"""
    raw = str(config.get("cpa_hotload_dir") or "").strip()
    if not raw:
        # 常见路径：Linux 本机部署 / 相邻仓库
        candidates = [
            "/root/CLIProxyAPI/auths",
            os.path.normpath(
                os.path.join(get_project_root(), "..", "CLIProxyAPI", "auths")
            ),
        ]
        for cand in candidates:
            parent = os.path.dirname(cand)
            if os.path.isdir(parent) or os.path.isdir(cand):
                raw = cand
                break
        if not raw:
            return ""
    if not os.path.isabs(raw):
        raw = os.path.join(get_project_root(), raw)
    try:
        os.makedirs(raw, exist_ok=True)
    except Exception:
        return ""
    return raw


def upload_auth_via_cpa_api(auth_path, log_callback=None):
    """通过 CLIProxyAPI Management API 上传 auth 文件。

    POST {cpa_api_base}/v0/management/auth-files?name=<file.json>
    Headers:
      Authorization: Bearer <cpa_api_key>
      或 X-Management-Key: <cpa_api_key>
    Body: raw JSON file content
    """
    auth_path = str(auth_path or "").strip()
    base = str(config.get("cpa_api_base") or "").strip().rstrip("/")
    key = str(
        config.get("cpa_api_key")
        or config.get("cpa_management_key")
        or config.get("cpa_api_password")
        or ""
    ).strip()
    if not base:
        return {"ok": False, "skipped": True, "reason": "cpa_api_base empty"}
    if not key:
        return {"ok": False, "skipped": True, "reason": "cpa_api_key empty"}
    if not auth_path or not os.path.isfile(auth_path):
        return {"ok": False, "error": "auth file missing"}

    name = os.path.basename(auth_path)
    if not name.lower().endswith(".json"):
        return {"ok": False, "error": "name must end with .json"}
    try:
        with open(auth_path, "rb") as f:
            data = f.read()
    except Exception as exc:
        return {"ok": False, "error": f"read file: {exc}"}

    url = f"{base}/v0/management/auth-files?name={name}"
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        # 优先 curl_cffi（与项目其它请求一致）
        try:
            r = requests.post(url, headers=headers, data=data, timeout=30, proxies={})
            status = getattr(r, "status_code", 0)
            text = getattr(r, "text", "") or ""
        except Exception:
            import urllib.request

            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = getattr(resp, "status", 200) or 200
                text = resp.read().decode("utf-8", errors="replace")
        if 200 <= int(status) < 300:
            if log_callback:
                log_callback(f"[cpa] API 上传成功: {base} <- {name} http={status}")
            return {"ok": True, "http": status, "name": name, "body": text[:300]}
        if log_callback:
            log_callback(
                f"[cpa] API 上传失败: http={status} body={(text or '')[:200]}"
            )
        return {"ok": False, "http": status, "error": (text or "")[:300], "name": name}
    except Exception as exc:
        if log_callback:
            log_callback(f"[cpa] API 上传异常: {exc}")
        return {"ok": False, "error": str(exc)}


def publish_auth_to_cpa(auth_path, log_callback=None):
    """把 xai-*.json 发布到 CLIProxyAPI。

    优先级：
      1) Management API（cpa_api_base + cpa_api_key）
      2) 本地 hotload 目录复制（cpa_hotload_dir）
      3) 远程 SCP（cpa_server_host）
    """
    auth_path = str(auth_path or "").strip()
    if not auth_path or not os.path.isfile(auth_path):
        return {"ok": False, "error": "auth file missing"}
    result = {"ok": False, "src": auth_path}

    # 1) Management API
    api_res = upload_auth_via_cpa_api(auth_path, log_callback=log_callback)
    if not api_res.get("skipped"):
        result["api"] = api_res
        if api_res.get("ok"):
            result["ok"] = True
            result["via"] = "api"

    # 2) 本地 hotload 复制（API 失败时作为兜底；也可同时启用）
    want_hotload = bool(config.get("cpa_copy_to_hotload", True))
    force_hotload = bool(config.get("cpa_hotload_always", False))
    if want_hotload and (force_hotload or not result.get("ok")):
        hot = get_cpa_hotload_dir()
        if hot:
            try:
                import shutil

                dst = os.path.join(hot, os.path.basename(auth_path))
                if os.path.normcase(os.path.abspath(auth_path)) != os.path.normcase(
                    os.path.abspath(dst)
                ):
                    shutil.copy2(auth_path, dst)
                    try:
                        os.chmod(dst, 0o600)
                    except Exception:
                        pass
                result["hotload_path"] = dst
                result["ok"] = True
                result.setdefault("via", "hotload")
                if log_callback:
                    log_callback(f"[cpa] 已同步到 CLIProxyAPI 目录: {dst}")
            except Exception as exc:
                result["hotload_error"] = str(exc)
                if log_callback:
                    log_callback(f"[cpa] 同步 CLIProxyAPI 目录失败: {exc}")
        elif log_callback and not result.get("ok"):
            log_callback("[cpa] 未配置 cpa_hotload_dir，且 API 未成功")

    # 3) 远程 SCP（可选）
    if str(config.get("cpa_server_host") or "").strip():
        try:
            if upload_to_cpa_server(auth_path, log_callback=log_callback):
                result["remote"] = True
                result["ok"] = True
                result.setdefault("via", "sftp")
        except Exception as exc:
            result["remote_error"] = str(exc)
            if log_callback:
                log_callback(f"[cpa] 远程上传失败: {exc}")
    return result


def get_full_account_path(email):
    safe = re.sub(r"[^a-zA-Z0-9@._-]+", "-", str(email or "").strip()) or "unknown"
    return os.path.join(get_output_dir(), "full", f"{safe}.json")


def append_mail_credential(email, dev_token):
    """追加到 register_output/mail_credentials.txt（按行去重）。"""
    path = get_mail_credentials_path()
    line = f"{email}\t{dev_token}"
    with _io_lock:
        existing = set()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    existing = {x.strip() for x in f if x.strip()}
            except Exception:
                existing = set()
        if line in existing:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _upsert_accounts_all_line(email, password, sso, accounts_output_file=None):
    """写入/更新 accounts_all.txt：同 email 覆盖，保持一行一账号。"""
    path = accounts_output_file or get_accounts_output_path()
    email = str(email or "").strip()
    password = str(password or "")
    sso = _normalize_sso_token(sso)
    new_line = f"{email}----{password}----{sso}"
    with _io_lock:
        lines = []
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = [x.rstrip("\n") for x in f if x.strip()]
            except Exception:
                lines = []
        out = []
        replaced = False
        key = email.lower()
        for line in lines:
            parts = line.split("----")
            if parts and parts[0].strip().lower() == key:
                out.append(new_line)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(new_line)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))


def _upsert_token_line(sso):
    """tokens.txt 去重追加。"""
    token = _normalize_sso_token(sso)
    if not token:
        return False
    path = get_tokens_file_path()
    with _io_lock:
        existing = set()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    existing = {x.strip() for x in f if x.strip()}
            except Exception:
                existing = set()
        if token in existing:
            return True
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(token + "\n")
    return True


def save_account_bundle(
    *,
    email,
    password,
    sso,
    accounts_output_file=None,
    log_callback=None,
    cpa_result=None,
    profile=None,
    write_accounts_line=True,
    write_sso_token=True,
):
    """按当前 register_output 格式落盘：
    - accounts_all.txt
    - tokens.txt
    - full/{email}.json
    - auths/xai-*.json（由 mint 写入，这里读取合并）
    """
    email = str(email or "").strip()
    password = str(password or "")
    sso = _normalize_sso_token(sso)
    if write_accounts_line:
        try:
            _upsert_accounts_all_line(
                email, password, sso, accounts_output_file or get_accounts_output_path()
            )
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 保存 accounts_all 失败: {exc}")

    if write_sso_token:
        try:
            ok = _upsert_token_line(sso)
            if ok and log_callback:
                log_callback(f"[+] 已写入 token 文件: {get_tokens_file_path()}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 token 文件失败: {exc}")

    record = {
        "email": email,
        "password": password,
        "sso": sso,
        "given_name": (profile or {}).get("given_name"),
        "family_name": (profile or {}).get("family_name"),
        "created_via": (profile or {}).get("created_via"),
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    cpa_result = cpa_result or {}
    auth_path = str(cpa_result.get("path") or "").strip()
    if not auth_path:
        # 默认 auth 路径约定
        cand = os.path.join(get_cpa_auth_dir(), f"xai-{email}.json")
        if os.path.isfile(cand):
            auth_path = cand
    if auth_path:
        record["auth_path"] = auth_path
    if auth_path and os.path.isfile(auth_path):
        try:
            with open(auth_path, "r", encoding="utf-8") as f:
                auth = json.load(f)
            for k in (
                "access_token",
                "refresh_token",
                "id_token",
                "expires_in",
                "expired",
                "token_type",
                "base_url",
                "sub",
            ):
                if auth.get(k) is not None:
                    record[k] = auth.get(k)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 读取 auth json 失败: {exc}")
    # mint 结果里可能直接带 token
    for k in ("access_token", "refresh_token", "id_token", "expires_in"):
        if cpa_result.get(k) is not None and not record.get(k):
            record[k] = cpa_result.get(k)

    full_path = get_full_account_path(email)
    try:
        # 合并旧 full，避免 mint 后覆盖掉 password/sso
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    merged = dict(old)
                    merged.update({k: v for k, v in record.items() if v is not None and v != ""})
                    record = merged
            except Exception:
                pass
        with _io_lock:
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
                f.write("\n")
        if log_callback:
            log_callback(f"[+] 完整账号已保存: {full_path}")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 保存完整账号 JSON 失败: {exc}")

    # 有 access/refresh 时自动同步到 CLIProxyAPI auths
    if auth_path and os.path.isfile(auth_path) and (
        record.get("access_token") and record.get("refresh_token")
    ):
        try:
            pub = publish_auth_to_cpa(auth_path, log_callback=log_callback)
            if pub.get("hotload_path"):
                record["cpa_hotload_path"] = pub["hotload_path"]
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 发布到 CPA 失败: {exc}")
    return record


def get_turnstile_timeout():
    try:
        return max(int(config.get("turnstile_timeout_sec", 28) or 28), 12)
    except Exception:
        return 28


def get_email_poll_interval():
    try:
        base = float(config.get("email_poll_interval_sec", 0.5) or 0.5)
        # speed_mode 允许更激进轮询
        floor = 0.35 if is_speed_mode() else 0.6
        return max(base, floor)
    except Exception:
        return 0.5


def get_email_resend_after_sec():
    try:
        # 与协议补发窗口对齐，默认 8s 起（可被 config 覆盖）
        base = float(
            config.get(
                "email_resend_after_sec",
                config.get("protocol_mail_fallback_sec", 8),
            )
            or 8
        )
        return max(base, 6.0 if is_speed_mode() else 10.0)
    except Exception:
        return 8.0


def get_email_resend_interval_sec():
    try:
        base = float(config.get("email_resend_interval_sec", 22) or 22)
        return max(base, 16.0 if is_speed_mode() else 28.0)
    except Exception:
        return 22.0


def get_email_resend_max():
    try:
        return max(int(config.get("email_resend_max", 3) or 3), 0)
    except Exception:
        return 3


def get_mail_pool_size():
    try:
        return max(int(config.get("mail_pool_size", 3) or 0), 0)
    except Exception:
        return 3


def get_browser_restart_mode():
    raw = str(config.get("browser_restart_mode", "soft") or "soft").strip().lower()
    return raw if raw in ("soft", "full") else "soft"


def get_register_mode():
    raw = str(config.get("register_mode", "hybrid") or "hybrid").strip().lower()
    if raw in ("browser", "ui"):
        return "browser"
    # hybrid / protocol / proto all map to hybrid pipeline
    return "hybrid"


def is_hybrid_mode():
    return get_register_mode() == "hybrid"


def pace_sleep(seconds, cancel_callback=None, *, speed_factor=0.35):
    """Sleep helper: shortens delays when speed_mode is on."""
    sec = float(seconds or 0)
    if is_speed_mode():
        sec = max(sec * float(speed_factor), 0.04 if sec > 0 else 0)
    sleep_with_cancel(sec, cancel_callback)


def reset_protocol_ctx():
    with _protocol_ctx_lock:
        _protocol_ctx["castle"] = ""
        # keep last next_action as fallback across accounts (same build)
        _protocol_ctx["capture_installed"] = False
        _protocol_ctx["create_email_seen"] = 0


def get_protocol_castle():
    with _protocol_ctx_lock:
        return str(_protocol_ctx.get("castle") or "")


def get_protocol_next_action():
    with _protocol_ctx_lock:
        configured = str(config.get("protocol_next_action") or "").strip()
        return configured or str(_protocol_ctx.get("next_action") or "")


def _parse_castle_from_create_email_body(raw: bytes) -> str:
    """Extract field3 string (castleRequestToken) from CreateEmailValidationCode frame."""
    if not raw or len(raw) < 6:
        return ""
    try:
        if raw[0] == 0 and len(raw) >= 5:
            ln = struct.unpack(">I", raw[1:5])[0]
            payload = raw[5 : 5 + ln]
        else:
            payload = raw

        def _varint(buf, i):
            result = 0
            shift = 0
            while i < len(buf):
                b = buf[i]
                i += 1
                result |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return result, i
                shift += 7
            raise ValueError("trunc")

        i = 0
        while i < len(payload):
            key, i = _varint(payload, i)
            field, wt = key >> 3, key & 7
            if wt == 2:
                n, i = _varint(payload, i)
                data = payload[i : i + n]
                i += n
                if field == 3:
                    return data.decode("utf-8", errors="ignore")
            elif wt == 0:
                _, i = _varint(payload, i)
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
    except Exception:
        return ""
    return ""


def install_protocol_capture(log_callback=None):
    """Capture castle / next-action via request events (does NOT intercept routes).

    Using page.route + continue_ previously could drop CreateEmail when continue
    failed silently, resulting in zero verification emails.
    """
    if not is_hybrid_mode():
        return
    page = _get_page()
    if page is None or not hasattr(page, "_pw"):
        return
    with _protocol_ctx_lock:
        if _protocol_ctx.get("capture_installed"):
            return
        _protocol_ctx["capture_installed"] = True
        _protocol_ctx["create_email_seen"] = 0

    pw = page._pw

    def on_request(req):
        try:
            if str(getattr(req, "method", "") or "").upper() != "POST":
                return
            url = str(getattr(req, "url", "") or "")
            if "accounts.x.ai" not in url:
                return
            headers = {str(k).lower(): str(v) for k, v in dict(req.headers).items()}
            if headers.get("next-action"):
                with _protocol_ctx_lock:
                    _protocol_ctx["next_action"] = headers["next-action"]
                if headers.get("next-router-state-tree"):
                    with _protocol_ctx_lock:
                        _protocol_ctx["router_tree"] = headers["next-router-state-tree"]
            if "CreateEmailValidationCode" in url:
                with _protocol_ctx_lock:
                    _protocol_ctx["create_email_seen"] = int(
                        _protocol_ctx.get("create_email_seen") or 0
                    ) + 1
                raw = b""
                try:
                    raw = req.post_data_buffer or b""
                except Exception:
                    pd = getattr(req, "post_data", None)
                    if pd:
                        raw = (
                            pd
                            if isinstance(pd, (bytes, bytearray))
                            else str(pd).encode("utf-8", errors="surrogateescape")
                        )
                castle = _parse_castle_from_create_email_body(raw)
                if castle:
                    with _protocol_ctx_lock:
                        _protocol_ctx["castle"] = castle
                if log_callback:
                    log_callback(
                        f"[*] 已捕获浏览器 CreateEmailValidationCode"
                        f" (castle={'yes' if castle else 'no'})"
                    )
        except Exception:
            pass

    try:
        pw.on("request", on_request)
        if log_callback:
            log_callback("[*] hybrid 协议捕获已安装 (request 监听, 不拦截)")
    except Exception as e:
        with _protocol_ctx_lock:
            _protocol_ctx["capture_installed"] = False
        if log_callback:
            log_callback(f"[Debug] 协议捕获安装失败: {e}")


def get_create_email_seen_count():
    with _protocol_ctx_lock:
        return int(_protocol_ctx.get("create_email_seen") or 0)


def protocol_send_otp(email, log_callback=None, castle=""):
    """Pure-protocol CreateEmailValidationCode (proven ~grpc-status:0)."""
    try:
        from protocol_xai import ProtocolXaiClient

        client = ProtocolXaiClient(
            proxy=str(config.get("proxy") or "").strip() or None,
            log=log_callback or (lambda m: None),
        )
        castle = castle or get_protocol_castle()
        r = client.create_email_validation_code(email, castle_request_token=castle)
        if log_callback:
            log_callback(
                f"[*] 协议发信 CreateEmailValidationCode ok={r.get('ok')} "
                f"grpc={r.get('grpc_status')}"
            )
        return r
    except Exception as e:
        if log_callback:
            log_callback(f"[!] 协议发信失败: {e}")
        return {"ok": False, "error": str(e)}


def protocol_verify_otp(email, code, log_callback=None):
    """Pure-protocol VerifyEmailValidationCode (code without dashes)."""
    try:
        from protocol_xai import ProtocolXaiClient

        client = ProtocolXaiClient(
            proxy=str(config.get("proxy") or "").strip() or None,
            log=log_callback or (lambda m: None),
        )
        r = client.verify_email_validation_code(email, code)
        if log_callback:
            log_callback(
                f"[*] 协议验码 VerifyEmailValidationCode ok={r.get('ok')} "
                f"grpc={r.get('grpc_status')} code={ProtocolXaiClient.normalize_email_code(code)}"
            )
        return r
    except Exception as e:
        if log_callback:
            log_callback(f"[!] 协议验码失败: {e}")
        return {"ok": False, "error": str(e)}


def protocol_create_user_via_action(
    email,
    code,
    given_name,
    family_name,
    password,
    turnstile_token,
    log_callback=None,
):
    """Create user via Next.js server-action from inside the page (same-origin fetch).

    External playwright/curl posts often get 403 or HTML without sso and may burn
    the one-shot Turnstile token. Page-side fetch keeps the tab's cookies + TLS.
    """
    from protocol_xai import ProtocolXaiClient

    castle = get_protocol_castle()
    action = get_protocol_next_action() or "7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259"
    tree = ""
    with _protocol_ctx_lock:
        tree = str(_protocol_ctx.get("router_tree") or "")

    payload = [
        {
            "emailValidationCode": ProtocolXaiClient.normalize_email_code(code),
            "createUserAndSessionRequest": {
                "email": email,
                "givenName": given_name,
                "familyName": family_name,
                "clearTextPassword": password,
                "tosAcceptedVersion": 1,
            },
            "turnstileToken": turnstile_token or "",
            "conversionId": str(__import__("uuid").uuid4()),
            "castleRequestToken": castle or "",
        },
        {"client": "$T", "meta": "$undefined", "mutationKey": "$undefined"},
    ]
    body = json.dumps(payload, separators=(",", ":"))
    page = _get_page()
    if page is None:
        return {"ok": False, "sso": "", "error": "no-page"}

    try:
        result = page.run_js(
            """
const body = arguments[0];
const action = arguments[1];
const tree = arguments[2] || '';
async function go() {
  const headers = {
    'content-type': 'text/plain;charset=UTF-8',
    'accept': 'text/x-component',
    'next-action': action,
  };
  if (tree) headers['next-router-state-tree'] = tree;
  const url = (location.pathname + location.search) || '/sign-up?redirect=grok-com';
  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body,
    credentials: 'include',
  });
  const text = await resp.text();
  const m = document.cookie.match(/(?:^|;\\s*)sso=([^;]+)/);
  const sso = m ? decodeURIComponent(m[1]) : '';
  return { status: resp.status, len: text.length, sso: sso, head: text.slice(0, 160) };
}
return go();
            """,
            body,
            action,
            tree,
        )
        status = 0
        sso = ""
        if isinstance(result, dict):
            status = int(result.get("status") or 0)
            sso = str(result.get("sso") or "").strip()
            if log_callback:
                log_callback(
                    f"[*] 协议建号 server-action(page-fetch) http={status} "
                    f"action={action[:12]}… sso={'yes' if sso else 'no'} "
                    f"len={result.get('len')}"
                )
        if not sso:
            sso = read_sso_from_browser()
        if sso:
            return {"ok": True, "sso": sso, "http_status": status, "via": "page-fetch"}
        return {
            "ok": False,
            "sso": "",
            "http_status": status,
            "via": "page-fetch",
            "head": (result or {}).get("head") if isinstance(result, dict) else "",
        }
    except Exception as e:
        if log_callback:
            log_callback(f"[!] page-fetch server-action 失败: {e}")
        return {"ok": False, "sso": "", "error": str(e), "via": "page-fetch"}


def read_sso_from_browser():
    page = _get_page()
    if page is None:
        return ""
    # cookies API first (survives navigation better)
    try:
        if hasattr(page, "_pw"):
            for c in page._pw.context.cookies():
                if c.get("name") == "sso" and c.get("value"):
                    return str(c["value"])
    except Exception:
        pass
    try:
        cookies = page.cookies(all_domains=True, all_info=True) or []
        for item in cookies:
            if isinstance(item, dict) and item.get("name") == "sso" and item.get("value"):
                return str(item["value"])
    except Exception:
        pass
    try:
        return str(
            page.run_js(
                "const m=document.cookie.match(/(?:^|;\\s*)sso=([^;]+)/);"
                "return m?decodeURIComponent(m[1]):'';"
            )
            or ""
        )
    except Exception:
        return ""


def create_browser_options():
    """创建尽量贴近真实浏览器的启动参数（DrissionPage 后端）。

    TUN 系统代理时请保持 config.proxy 为空，让 Chromium 走系统网络栈。
    不要默认 new_env / 强制 UA / 过多 flag，容易触发 Cloudflare「故障排除」。
    """
    if ChromiumOptions is None:
        raise Exception("DrissionPage 未安装，无法使用 browser_engine=drission")
    options = ChromiumOptions()
    options.set_timeouts(base=1)
    # 并发时为每个 worker 分配独立资料目录，避免 cookie/会话互相污染
    profile_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_profiles")
    try:
        os.makedirs(profile_root, exist_ok=True)
        wid = _get_worker_id()
        profile_dir = os.path.join(
            profile_root,
            f"w{wid}_{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000) % 1000000}",
        )
        options.set_user_data_path(profile_dir)
    except Exception:
        pass
    # set_user_data_path 可能清掉 auto_port，必须放在后面重新启用
    options.auto_port()
    for flag in (
        "--no-first-run",
        "--no-default-browser-check",
    ):
        options.set_argument(flag)
    # 仅显式配置 proxy 时写入；TUN 模式保持空
    proxy = str(config.get("proxy", "") or "").strip()
    if proxy:
        try:
            options.set_proxy(proxy)
        except Exception:
            options.set_argument(f"--proxy-server={proxy}")
    # 默认使用浏览器真实 UA；仅当用户显式打开时才覆盖
    if config.get("browser_use_custom_ua", False):
        ua = get_user_agent()
        if ua:
            try:
                options.set_user_agent(ua)
            except Exception:
                options.set_argument(f"--user-agent={ua}")
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


def _build_request_kwargs(url=None, **kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies(url)
    # empty dict means explicit direct
    if proxies is not None:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 30)
    return request_kwargs


def _http_should_retry_direct(err: str) -> bool:
    e = str(err or "").lower()
    return any(
        k in e
        for k in (
            "127.0.0.1 port 7890",
            "could not connect to server",
            "connection timed out",
            "curl: (28)",
            "curl: (97)",
            "cannot complete socks5",
            "socks",
            "proxy",
            "timed out",
            "timeout",
        )
    )


def http_get(url, **kwargs):
    last_exc = None
    for attempt in range(1, 4):
        try:
            return requests.get(url, **_build_request_kwargs(url=url, **kwargs))
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            # 代理不可用 / 超时 / SOCKS 失败：直连重试
            if _http_should_retry_direct(err):
                try:
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["proxies"] = {}
                    retry_kwargs.setdefault("timeout", 35)
                    return requests.get(url, **_build_request_kwargs(url=url, **retry_kwargs))
                except Exception as exc2:
                    last_exc = exc2
            time.sleep(min(0.6 * attempt, 2.0))
    raise last_exc


def http_post(url, **kwargs):
    last_exc = None
    for attempt in range(1, 4):
        try:
            return requests.post(url, **_build_request_kwargs(url=url, **kwargs))
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            if _http_should_retry_direct(err):
                try:
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["proxies"] = {}
                    retry_kwargs.setdefault("timeout", 35)
                    return requests.post(url, **_build_request_kwargs(url=url, **retry_kwargs))
                except Exception as exc2:
                    last_exc = exc2
            time.sleep(min(0.6 * attempt, 2.0))
    raise last_exc


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


# ---------------------------------------------------------------------------
# ShiroMail (https://shiromail.galiais.com) — Bearer API key temp mail
# ---------------------------------------------------------------------------

def get_shiromail_api_base():
    return str(
        config.get("shiromail_api_base", "https://shiromail.galiais.com") or ""
    ).rstrip("/")


def get_shiromail_api_key():
    return str(config.get("shiromail_api_key", "") or "").strip()


def get_shiromail_domain():
    return str(
        config.get("shiromail_domain", "galiais.online") or "galiais.online"
    ).strip()


def get_shiromail_expires_in_hours():
    try:
        hours = int(config.get("shiromail_expires_in_hours", 24) or 24)
    except Exception:
        hours = 24
    return max(hours, 1)


def shiromail_build_headers(content_type=False):
    key = get_shiromail_api_key()
    if not key:
        raise Exception("ShiroMail API Key 未配置")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def shiromail_domain_name(item):
    if not isinstance(item, dict):
        return ""
    for key in ("domain", "name", "fqdn", "rootDomain", "root_domain"):
        val = item.get(key)
        if val:
            return str(val).strip().lower()
    return ""


_shiromail_rate_lock = threading.Lock()
_shiromail_rate_until = 0.0


def _shiromail_note_rate_limit(seconds=30.0):
    global _shiromail_rate_until
    with _shiromail_rate_lock:
        _shiromail_rate_until = max(_shiromail_rate_until, time.time() + float(seconds))


def _shiromail_wait_rate_limit():
    with _shiromail_rate_lock:
        until = float(_shiromail_rate_until or 0)
    wait = until - time.time()
    if wait > 0:
        time.sleep(min(wait, 60.0))


def _is_rate_limit_error(exc_or_status) -> bool:
    s = str(exc_or_status or "").lower()
    return ("429" in s) or ("rate limit" in s) or ("too many" in s)


def shiromail_get_domains():
    api_base = get_shiromail_api_base()
    if not api_base:
        raise Exception("ShiroMail API Base 未配置")
    _shiromail_wait_rate_limit()
    res = http_get(f"{api_base}/api/v1/domains", headers=shiromail_build_headers())
    if res.status_code == 429:
        _shiromail_note_rate_limit(45)
        raise Exception(f"ShiroMail domains 限流 HTTP 429: {(res.text or '')[:200]}")
    res.raise_for_status()
    return _pick_list_payload(res.json())


def shiromail_resolve_domain_id(preferred_domain=None, *, force_refresh=False):
    """按域名字符串解析 domainId；优先匹配 shiromail_domain。带 10min 缓存。"""
    preferred = (preferred_domain or get_shiromail_domain() or "").strip().lower()
    now = time.time()
    with _shiromail_domain_lock:
        cached_id = _shiromail_domain_cache.get("id")
        cached_name = str(_shiromail_domain_cache.get("name") or "")
        cached_ts = float(_shiromail_domain_cache.get("ts") or 0)
        # 有缓存时尽量不打 /domains，避免 429 风暴
        if (
            not force_refresh
            and cached_id is not None
            and (not preferred or cached_name == preferred or not preferred)
            and now - cached_ts < 1800
        ):
            return int(cached_id), cached_name or preferred

    domains = shiromail_get_domains()
    if not domains:
        raise Exception("ShiroMail 未返回可用域名")
    resolved_id = None
    resolved_name = ""
    if preferred:
        for item in domains:
            if shiromail_domain_name(item) == preferred:
                domain_id = item.get("id") or item.get("domainId") or item.get("domain_id")
                if domain_id is not None:
                    resolved_id, resolved_name = int(domain_id), shiromail_domain_name(item) or preferred
                    break
        if resolved_id is None:
            # 首选域名不在列表：回退第一个可用域名，避免反复 force_refresh 打爆 API
            first = domains[0]
            domain_id = first.get("id") or first.get("domainId") or first.get("domain_id")
            if domain_id is None:
                available = ", ".join(
                    shiromail_domain_name(d) or str(d.get("id", "?")) for d in domains[:12]
                )
                raise Exception(
                    f"ShiroMail 找不到域名 {preferred}；当前可见: {available or '(空)'}"
                )
            resolved_id = int(domain_id)
            resolved_name = shiromail_domain_name(first)
    else:
        first = domains[0]
        domain_id = first.get("id") or first.get("domainId") or first.get("domain_id")
        if domain_id is None:
            raise Exception("ShiroMail 域名条目缺少 id")
        resolved_id, resolved_name = int(domain_id), shiromail_domain_name(first)

    with _shiromail_domain_lock:
        _shiromail_domain_cache["id"] = resolved_id
        _shiromail_domain_cache["name"] = resolved_name
        _shiromail_domain_cache["ts"] = time.time()
    return resolved_id, resolved_name


def shiromail_create_mailbox(domain_id=None, local_part=None, expires_in_hours=None):
    api_base = get_shiromail_api_base()
    if not api_base:
        raise Exception("ShiroMail API Base 未配置")
    if domain_id is None:
        domain_id, _ = shiromail_resolve_domain_id()
    hours = (
        expires_in_hours
        if expires_in_hours is not None
        else get_shiromail_expires_in_hours()
    )
    body = {
        "domainId": int(domain_id),
        "expiresInHours": int(max(int(hours), 1)),
    }
    if local_part:
        body["localPart"] = str(local_part)
    _shiromail_wait_rate_limit()
    res = http_post(
        f"{api_base}/api/v1/mailboxes",
        headers=shiromail_build_headers(content_type=True),
        json=body,
    )
    if res.status_code == 429:
        _shiromail_note_rate_limit(45)
        preview = (res.text or "")[:300]
        raise Exception(f"ShiroMail 创建邮箱限流 HTTP 429: {preview}")
    if res.status_code >= 400:
        preview = (res.text or "")[:300]
        raise Exception(f"ShiroMail 创建邮箱失败 HTTP {res.status_code}: {preview}")
    data = res.json()
    if not isinstance(data, dict):
        raise Exception("ShiroMail 创建邮箱响应格式错误")
    return data


def shiromail_get_messages(mailbox_id):
    api_base = get_shiromail_api_base()
    res = http_get(
        f"{api_base}/api/v1/mailboxes/{mailbox_id}/messages",
        headers=shiromail_build_headers(),
    )
    res.raise_for_status()
    return _pick_list_payload(res.json())


def shiromail_get_message_detail(mailbox_id, message_id):
    api_base = get_shiromail_api_base()
    res = http_get(
        f"{api_base}/api/v1/mailboxes/{mailbox_id}/messages/{message_id}",
        headers=shiromail_build_headers(),
    )
    res.raise_for_status()
    data = res.json()
    return data if isinstance(data, dict) else {}


def shiromail_get_extractions(mailbox_id, message_id):
    api_base = get_shiromail_api_base()
    res = http_get(
        f"{api_base}/api/v1/mailboxes/{mailbox_id}/messages/{message_id}/extractions",
        headers=shiromail_build_headers(),
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    try:
        return res.json()
    except Exception:
        return None


def shiromail_get_raw_parsed(mailbox_id, message_id):
    api_base = get_shiromail_api_base()
    res = http_get(
        f"{api_base}/api/v1/mailboxes/{mailbox_id}/messages/{message_id}/raw/parsed",
        headers=shiromail_build_headers(),
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    try:
        return res.json()
    except Exception:
        return {"text": res.text or ""}


def _shiromail_collect_strings(obj, out, depth=0):
    if depth > 10 or obj is None:
        return
    if isinstance(obj, str):
        if obj.strip():
            out.append(obj)
        return
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(str(obj))
        return
    if isinstance(obj, dict):
        for value in obj.values():
            _shiromail_collect_strings(value, out, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _shiromail_collect_strings(item, out, depth + 1)


def shiromail_code_from_payload(payload, subject=""):
    """从 extractions / message / raw-parsed 响应中提取验证码。"""
    if payload is None:
        return None
    strings = []
    _shiromail_collect_strings(payload, strings)
    if subject:
        strings.insert(0, subject)
    for text in strings:
        code = extract_verification_code(
            text, subject=subject if text is subject else ""
        )
        if code:
            return code
    combined = "\n".join(strings)
    return extract_verification_code(combined, subject)


def shiromail_message_body_text(detail):
    parts = []
    if not isinstance(detail, dict):
        return ""
    subject = detail.get("subject") or ""
    if subject:
        parts.append(str(subject))
    for key in (
        "text",
        "textBody",
        "text_body",
        "plain",
        "body",
        "content",
        "snippet",
        "preview",
    ):
        val = detail.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
        elif isinstance(val, dict):
            _shiromail_collect_strings(val, parts)
    html = detail.get("html") or detail.get("htmlBody") or detail.get("html_body")
    if isinstance(html, str):
        parts.append(re.sub(r"<[^>]+>", " ", html))
    elif isinstance(html, list):
        for h in html:
            if isinstance(h, str):
                parts.append(re.sub(r"<[^>]+>", " ", h))
    return "\n".join(parts)


def shiromail_get_email_and_token(api_key=None, *, use_pool=True):
    """创建 ShiroMail 邮箱；dev_token 存 mailboxId（字符串）。优先邮箱池。"""
    if api_key:
        config["shiromail_api_key"] = str(api_key).strip()
    if not get_shiromail_api_key():
        raise Exception("ShiroMail API Key 未配置")
    if use_pool and get_mail_pool_size() > 0:
        try:
            pair = _mail_pool.get_nowait()
            if pair and pair[0] and pair[1]:
                return str(pair[0]), str(pair[1])
        except Exception:
            pass
    domain_id, domain_name = shiromail_resolve_domain_id()
    local_part = generate_username(10)
    last_exc = None
    data = None
    for attempt in range(1, 4):
        try:
            data = shiromail_create_mailbox(domain_id=domain_id, local_part=local_part)
            break
        except Exception as exc:
            last_exc = exc
            # 429：退避，不要立刻 force_refresh 再打 /domains
            if _is_rate_limit_error(exc):
                _shiromail_note_rate_limit(30 + 15 * attempt)
                time.sleep(min(5.0 * attempt, 20.0))
                continue
            # domain cache 可能过期，仅非限流时刷新一次
            if attempt == 2:
                try:
                    domain_id, domain_name = shiromail_resolve_domain_id(force_refresh=True)
                except Exception as exc2:
                    if _is_rate_limit_error(exc2):
                        _shiromail_note_rate_limit(45)
            local_part = generate_username(10)
            time.sleep(0.6 * attempt)
    if data is None:
        raise last_exc or Exception("ShiroMail 创建邮箱失败")
    mailbox_id = data.get("id") or data.get("mailboxId") or data.get("mailbox_id")
    address = (
        data.get("address")
        or data.get("email")
        or f"{data.get('localPart') or local_part}@{data.get('domain') or domain_name}"
    )
    if mailbox_id is None:
        raise Exception("ShiroMail 创建邮箱成功但缺少 mailbox id")
    if not address or "@" not in str(address):
        raise Exception(f"ShiroMail 创建邮箱响应缺少 address: {data}")
    return str(address), str(mailbox_id)


def _mail_pool_worker():
    """Background: keep N pre-created mailboxes ready."""
    while not _mail_pool_stop.is_set():
        try:
            target = get_mail_pool_size()
            if target <= 0 or get_email_provider() not in ("shiromail", "shiro", "galiais"):
                if _mail_pool_stop.wait(1.0):
                    break
                continue
            if _mail_pool.qsize() >= target:
                if _mail_pool_stop.wait(0.6):
                    break
                continue
            try:
                pair = shiromail_get_email_and_token(use_pool=False)
                _mail_pool.put(pair, timeout=2)
            except Exception as pool_exc:
                # 限流时拉长等待，避免池线程把 /domains 打爆
                wait = 8.0 if _is_rate_limit_error(pool_exc) else 2.0
                if _is_rate_limit_error(pool_exc):
                    _shiromail_note_rate_limit(40)
                if _mail_pool_stop.wait(wait):
                    break
        except Exception:
            if _mail_pool_stop.wait(1.0):
                break


def start_mail_pool(log_callback=None):
    global _mail_pool_thread
    if get_mail_pool_size() <= 0:
        return
    if get_email_provider() not in ("shiromail", "shiro", "galiais"):
        return
    with _mail_pool_lock:
        if _mail_pool_thread is not None and _mail_pool_thread.is_alive():
            return
        _mail_pool_stop.clear()
        _mail_pool_thread = threading.Thread(
            target=_mail_pool_worker, name="mail-pool", daemon=True
        )
        _mail_pool_thread.start()
        if log_callback:
            log_callback(f"[*] 邮箱预创建池已启动 (size={get_mail_pool_size()})")


def stop_mail_pool():
    _mail_pool_stop.set()
    # drain
    try:
        while True:
            _mail_pool.get_nowait()
    except Exception:
        pass


def shiromail_get_oai_code(
    mailbox_id,
    email,
    timeout=180,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    if not mailbox_id:
        raise Exception("ShiroMail mailboxId 为空")
    if poll_interval is None:
        poll_interval = get_email_poll_interval()
    deadline = time.time() + timeout
    seen_ids = set()
    next_resend_at = time.time() + get_email_resend_after_sec()
    resend_interval = get_email_resend_interval_sec()
    resend_max = get_email_resend_max()
    resend_count = 0
    deep_parse_ids = set()
    first_poll = True
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if (
            resend_callback
            and resend_count < resend_max
            and time.time() >= next_resend_at
        ):
            try:
                resend_callback()
                resend_count += 1
                if log_callback:
                    log_callback(
                        f"[*] 已触发重新发送验证码 ({resend_count}/{resend_max})"
                    )
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + resend_interval
        try:
            messages = shiromail_get_messages(mailbox_id)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] ShiroMail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            first_poll = False
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") or msg.get("messageId") or msg.get("message_id")
            if msg_id is None:
                continue
            list_subject = str(msg.get("subject") or "")
            # Fast path: list payload often already has OTP
            list_code = shiromail_code_from_payload(msg, list_subject)
            if list_code:
                if log_callback:
                    log_callback(f"[*] ShiroMail 从列表项提取到验证码: {list_code}")
                return list_code
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            # Medium path: extractions only once per message
            try:
                extractions = shiromail_get_extractions(mailbox_id, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] ShiroMail extractions 失败: {exc}")
                extractions = None
            code = shiromail_code_from_payload(extractions, list_subject)
            if code:
                if log_callback:
                    log_callback(f"[*] ShiroMail 从 extractions 提取到验证码: {code}")
                return code
            # Slow path: full detail only if still needed
            if msg_id in deep_parse_ids:
                continue
            deep_parse_ids.add(msg_id)
            try:
                detail = shiromail_get_message_detail(mailbox_id, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] ShiroMail 获取邮件详情失败: {exc}")
                detail = {}
            subject = str((detail or {}).get("subject") or list_subject or "")
            if log_callback and subject:
                log_callback(f"[Debug] ShiroMail 收到邮件: {subject}")
            body = shiromail_message_body_text(detail)
            code = extract_verification_code(body, subject) or shiromail_code_from_payload(
                detail, subject
            )
            if code:
                if log_callback:
                    log_callback(f"[*] ShiroMail 从邮件正文提取到验证码: {code}")
                return code
            try:
                parsed = shiromail_get_raw_parsed(mailbox_id, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] ShiroMail raw/parsed 失败: {exc}")
                parsed = None
            code = shiromail_code_from_payload(parsed, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] ShiroMail 从 raw/parsed 提取到验证码: {code}")
                return code
            # 正文可能延迟可读，下一轮再试同一封
            seen_ids.discard(msg_id)
        # 首轮立即再拉一轮，不 sleep，抢先拿到快到达的邮件
        if first_poll:
            first_poll = False
            continue
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"ShiroMail 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def get_email_provider():
    return str(config.get("email_provider", "duckmail") or "duckmail").strip().lower()


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider in ("shiromail", "shiro", "galiais"):
        return shiromail_get_email_and_token(api_key=api_key)
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("鑾峰彇 DuckMail token 澶辫触")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    if poll_interval is None:
        poll_interval = get_email_poll_interval()
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider in ("shiromail", "shiro", "galiais"):
        return shiromail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_tls = threading.local()
_cpa_async_threads: list = []


def _wait_cpa_async_threads(timeout=300, log_callback=None, skip_if_stopping=None):
    global _cpa_async_threads
    if skip_if_stopping and skip_if_stopping():
        timeout = min(float(timeout or 0), 5.0)
        if log_callback:
            log_callback(f"[*] 停止中，仅短暂等待 CPA mint 线程（{timeout:.0f}s）...")
    with _cpa_threads_lock:
        threads = [t for t in _cpa_async_threads if t.is_alive()]
        _cpa_async_threads = [t for t in _cpa_async_threads if t.is_alive()]
    if not threads:
        return
    if log_callback and not (skip_if_stopping and skip_if_stopping()):
        log_callback(f"[*] 等待 {len(threads)} 个异步 CPA mint 线程完成...")
    deadline = time.time() + max(float(timeout or 0), 0)
    for t in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(timeout=remaining)
    alive = [t for t in threads if t.is_alive()]
    if log_callback:
        if alive:
            log_callback(f"[!] {len(alive)} 个 CPA mint 线程超时未完成")
        else:
            log_callback("[+] 所有 CPA mint 线程已完成")


def _track_cpa_async_thread(thread):
    with _cpa_threads_lock:
        _cpa_async_threads.append(thread)


def _join_threads_interruptible(threads, should_stop=None, timeout=None, poll=0.5):
    """可被 stop/Ctrl+C 打断的线程等待，避免 join() 永久阻塞。"""
    threads = [t for t in (threads or []) if t is not None]
    if not threads:
        return
    deadline = None if timeout is None else (time.time() + max(float(timeout), 0))
    while any(t.is_alive() for t in threads):
        if should_stop and should_stop():
            # 给 worker 一点时间走 finally/stop_browser，再返回
            grace_deadline = time.time() + 3
            while any(t.is_alive() for t in threads) and time.time() < grace_deadline:
                for t in threads:
                    t.join(timeout=poll)
            return
        if deadline is not None and time.time() >= deadline:
            return
        for t in threads:
            t.join(timeout=poll)


def _get_browser():
    return getattr(_tls, 'browser', None)


def _set_browser(b):
    _tls.browser = b


def _get_page():
    return getattr(_tls, 'page', None)


def _set_page(p):
    _tls.page = p


def _get_worker_id():
    return getattr(_tls, 'worker_id', 0)


def _set_worker_id(wid):
    _tls.worker_id = wid


def start_browser(log_callback=None):
    engine = get_browser_engine()
    if log_callback:
        log_callback(f"[*] 浏览器引擎: {engine}")

    if engine == "cloak":
        last_exc = None
        for attempt in range(1, 5):
            try:
                ua = get_user_agent() if config.get("browser_use_custom_ua", False) else None
                ext_paths = [EXTENSION_PATH] if os.path.exists(EXTENSION_PATH) else None
                # Headless prefers default preset (faster, still humanized clicks)
                default_preset = (
                    "default" if config.get("browser_headless", True) else "careful"
                )
                browser, page = start_cloak_browser(
                    proxy=str(config.get("proxy", "") or "").strip(),
                    headless=bool(config.get("browser_headless", True)),
                    humanize=bool(config.get("cloak_humanize", True)),
                    human_preset=str(
                        config.get("cloak_human_preset", default_preset) or default_preset
                    ),
                    user_agent=ua,
                    extension_paths=ext_paths,
                    worker_id=int(_get_worker_id() or 0),
                    locale=str(config.get("cloak_locale", "en-US") or "en-US"),
                    timezone=str(
                        config.get("cloak_timezone", "America/New_York")
                        or "America/New_York"
                    ),
                    speed_mode=is_speed_mode(),
                    block_heavy_assets=bool(config.get("block_heavy_assets", True)),
                    log_callback=log_callback,
                )
                _set_browser(browser)
                _set_page(page)
                try:
                    install_protocol_capture(log_callback=log_callback)
                except Exception:
                    pass
                if log_callback and attempt > 1:
                    log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
                return _get_browser(), _get_page()
            except Exception as exc:
                last_exc = exc
                if log_callback:
                    log_callback(f"[Debug] CloakBrowser 启动失败(第{attempt}/4次): {exc}")
                try:
                    if _get_browser() is not None:
                        _get_browser().quit(del_data=True)
                except Exception:
                    pass
                _set_browser(None)
                _set_page(None)
                time.sleep(min(1.5 * attempt, 4))
        raise Exception(f"CloakBrowser 启动失败，已重试4次: {last_exc}")

    if Chromium is None:
        raise Exception(
            "DrissionPage 未安装。请 pip install DrissionPage，"
            "或将 browser_engine 设为 cloak"
        )
    last_exc = None
    for attempt in range(1, 5):
        try:
            _set_browser(Chromium(create_browser_options()))
            tabs = _get_browser().get_tabs()
            _set_page(tabs[-1] if tabs else _get_browser().new_tab())
            if log_callback and getattr(_get_browser(), "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {_get_browser().user_data_path}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return _get_browser(), _get_page()
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                if _get_browser() is not None:
                    _get_browser().quit(del_data=True)
            except Exception:
                pass
            _set_browser(None)
            _set_page(None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    profile_path = None
    browser = _get_browser()
    if browser is not None:
        try:
            profile_path = getattr(browser, "user_data_path", None)
        except Exception:
            profile_path = None
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    _set_browser(None)
    _set_page(None)
    if profile_path:
        try:
            import shutil

            root = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_profiles")
            )
            abs_profile = os.path.abspath(str(profile_path))
            if abs_profile.startswith(root) and os.path.isdir(abs_profile):
                shutil.rmtree(abs_profile, ignore_errors=True)
        except Exception:
            pass


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def soft_reset_browser(log_callback=None):
    """Clear cookies/storage without killing Chromium — much faster between accounts."""
    browser = _get_browser()
    page = _get_page()
    if browser is None or page is None:
        return start_browser(log_callback=log_callback)
    try:
        if hasattr(browser, "soft_reset"):
            page = browser.soft_reset()
            _set_page(page)
        else:
            try:
                page.set_cookies(False)
            except Exception:
                pass
            try:
                if hasattr(browser, "set_cookies"):
                    browser.set_cookies(False)
            except Exception:
                pass
            try:
                page.get("about:blank")
            except Exception:
                pass
            try:
                page.run_js(
                    "try{localStorage.clear()}catch(e){}try{sessionStorage.clear()}catch(e){}"
                )
            except Exception:
                pass
        with _protocol_ctx_lock:
            _protocol_ctx["capture_installed"] = False
        try:
            install_protocol_capture(log_callback=log_callback)
        except Exception:
            pass
        if log_callback:
            log_callback("[*] 软重置浏览器会话完成（保留进程，提速）")
        return browser, _get_page()
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] 软重置失败，改为完整重启: {exc}")
        return restart_browser(log_callback=log_callback)


def prepare_next_account_browser(log_callback=None, *, force_full=False, reason=""):
    """Choose soft vs full browser recycle based on config and failure reason."""
    mode = get_browser_restart_mode()
    need_full = force_full or mode == "full"
    reason_l = str(reason or "").lower()
    # Hard failures that need a brand-new browser
    if any(
        k in reason_l
        for k in (
            "tos-gate",
            "cloudflare 拦截",
            "page disconnected",
            "浏览器",
            "target closed",
            "turnstile 获取 token 失败",
        )
    ):
        need_full = True
    if need_full or get_browser_engine() != "cloak":
        if log_callback and reason:
            log_callback(f"[*] 完整重启浏览器（{reason[:80]}）")
        return restart_browser(log_callback=log_callback)
    return soft_reset_browser(log_callback=log_callback)


def prepare_clean_browser_session(log_callback=None, cancel_callback=None):
    """轻量清理：避免预访问 xAI/grok 触发 Cloudflare，同时尽量清掉残留登录态。"""
    raise_if_cancelled(cancel_callback)
    page = _get_page()
    browser = _get_browser()
    if page is None or browser is None:
        start_browser(log_callback=log_callback)
        page = _get_page()
        browser = _get_browser()
    try:
        if page is not None:
            try:
                page.get("about:blank")
            except Exception:
                pass
            try:
                page.run_js(
                    """
try { localStorage.clear(); } catch (e) {}
try { sessionStorage.clear(); } catch (e) {}
"""
                )
            except Exception:
                pass
        # 尽量清 cookie，但不主动打开 accounts.x.ai / grok.com（容易先撞 CF）
        if browser is not None and hasattr(browser, "set_cookies"):
            try:
                browser.set_cookies(False)
            except Exception:
                pass
        if page is not None and hasattr(page, "set_cookies"):
            try:
                page.set_cookies(False)
            except Exception:
                pass
        if log_callback:
            log_callback("[Debug] 已做轻量会话清理，准备打开注册页")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 清理浏览器会话失败，将重启浏览器: {exc}")
        restart_browser(log_callback=log_callback)


def detect_cloudflare_block_page(log_callback=None):
    """检测当前页是否为 Cloudflare 拦截/故障排除页。"""
    page = _get_page()
    if page is None:
        return False, ""
    try:
        info = page.run_js(
            r"""
const body = ((document.body && (document.body.innerText || document.body.textContent)) || '')
  .replace(/\s+/g, ' ').trim().slice(0, 500);
const title = document.title || '';
const html = (document.documentElement && document.documentElement.innerHTML || '').slice(0, 2000);
return { url: location.href || '', title, body, html };
"""
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 读取页面检测 CF 失败: {exc}")
        return False, ""
    if not isinstance(info, dict):
        return False, ""
    blob = " ".join(
        [
            str(info.get("url") or ""),
            str(info.get("title") or ""),
            str(info.get("body") or ""),
            str(info.get("html") or ""),
        ]
    ).lower()
    markers = (
        "故障排除",
        "attention required",
        "cf-error",
        "cf-error-details",
        "sorry, you have been blocked",
        "you have been blocked",
        "checking your browser before accessing",
        "enable javascript and cookies",
        "cloudflare ray id",
        "error code 1020",
        "error code 1005",
        "access denied",
    )
    hit = next((m for m in markers if m in blob), "")
    if not hit:
        return False, ""
    detail = f"url={info.get('url') or ''}; marker={hit}; title={info.get('title') or ''}"
    return True, detail


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")


def refresh_active_page():
    if _get_browser() is None:
        restart_browser()
    try:
        tabs = _get_browser().get_tabs()
        if tabs:
            _set_page(tabs[-1])
        else:
            _set_page(_get_browser().new_tab())
    except Exception:
        restart_browser()
    return _get_page()


_EMAIL_SIGNUP_JS = r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('value'),
        node.getAttribute('href'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const text = nodeText(node);
    const compact = text.replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册') || compact.includes('用邮箱注册') || compact.includes('邮箱注册')) return 100;
    if (lower.includes('signupwithemail') || lower.includes('sign-up-with-email') || lower.includes('sign_up_with_email')) return 95;
    if (lower.includes('continuewithemail') || lower.includes('continue-with-email')) return 90;
    if ((lower.includes('email') || compact.includes('邮箱')) &&
        (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with') || compact.includes('注册') || compact.includes('继续'))) {
        return 80;
    }
    if (lower === 'email' || lower === '邮箱' || compact.includes('电子邮箱')) return 70;
    return 0;
}
function emailInputReady() {
    const selectors = [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[placeholder*="mail" i]',
        'input[aria-label*="mail" i]',
        'input[aria-label*="邮箱"]',
        'input[placeholder*="邮箱"]',
    ];
    for (const sel of selectors) {
        const node = document.querySelector(sel);
        if (node && isVisible(node) && !node.disabled && !node.readOnly) return true;
    }
    return false;
}
function collectCandidates() {
    const nodes = Array.from(document.querySelectorAll(
        'button, a, [role="button"], input[type="button"], input[type="submit"], div[role="button"], span[role="button"]'
    ));
    return nodes
        .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
        .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score);
}
const url = location.href || '';
const title = document.title || '';
const bodyText = (document.body && (document.body.innerText || document.body.textContent) || '').replace(/\s+/g, ' ').trim().slice(0, 240);
const candidates = collectCandidates();
const buttons = candidates.slice(0, 8).map((item) => item.text || '').filter(Boolean);
if (emailInputReady()) {
    return {
        state: 'email-form-ready',
        url,
        title,
        buttons,
        body: bodyText,
    };
}
const target = candidates[0] || null;
if (!target) {
    return {
        state: 'not-found',
        url,
        title,
        buttons: Array.from(document.querySelectorAll('button, a, [role="button"]'))
            .filter((node) => isVisible(node))
            .map(nodeText)
            .filter(Boolean)
            .slice(0, 10),
        body: bodyText,
    };
}
try { target.node.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
target.node.click();
return {
    state: 'clicked',
    text: target.text || true,
    url,
    title,
    buttons,
    body: bodyText,
};
"""


def _signup_page_snapshot(log_callback=None):
    page = _get_page()
    if page is None:
        return {"url": "none", "title": "", "buttons": [], "body": ""}
    try:
        snap = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
  return [node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title'), node.getAttribute('href')]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
return {
  url: location.href || '',
  title: document.title || '',
  buttons: Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((n) => isVisible(n))
    .map(nodeText)
    .filter(Boolean)
    .slice(0, 12),
  body: ((document.body && (document.body.innerText || document.body.textContent)) || '').replace(/\s+/g, ' ').trim().slice(0, 300),
  hasEmail: !!document.querySelector('input[type="email"], input[name="email"], input[data-testid="email"]'),
};
"""
        )
        if isinstance(snap, dict):
            return snap
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 读取注册页快照失败: {exc}")
    try:
        return {
            "url": getattr(page, "url", "") or "",
            "title": "",
            "buttons": [],
            "body": (page.html or "")[:300],
            "hasEmail": False,
        }
    except Exception:
        return {"url": "none", "title": "", "buttons": [], "body": "", "hasEmail": False}


def click_email_signup_button(timeout=18, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_diag = 0.0
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        blocked, detail = detect_cloudflare_block_page(log_callback=log_callback)
        if blocked:
            raise Exception(f"Cloudflare 拦截页，无法点击邮箱注册: {detail}")
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        try:
            clicked = _get_page().run_js(_EMAIL_SIGNUP_JS)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 查找邮箱注册按钮异常: {exc}")
            clicked = None

        state = clicked.get("state") if isinstance(clicked, dict) else clicked
        if state in ("clicked", True) or (isinstance(clicked, str) and clicked):
            detail = ""
            if isinstance(clicked, dict):
                detail = f": {clicked.get('text')}" if clicked.get("text") else ""
            elif isinstance(clicked, str):
                detail = f": {clicked}"
            if log_callback:
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            pace_sleep(0.55, cancel_callback)
            return True
        if state == "email-form-ready":
            if log_callback:
                log_callback("[*] 已处于邮箱注册表单，跳过入口按钮点击")
            return True

        now = time.time()
        if log_callback and now - last_diag >= 2:
            last_diag = now
            snap = clicked if isinstance(clicked, dict) else _signup_page_snapshot(log_callback)
            url = (snap or {}).get("url") or (_get_page().url if _get_page() else "none")
            buttons = " | ".join((snap or {}).get("buttons") or []) or "none"
            body = ((snap or {}).get("body") or "")[:160]
            log_callback(f"[Debug] 当前URL: {url}; buttons={buttons}; body={body}")

        # 页面若仍空白/未加载完，主动再刷一次注册页
        try:
            url_now = (_get_page().url if _get_page() else "") or ""
            if "about:blank" in url_now or not url_now:
                _get_page().get(SIGNUP_URL)
                _get_page().wait.doc_loaded()
        except Exception:
            pass
        pace_sleep(0.35, cancel_callback)

    blocked, detail = detect_cloudflare_block_page(log_callback=log_callback)
    if blocked:
        raise Exception(f"Cloudflare 拦截页，无法点击邮箱注册: {detail}")
    snap = _signup_page_snapshot(log_callback)
    if log_callback:
        log_callback(
            f"[Debug] 页面内容片段: url={snap.get('url')}; title={snap.get('title')}; "
            f"buttons={' | '.join(snap.get('buttons') or []) or 'none'}; body={(snap.get('body') or '')[:300]}"
        )
    fail_url = str(snap.get("url") or "unknown")
    fail_buttons = " | ".join(snap.get("buttons") or []) or "none"
    residual_hint = ""
    low = fail_url.lower()
    if any(k in low for k in ("tos-gate", "accept-tos", "/tos", "grok.com")) or any(
        k in fail_buttons for k in ("知道了", "Got it", "I understand")
    ):
        residual_hint = "；疑似上号会话/TOS 残留（非缺点击流程），账号结束后将完整重启浏览器"
    raise Exception(
        "未找到「使用邮箱注册」按钮"
        f"（url={fail_url}; buttons={fail_buttons}{residual_hint}）"
    )


def open_signup_page(log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    if _get_browser() is None:
        start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")
        if not os.path.exists(EXTENSION_PATH) and log_callback:
            log_callback("[!] 未找到 turnstilePatch 扩展目录，Turnstile 辅助可能不可用")
    prepare_clean_browser_session(log_callback=log_callback, cancel_callback=cancel_callback)
    last_exc = None
    opened = False
    for attempt in range(1, 4):
        raise_if_cancelled(cancel_callback)
        try:
            browser = _get_browser()
            if browser is None:
                start_browser(log_callback=log_callback)
                browser = _get_browser()
            try:
                tabs = browser.get_tabs()
                _set_page(tabs[0] if tabs else browser.new_tab())
            except Exception:
                _set_page(browser.new_tab())
            _get_page().get(SIGNUP_URL)
            _get_page().wait.doc_loaded()
            # 给 CF/前端一点渲染时间
            pace_sleep(0.28 if is_speed_mode() else 0.55, cancel_callback)
            blocked, detail = detect_cloudflare_block_page(log_callback=log_callback)
            if blocked:
                last_exc = Exception(f"Cloudflare 拦截页: {detail}")
                if log_callback:
                    log_callback(f"[!] 检测到 Cloudflare 拦截/故障排除页，重启浏览器重试 ({attempt}/3): {detail}")
                restart_browser(log_callback=log_callback)
                sleep_with_cancel(1.5, cancel_callback)
                continue
            last_exc = None
            opened = True
            break
        except RegistrationCancelled:
            raise
        except Exception as e:
            last_exc = e
            if log_callback:
                log_callback(f"[Debug] 打开注册页失败(第{attempt}/3次): {e}")
            try:
                restart_browser(log_callback=log_callback)
            except Exception as e2:
                if log_callback:
                    log_callback(f"[Debug] 重启浏览器失败: {e2}")
            sleep_with_cancel(1, cancel_callback)
    if not opened:
        raise Exception(f"打开注册页失败: {last_exc}")

    _deadline = time.time() + 10
    while time.time() < _deadline:
        raise_if_cancelled(cancel_callback)
        blocked, detail = detect_cloudflare_block_page(log_callback=log_callback)
        if blocked:
            if log_callback:
                log_callback(f"[!] 注册页加载后仍是 Cloudflare 拦截页: {detail}")
            raise Exception(f"Cloudflare 拦截页: {detail}")
        try:
            _ready = _get_page().run_js(
                "return !!document.querySelector('button, input[type=\"email\"], a[href*=\"sign\"], a[href*=\"email\"], form')"
            )
            if _ready:
                break
        except Exception:
            pass
        time.sleep(0.3)
    if log_callback:
        log_callback(f"[*] 当前URL: {_get_page().url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
    try:
        return bool(
            _get_page().run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(
    timeout=45,
    log_callback=None,
    cancel_callback=None,
    email=None,
    dev_token=None,
):
    raise_if_cancelled(cancel_callback)
    if not email or not dev_token:
        email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = _get_page().run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                try:
                    reclicked = _get_page().run_js(_EMAIL_SIGNUP_JS)
                except Exception:
                    reclicked = None
                last_reclick_time = now
                re_state = reclicked.get("state") if isinstance(reclicked, dict) else reclicked
                if re_state == "email-form-ready":
                    if log_callback:
                        log_callback("[Debug] 邮箱输入框检测中：页面已进入邮箱表单")
                elif re_state in ("clicked", True) or (isinstance(reclicked, str) and reclicked):
                    detail = ""
                    if isinstance(reclicked, dict) and reclicked.get("text"):
                        detail = f": {reclicked.get('text')}"
                    elif isinstance(reclicked, str):
                        detail = f": {reclicked}"
                    if log_callback:
                        log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", _get_page().url if _get_page() else "") if isinstance(filled, dict) else (_get_page().url if _get_page() else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        pace_sleep(0.25 if is_speed_mode() else 0.8, cancel_callback)
        clicked = _get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            # 关键：必须等到浏览器真正发出 CreateEmail 或进入 OTP 页
            # 否则“假提交”会导致收不到邮件，后续协议补发又与迟到的 UI 发信双码冲突
            seen_before = get_create_email_seen_count()
            wait_deadline = time.time() + (12 if is_speed_mode() else 18)
            reclick_at = time.time() + 2.5
            while time.time() < wait_deadline:
                raise_if_cancelled(cancel_callback)
                seen_now = get_create_email_seen_count()
                state = str(_page_after_otp_state() or "")
                if seen_now > seen_before or state in ("profile-ready", "still-otp"):
                    if log_callback:
                        log_callback(
                            f"[*] 邮箱提交已确认 create_email_seen={seen_now} state={state}"
                        )
                    return email, dev_token
                # 仍停在邮箱表单则再点一次提交
                if time.time() >= reclick_at and state not in ("still-otp", "profile-ready"):
                    reclick_at = time.time() + 3.0
                    try:
                        _get_page().run_js(
                            r"""
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const btn = buttons.find((n) => {
  const t = (n.innerText||n.textContent||'').replace(/\s+/g,'').toLowerCase();
  return t.includes('signup') || t.includes('sign up') || t.includes('注册') || t.includes('继续');
});
if (btn && !btn.disabled) { btn.click(); return true; }
return false;
                            """
                        )
                        if log_callback:
                            log_callback("[*] 邮箱提交未生效，已重试点击提交")
                    except Exception:
                        pass
                pace_sleep(0.25, cancel_callback)
            # 超时仍未见 CreateEmail：留给后续协议补发，但标记清楚
            if log_callback:
                log_callback(
                    f"[!] 邮箱提交后未确认 CreateEmail (seen={get_create_email_seen_count()})，将依赖协议补发"
                )
            return email, dev_token
        pace_sleep(0.25 if is_speed_mode() else 0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", _get_page().url if _get_page() else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def _page_after_otp_state():
    """Detect whether OTP step advanced to profile form or is still stuck/error."""
    page = _get_page()
    if page is None:
        return "no-page"
    try:
        return page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const body = (document.body && document.body.innerText || '').slice(0, 800).toLowerCase();
if (/invalid|incorrect|wrong code|验证码.*错误|无效|expired|已过期/.test(body)) {
  return 'code-error';
}
const given = Array.from(document.querySelectorAll(
  'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]'
)).some((n) => isVisible(n));
const pwd = Array.from(document.querySelectorAll(
  'input[data-testid="password"], input[name="password"], input[type="password"]'
)).some((n) => isVisible(n));
if (given && pwd) return 'profile-ready';
const otp = Array.from(document.querySelectorAll(
  'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"]'
)).some((n) => isVisible(n));
if (otp) return 'still-otp';
return 'unknown:' + location.href + '|' + body.slice(0, 120).replace(/\s+/g, ' ');
            """
        )
    except Exception as e:
        return f"js-error:{e}"


def start_code_prefetch(email, dev_token, log_callback=None, cancel_callback=None, resend_callback=None):
    """Background mail poll so OTP fetch overlaps UI settle after email submit."""
    holder = {
        "code": None,
        "err": None,
        "done": threading.Event(),
        "t0": time.time(),
    }
    fallback_sec = 8.0
    try:
        fallback_sec = max(float(config.get("protocol_mail_fallback_sec", 8) or 8), 4.0)
    except Exception:
        fallback_sec = 8.0
    fallback_used = {"n": 0}

    def _worker():
        try:
            # 若浏览器 CreateEmail 未发出或邮件迟迟不到，协议补发一次（已验证可达）
            def _wrapped_resend():
                if resend_callback:
                    resend_callback()

            # 首次：短等后若仍无信且未见浏览器 CreateEmail，主动协议发信
            def _maybe_proto_boost():
                if fallback_used["n"] > 0 or not email:
                    return
                if not bool(config.get("protocol_early_otp", True)):
                    return
                if time.time() - holder["t0"] < fallback_sec:
                    return
                seen = get_create_email_seen_count()
                # 浏览器已发过 CreateEmail 则再等一会儿，避免协议/UI 双码把 OTP 弄废
                if seen > 0 and time.time() - holder["t0"] < fallback_sec + 12:
                    return
                fallback_used["n"] += 1
                if log_callback:
                    log_callback(
                        f"[*] {time.time() - holder['t0']:.0f}s 未收到验证码，协议补发"
                        f" (browser_create_email_seen={seen})..."
                    )
                protocol_send_otp(
                    email,
                    log_callback=log_callback,
                    castle=get_protocol_castle(),
                )

            def _resend_with_boost():
                _maybe_proto_boost()
                if resend_callback:
                    resend_callback()

            # 首次 resend 时间对齐 fallback，尽快协议补发
            code = get_oai_code(
                dev_token,
                email,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
                resend_callback=_resend_with_boost,
            )
            holder["code"] = code
        except Exception as exc:
            holder["err"] = exc
        finally:
            holder["done"].set()

    t = threading.Thread(target=_worker, name="code-prefetch", daemon=True)
    t.start()
    holder["thread"] = t
    return holder


def wait_code_prefetch(holder, timeout=240, cancel_callback=None):
    if not holder:
        return None
    deadline = time.time() + max(float(timeout or 240), 30)
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if holder["done"].wait(0.2):
            break
    if holder.get("err") is not None and not holder.get("code"):
        raise holder["err"]
    code = holder.get("code")
    if code:
        return code
    # 仍在跑：再等一小段，禁止立刻再开第二路轮询
    if not holder["done"].is_set():
        extra = min(60.0, max(0.0, deadline - time.time()) + 45.0)
        end2 = time.time() + extra
        while time.time() < end2:
            raise_if_cancelled(cancel_callback)
            if holder["done"].wait(0.2):
                break
        if holder.get("err") is not None and not holder.get("code"):
            raise holder["err"]
        code = holder.get("code")
        if code:
            return code
    return None


def make_otp_resend_callback(email, log_callback=None):
    """仅点击页面 Resend；协议补发由 start_code_prefetch 的 boost 统一负责。"""

    def _resend_code():
        try:
            ok = _get_page().run_js(
                r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
                """
            )
            if log_callback and ok:
                log_callback("[*] 已点击页面重新发送验证码")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] UI resend 失败: {exc}")

    return _resend_code


def fill_code_and_submit(
    email,
    dev_token,
    timeout=240,
    log_callback=None,
    cancel_callback=None,
    code=None,
    code_prefetch=None,
):
    resend_cb = make_otp_resend_callback(email, log_callback=log_callback)
    mail_timeout = max(int(timeout or 240), 120)

    if not code and code_prefetch is not None:
        if log_callback and not code_prefetch["done"].is_set():
            log_callback("[*] 并行取信中（与 OTP 页就绪重叠）...")
        code = wait_code_prefetch(
            code_prefetch, timeout=mail_timeout, cancel_callback=cancel_callback
        )
    if not code:
        # 仅在没有并行预取，或预取彻底失败时才单开一路取信
        if code_prefetch is not None and log_callback:
            log_callback("[!] 并行取信未拿到码，回退单路轮询...")
        code = get_oai_code(
            dev_token,
            email,
            timeout=mail_timeout,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_cb,
        )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = _get_page().run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = _get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            # Wait for profile form (do not return blind after 1.5s)
            wait_deadline = time.time() + (12 if is_speed_mode() else 25)
            last_state = ""
            while time.time() < wait_deadline:
                raise_if_cancelled(cancel_callback)
                state = str(_page_after_otp_state() or "")
                last_state = state
                if state == "profile-ready":
                    if log_callback:
                        log_callback("[*] 验证码已通过，资料页已就绪")
                    return code
                if state == "code-error":
                    raise Exception(f"验证码被拒绝: {code}")
                pace_sleep(0.2 if is_speed_mode() else 0.5, cancel_callback)
            # Soft success: profile may still render; fill_profile will retry
            if log_callback:
                log_callback(f"[!] 提交验证码后资料页未立即出现 (state={last_state})，继续尝试填写资料")
            return code

        pace_sleep(0.2 if is_speed_mode() else 0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def _read_turnstile_token_js():
    page = _get_page()
    if page is None:
        return ""
    # CloakPage has native reader
    if hasattr(page, "read_turnstile_token"):
        try:
            return str(page.read_turnstile_token() or "").strip()
        except Exception:
            pass
    try:
        token = page.run_js(
            """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
            """
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _inject_turnstile_token(token, log_callback=None):
    token = str(token or "").strip()
    if not token or _get_page() is None:
        return 0
    try:
        synced = _get_page().run_js(
            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]')
  || document.querySelector('textarea[name="cf-turnstile-response"]');
if (!cfInput || !token) return 0;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
try {
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    /* token already issued by CF widget */
  }
} catch (e) {}
return String(cfInput.value || '').trim().length;
            """,
            token,
        )
        return int(synced or 0)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 回填 Turnstile token 失败: {exc}")
        return 0


_turnstile_solve_lock = threading.Lock()


def getTurnstileToken(log_callback=None, cancel_callback=None, timeout=None):
    """自动通过 Cloudflare Turnstile（优先真实鼠标点击 managed 复选框）。

    注意：不要调用 turnstile.reset()——会打断非交互评分并强制出勾选框。
    """
    if timeout is None:
        timeout = get_turnstile_timeout()
    page = _get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    # 已有 token 直接返回
    existing = _read_turnstile_token_js()
    if len(existing) >= 80:
        if log_callback:
            log_callback(f"[*] Turnstile 已通过，token长度={len(existing)}")
        return existing

    # 串行化求解，避免主流程与 early-cf 线程双点冲突
    if not _turnstile_solve_lock.acquire(blocking=False):
        # 另一线程正在点：短等共享结果
        wait_deadline = time.time() + min(float(timeout or 20), 20)
        while time.time() < wait_deadline:
            raise_if_cancelled(cancel_callback)
            existing = _read_turnstile_token_js()
            if len(existing) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(existing)}")
                return existing
            if _turnstile_solve_lock.acquire(blocking=False):
                break
            sleep_with_cancel(0.15, cancel_callback)
        else:
            existing = _read_turnstile_token_js()
            if len(existing) >= 80:
                return existing
            raise Exception("Turnstile 求解被占用且超时")
    try:
        existing = _read_turnstile_token_js()
        if len(existing) >= 80:
            if log_callback:
                log_callback(f"[*] Turnstile 已通过，token长度={len(existing)}")
            return existing

        # Cloak 路径：真人鼠标点 iframe 复选框
        if hasattr(page, "solve_turnstile"):
            mode = "无头" if config.get("browser_headless", True) else "有头"
            if log_callback:
                log_callback(f"[*] 使用 Cloak 自动点击 Turnstile（{mode}/真人轨迹）...")
            try:
                return page.solve_turnstile(
                    timeout=float(timeout or get_turnstile_timeout()),
                    poll_interval=0.28 if is_speed_mode() else 0.55,
                    log=log_callback,
                    cancel=cancel_callback,
                    min_token_len=80,
                )
            except Exception as exc:
                if log_callback:
                    log_callback(f"[!] Cloak Turnstile 求解失败，回退旧逻辑: {exc}")

        # Drission / 通用回退：坐标点击 + 轮询（仍不 reset）
        deadline = time.time() + max(float(timeout), 10)
        last_click = 0.0
        click_n = 0
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)
            token = _read_turnstile_token_js()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            now = time.time()
            if now - last_click >= (0.9 if is_speed_mode() else 1.5):
                last_click = now
                click_n += 1
                try:
                    clicked = False
                    challenge_input = page.ele("@name=cf-turnstile-response") if hasattr(page, "ele") else None
                    if challenge_input:
                        wrapper = challenge_input.parent() if hasattr(challenge_input, "parent") else None
                        iframe = None
                        try:
                            iframe = wrapper.shadow_root.ele("tag:iframe") if wrapper else None
                        except Exception:
                            iframe = None
                        if iframe is None and hasattr(page, "ele"):
                            try:
                                iframe = page.ele("tag:iframe")
                            except Exception:
                                iframe = None
                        if iframe is not None:
                            try:
                                iframe.click()
                                clicked = True
                            except Exception:
                                pass
                            try:
                                body_sr = iframe.ele("tag:body").shadow_root
                                btn = body_sr.ele("tag:input") if body_sr else None
                                if btn:
                                    btn.click()
                                    clicked = True
                            except Exception:
                                pass
                    if not clicked:
                        page.run_js(
                            """
const iframe = document.querySelector(
  'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[src*="cf-chl"]'
);
if (!iframe) return 'no-iframe';
iframe.scrollIntoView({block:'center', inline:'center'});
const r = iframe.getBoundingClientRect();
const x = r.left + 28;
const y = r.top + r.height / 2;
const el = document.elementFromPoint(x, y) || iframe;
const opts = {bubbles:true, clientX:x, clientY:y, view:window};
el.dispatchEvent(new MouseEvent('mousemove', opts));
el.dispatchEvent(new MouseEvent('mousedown', opts));
el.dispatchEvent(new MouseEvent('mouseup', opts));
el.dispatchEvent(new MouseEvent('click', opts));
return 'js-click';
                            """
                        )
                    if log_callback and click_n <= 4:
                        log_callback(f"[*] Turnstile 回退点击 #{click_n}")
                except Exception:
                    pass
            sleep_with_cancel(0.45 if is_speed_mode() else 0.8, cancel_callback)

        raise Exception(f"Turnstile 获取 token 失败（超时 {int(timeout)}s）")
    finally:
        try:
            _turnstile_solve_lock.release()
        except Exception:
            pass


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(
    timeout=120,
    log_callback=None,
    cancel_callback=None,
    email="",
    email_code="",
):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0
    last_not_ready_log = 0.0
    hybrid = is_hybrid_mode() and bool(config.get("protocol_create_user", True))
    hybrid_create_tried = False
    early_cf_started = False
    early_cf_holder = {"token": "", "done": False}

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        # 资料页一出现就后台点 Turnstile，与姓名密码填写重叠
        if (
            is_speed_mode()
            and (not early_cf_started)
            and (not form_filled_once)
            and has_profile_form()
        ):
            early_cf_started = True
            if len(_read_turnstile_token_js()) < 80:
                if log_callback:
                    log_callback("[*] 资料页已出现，后台提前处理 Turnstile...")

                def _early_cf():
                    try:
                        tok = getTurnstileToken(
                            log_callback=None,
                            cancel_callback=cancel_callback,
                            timeout=min(get_turnstile_timeout(), 22),
                        )
                        if tok:
                            early_cf_holder["token"] = tok
                            try:
                                _inject_turnstile_token(tok, log_callback=None)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    finally:
                        early_cf_holder["done"] = True

                threading.Thread(target=_early_cf, name="early-cf", daemon=True).start()

        if not form_filled_once:
            filled = _get_page().run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                if log_callback:
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 立即尝试自动点 Turnstile（不要干等 12s）
                # 立即处理 Turnstile（无冷却空窗）
                if now - last_cf_retry_at >= 0.8:
                    if log_callback:
                        log_callback("[*] 自动处理 Cloudflare Turnstile...")
                    try:
                        token = getTurnstileToken(
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                            timeout=get_turnstile_timeout(),
                        )
                        if token:
                            synced = _inject_turnstile_token(token, log_callback=log_callback)
                            if log_callback:
                                log_callback(f"[*] Turnstile 自动处理完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 自动处理失败: {cf_exc}")
                    last_cf_retry_at = time.time()
                pace_sleep(0.25, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                now = time.time()
                if log_callback and now - last_not_ready_log >= 8:
                    last_not_ready_log = now
                    try:
                        diag = _page_after_otp_state()
                        log_callback(f"[Debug] 资料页未就绪: {diag}")
                    except Exception:
                        pass
                sleep_with_cancel(0.5, cancel_callback)
                continue

        # Hybrid: try page-side server-action once; on fail re-solve Turnstile then UI
        if (
            hybrid
            and (not hybrid_create_tried)
            and form_filled_once
            and email
            and email_code
        ):
            ts = _read_turnstile_token_js()
            if len(ts) >= 80:
                hybrid_create_tried = True
                if log_callback:
                    log_callback("[*] hybrid 协议建号 (page-fetch server-action)...")
                try:
                    created = protocol_create_user_via_action(
                        email,
                        email_code,
                        given_name,
                        family_name,
                        password,
                        ts,
                        log_callback=log_callback,
                    )
                    sso = (created or {}).get("sso") or read_sso_from_browser()
                    if sso:
                        if log_callback:
                            log_callback("[+] hybrid 协议建号成功，已拿到 sso")
                        return {
                            "given_name": given_name,
                            "family_name": family_name,
                            "password": password,
                            "sso": sso,
                            "created_via": "protocol",
                        }
                    # Failed create likely burned one-shot Turnstile — re-solve for UI
                    if log_callback:
                        log_callback("[!] 协议建号未拿到 sso，重新处理 Turnstile 后走 UI 提交")
                    try:
                        _get_page().run_js(
                            """
const cf = document.querySelector('input[name="cf-turnstile-response"]');
if (cf) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  if (setter) setter.call(cf, '');
  else cf.value = '';
}
"""
                        )
                    except Exception:
                        pass
                    try:
                        token = getTurnstileToken(
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                            timeout=get_turnstile_timeout(),
                        )
                        if token:
                            _inject_turnstile_token(token, log_callback=log_callback)
                            if log_callback:
                                log_callback(
                                    f"[*] UI 回退 Turnstile 已就绪 len={len(token)}"
                                )
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] UI 回退 Turnstile 失败: {cf_exc}")
                except Exception as e:
                    if log_callback:
                        log_callback(f"[!] hybrid 协议建号异常，回退 UI 提交: {e}")

        submit_state = _get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - last_cf_retry_at >= 0.8:
                if log_callback:
                    log_callback("[*] 提交前自动处理 Turnstile...")
                try:
                    token = getTurnstileToken(
                        log_callback=log_callback,
                        cancel_callback=cancel_callback,
                        timeout=get_turnstile_timeout(),
                    )
                    if token:
                        synced = _inject_turnstile_token(token, log_callback=log_callback)
                        if log_callback:
                            log_callback(f"[*] Turnstile 自动处理完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 自动处理失败: {cf_exc}")
                last_cf_retry_at = time.time()
            pace_sleep(0.25, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
                "created_via": "ui",
            }
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        pace_sleep(0.3, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=90, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if _get_page() is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = _get_page().run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 2.0:
                        if log_callback:
                            log_callback("[*] 最终页自动处理 Turnstile...")
                        try:
                            token = getTurnstileToken(
                                log_callback=log_callback,
                                cancel_callback=cancel_callback,
                                timeout=get_turnstile_timeout(),
                            )
                            if token:
                                synced = _inject_turnstile_token(
                                    token, log_callback=log_callback
                                )
                                if log_callback:
                                    log_callback(
                                        f"[*] 最终页 Turnstile 完成，回填长度={synced}"
                                    )
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 失败: {cf_exc}")
                        last_cf_retry_at = time.time()

            cookies = _get_page().cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        pace_sleep(0.4, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


class CliStopController:
    def __init__(self):
        self.stop_requested = False
        self._sigint_count = 0
        self._lock = threading.Lock()

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        with self._lock:
            self.stop_requested = True

    def handle_sigint(self, signum=None, frame=None):
        """第一次 Ctrl+C 请求优雅停止；第二次强制退出。"""
        with self._lock:
            self._sigint_count += 1
            count = self._sigint_count
            self.stop_requested = True
        if count == 1:
            cli_log("[!] 收到 Ctrl+C，正在停止...（再按一次强制退出）")
            return
        cli_log("[!] 再次收到 Ctrl+C，强制退出")
        try:
            os._exit(1)
        except Exception:
            raise SystemExit(1)


def cli_log(message):
    if not should_emit_log(message):
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def _install_cli_sigint_handler(controller):
    """安装可重入的 Ctrl+C 处理。Windows/Git Bash 下尽量可用。"""
    previous = None
    try:
        import signal

        previous = signal.getsignal(signal.SIGINT)

        def _handler(signum, frame):
            controller.handle_sigint(signum, frame)

        signal.signal(signal.SIGINT, _handler)
        return previous
    except Exception:
        return previous


def _restore_sigint_handler(previous):
    try:
        import signal

        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    except Exception:
        pass


def _register_one_account_cli(log_fn, stop_fn, accounts_output_file):
    email = ""
    dev_token = ""
    code = ""
    mail_ok = False
    max_mail_retry = 3
    hybrid = is_hybrid_mode()
    t_account = time.time()
    if hybrid:
        log_fn(
            "[*] 注册模式: hybrid"
            + (" + 协议建号" if config.get("protocol_create_user") else " (UI 建号，协议捕获就绪)")
        )
        install_protocol_capture(log_callback=log_fn)
    for mail_try in range(1, max_mail_retry + 1):
        log_fn(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
        mail_box = {"pair": None, "err": None}
        mail_thread = None
        if config.get("parallel_email_create", True):
            def _create_mail():
                try:
                    mail_box["pair"] = get_email_and_token()
                except Exception as exc:
                    mail_box["err"] = exc
            mail_thread = threading.Thread(target=_create_mail, daemon=True)
            mail_thread.start()
        open_signup_page(log_callback=log_fn, cancel_callback=stop_fn)
        install_protocol_capture(log_callback=log_fn)
        log_fn("[*] 2. 创建邮箱并提交")
        pre_email = pre_token = None
        if mail_thread is not None:
            mail_thread.join(timeout=45)
            if mail_box["err"] is not None:
                raise mail_box["err"]
            if mail_box["pair"]:
                pre_email, pre_token = mail_box["pair"]
        t_mail = time.time()
        email, dev_token = fill_email_and_submit(
            log_callback=log_fn,
            cancel_callback=stop_fn,
            email=pre_email,
            dev_token=pre_token,
        )
        log_fn(f"[*] 邮箱: {email} (+{time.time() - t_account:.1f}s)")
        # 注意：UI 已 CreateEmail；禁止立刻协议二次发信
        try:
            append_mail_credential(email, dev_token)
        except Exception:
            pass
        log_fn("[*] 3. 拉取验证码")
        t_code = time.time()
        code_prefetch = None
        if config.get("parallel_code_prefetch", True):
            code_prefetch = start_code_prefetch(
                email,
                dev_token,
                log_callback=log_fn,
                cancel_callback=stop_fn,
                resend_callback=make_otp_resend_callback(email, log_callback=log_fn),
            )
        try:
            code = fill_code_and_submit(
                email,
                dev_token,
                log_callback=log_fn,
                cancel_callback=stop_fn,
                code_prefetch=code_prefetch,
            )
            mail_ok = True
            log_fn(f"[*] 验证码阶段耗时 {time.time() - t_code:.1f}s (提交邮箱起 {time.time() - t_mail:.1f}s)")
            break
        except Exception as mail_exc:
            msg = str(mail_exc)
            if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                log_fn(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                prepare_next_account_browser(
                    log_callback=log_fn, force_full=True, reason=msg
                )
                pace_sleep(0.6, stop_fn)
                continue
            raise
    if not mail_ok:
        raise Exception("验证码阶段失败，已达到最大重试次数")
    log_fn(f"[*] 验证码: {code}")
    log_fn("[*] 4. 填写资料")
    t_profile = time.time()
    profile = fill_profile_and_submit(
        log_callback=log_fn,
        cancel_callback=stop_fn,
        email=email,
        email_code=code,
    )
    log_fn(
        f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}"
        f" via={profile.get('created_via', 'ui')} (+{time.time() - t_profile:.1f}s)"
    )
    sso = str(profile.get("sso") or "").strip()
    if not sso:
        log_fn("[*] 5. 等待 sso cookie")
        sso = wait_for_sso_cookie(
            log_callback=log_fn, cancel_callback=stop_fn
        )
    else:
        log_fn("[*] 5. sso 已由协议建号返回")
    _cpa_page = _get_page()
    cpa_result = None
    if config.get("cpa_export_enabled", True):
        cpa_async = bool(config.get("cpa_mint_async", True))
        if cpa_async:
            log_fn("[*] 6. 登录获取 access/refresh token (异步)")
            pwd = profile.get("password", "")

            def _cpa_mint_bg():
                time.sleep(2)
                try:
                    r = export_cpa_xai_for_account(
                        email, pwd, sso=sso, log_callback=log_fn, page=None
                    )
                    if r.get("ok"):
                        log_fn(f"[+] OAuth 导出成功: {r.get('path', '')}")
                    elif not r.get("skipped"):
                        log_fn(f"[!] OAuth 导出失败: {r.get('error', '未知错误')}")
                    save_account_bundle(
                        email=email,
                        password=pwd,
                        sso=sso,
                        log_callback=log_fn,
                        cpa_result=r,
                        profile=profile,
                        write_accounts_line=False,
                        write_sso_token=False,
                    )
                except Exception as e:
                    log_fn(f"[!] OAuth 导出异常: {e}")

            _t = threading.Thread(target=_cpa_mint_bg, daemon=True)
            _t.start()
            _track_cpa_async_thread(_t)
        else:
            log_fn("[*] 6. 登录获取 access/refresh token (同步)")
            cpa_result = export_cpa_xai_for_account(
                email,
                profile.get("password", ""),
                sso=sso,
                log_callback=log_fn,
                page=_cpa_page,
            )
            if cpa_result.get("ok"):
                log_fn(f"[+] OAuth 导出成功: {cpa_result.get('path', '')}")
            elif not cpa_result.get("skipped"):
                log_fn(f"[!] OAuth 导出失败: {cpa_result.get('error', '未知错误')}")
    if config.get("enable_nsfw", True):
        log_fn("[*] 7. 开启 NSFW")
        nsfw_ok, nsfw_msg = enable_nsfw_for_token(sso, log_callback=log_fn)
        if nsfw_ok:
            log_fn(f"[+] NSFW 开启成功: {nsfw_msg}")
        else:
            log_fn(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
    save_account_bundle(
        email=email,
        password=profile.get("password", ""),
        sso=sso,
        accounts_output_file=accounts_output_file,
        log_callback=log_fn,
        cpa_result=cpa_result,
        profile=profile,
    )
    add_token_to_grok2api_pools(sso, email=email, log_callback=log_fn)
    log_fn(f"[+] 注册成功: {email} | 总耗时 {time.time() - t_account:.1f}s")


def _cli_worker_loop(
    worker_id,
    task_queue,
    total_count,
    controller,
    accounts_output_file,
    stats,
    unlimited=False,
):
    _set_worker_id(worker_id)
    prefix = f"[W{worker_id}]"
    log_fn = lambda msg: cli_log(f"{prefix} {msg}")
    try:
        start_browser(log_callback=log_fn)
        log_fn(f"[*] Worker-{worker_id} 浏览器已启动")
    except Exception as e:
        log_fn(f"[!] Worker-{worker_id} 浏览器启动失败: {e}")
        return
    restart_every = int(config.get("browser_restart_every", 10) or 0)
    local_success = 0
    local_attempts = 0
    max_slot_retry = 3
    try:
        while not controller.should_stop():
            if not unlimited:
                try:
                    task_queue.get_nowait()
                except Exception:
                    break
            slot_done = False
            retry_count_for_slot = 0
            while not slot_done and not controller.should_stop():
                try:
                    _register_one_account_cli(log_fn, controller.should_stop, accounts_output_file)
                    with stats["lock"]:
                        stats["success"] += 1
                        local_success += 1
                    slot_done = True
                except RegistrationCancelled:
                    return
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        log_fn(
                            f"[!] 账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                        prepare_next_account_browser(
                            log_callback=log_fn, force_full=True, reason=str(exc)
                        )
                        continue
                    with stats["lock"]:
                        stats["fail"] += 1
                    log_fn(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
                    slot_done = True
                except Exception as exc:
                    with stats["lock"]:
                        stats["fail"] += 1
                    log_fn(f"[-] 注册失败: {exc}")
                    slot_done = True
                finally:
                    local_attempts += 1
                    if controller.should_stop():
                        break
                    if _get_browser() is None:
                        start_browser(log_callback=log_fn)
                    else:
                        force_full = (
                            restart_every > 0 and local_attempts % restart_every == 0
                        )
                        if force_full:
                            log_fn(
                                f"[*] Worker-{worker_id} 已处理 {local_attempts} 个账号，周期完整重启"
                            )
                        prepare_next_account_browser(
                            log_callback=log_fn,
                            force_full=force_full,
                            reason="账号间隔",
                        )
                    pace_sleep(0.5, controller.should_stop)
    finally:
        stop_browser()


def run_registration_cli(count, unlimited=False):
    """CLI 注册主循环。

    count: 目标数量（unlimited=True 时忽略）
    unlimited: True 时一直注册，直到 Ctrl+C
    """
    controller = CliStopController()
    prev_handler = _install_cli_sigint_handler(controller)
    accounts_output_file = get_accounts_output_path()
    worker_count = max(1, int(config.get("concurrent_count", 1) or 1))
    stats = {"success": 0, "fail": 0, "lock": threading.Lock()}
    stop_speed = threading.Event()
    interval = float(config.get("speed_log_interval_sec", 60) or 60)
    # count<=0 也视为不限量
    if not unlimited and int(count or 0) <= 0:
        unlimited = True
    if unlimited:
        count = 0
    count_label = "不限量(∞)" if unlimited else str(count)

    def _cli_counts():
        with stats["lock"]:
            return stats["success"], stats["fail"]

    speed_thread, _meter = start_speed_logger(
        get_counts=_cli_counts,
        log_callback=cli_log,
        stop_event=stop_speed,
        interval_sec=interval,
    )
    cli_log(f"[*] 终端模式启动，目标数量: {count_label}，并发: {worker_count}")
    cli_log(f"[*] 输出目录: {get_output_dir()}")
    cli_log(f"[*] 账号汇总: {accounts_output_file}")
    cli_log(f"[*] tokens: {get_tokens_file_path()}")
    cli_log(f"[*] OAuth auths: {get_cpa_auth_dir()}")
    cli_log(f"[*] full JSON: {os.path.join(get_output_dir(), 'full')}")
    cli_log(
        f"[*] 引擎={get_browser_engine()} headless={bool(config.get('browser_headless', True))} "
        f"mode={get_register_mode()} restart={get_browser_restart_mode()} "
        f"speed={is_speed_mode()} pool={get_mail_pool_size()}"
    )
    cli_log(f"[*] 日志级别: {get_log_level()} | 速度统计间隔: {int(interval)}s")
    cli_log("[*] 按 Ctrl+C 停止（连按两次强制退出）")
    start_mail_pool(log_callback=cli_log)
    try:
        if worker_count > 1:
            import queue
            task_queue = queue.Queue()
            if not unlimited:
                for idx in range(count):
                    task_queue.put(idx)
            threads = []
            for wid in range(worker_count):
                if controller.should_stop():
                    break
                t = threading.Thread(
                    target=_cli_worker_loop,
                    args=(
                        wid,
                        task_queue,
                        count,
                        controller,
                        accounts_output_file,
                        stats,
                        unlimited,
                    ),
                    daemon=True,
                )
                t.start()
                threads.append(t)
                # 可中断的启动间隔
                sleep_with_cancel(2, controller.should_stop)
            _join_threads_interruptible(
                threads,
                should_stop=controller.should_stop,
                timeout=None,
                poll=0.5,
            )
            if controller.should_stop():
                cli_log("[!] 已请求停止，等待 worker 收尾...")
                _join_threads_interruptible(
                    threads,
                    should_stop=None,
                    timeout=5,
                    poll=0.5,
                )
        else:
            start_browser(log_callback=cli_log)
            cli_log("[*] 浏览器已启动")
            restart_every = int(config.get("browser_restart_every", 10) or 0)
            i = 0
            retry_count_for_slot = 0
            max_slot_retry = 3
            while unlimited or i < count:
                if controller.should_stop():
                    break
                slot_label = f"{i + 1}/∞" if unlimited else f"{i + 1}/{count}"
                cli_log(f"--- 开始第 {slot_label} 个账号 ---")
                try:
                    _register_one_account_cli(cli_log, controller.should_stop, accounts_output_file)
                    with stats["lock"]:
                        stats["success"] += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[*] 当前统计: 成功 {stats['success']} | 失败 {stats['fail']}")
                    if restart_every > 0 and i > 0 and i % restart_every == 0:
                        cli_log(f"[*] 已注册 {i} 个账号，周期完整重启浏览器")
                        restart_browser(log_callback=cli_log)
                    if (
                        stats["success"] > 0
                        and stats["success"] % MEMORY_CLEANUP_INTERVAL == 0
                        and (unlimited or i < count)
                    ):
                        cleanup_runtime_memory(
                            log_callback=cli_log,
                            reason=f"已成功 {stats['success']} 个账号，执行定期清理",
                        )
                except RegistrationCancelled:
                    cli_log("[!] 注册被停止")
                    break
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        cli_log(
                            f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                    else:
                        with stats["lock"]:
                            stats["fail"] += 1
                        retry_count_for_slot = 0
                        i += 1
                        cli_log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
                except Exception as exc:
                    with stats["lock"]:
                        stats["fail"] += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 注册失败: {exc}")
                finally:
                    if controller.should_stop():
                        break
                    # 有限量：最后一号完成后不必再软重置，直接收尾
                    if not unlimited and i >= count:
                        break
                    if _get_browser() is None:
                        start_browser(log_callback=cli_log)
                    else:
                        prepare_next_account_browser(
                            log_callback=cli_log,
                            force_full=False,
                            reason="账号间隔",
                        )
                    pace_sleep(0.25, controller.should_stop)
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 KeyboardInterrupt，正在停止并清理")
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        stop_mail_pool()
        stop_speed.set()
        try:
            speed_thread.join(timeout=2)
        except Exception:
            pass
        stopping = controller.should_stop()
        controller.stop()
        _wait_cpa_async_threads(
            timeout=5 if stopping else 300,
            log_callback=cli_log,
            skip_if_stopping=(lambda: stopping),
        )
        try:
            stop_browser()
        except Exception:
            pass
        try:
            cleanup_runtime_memory(log_callback=cli_log, reason="任务结束")
        except Exception as clean_exc:
            cli_log(f"[Debug] 结束清理异常: {clean_exc}")
        _restore_sigint_handler(prev_handler)
        with stats["lock"]:
            ok, bad = stats["success"], stats["fail"]
        cli_log(f"[*] 任务结束。成功 {ok} | 失败 {bad}")


_UNLIMITED_TOKENS = frozenset(
    {"0", "forever", "loop", "infinite", "unlimited", "inf", "∞"}
)


def _parse_cli_count_args(argv_tail):
    """解析 CLI 数量参数。返回 (count, unlimited)。"""
    count = int(config.get("register_count", 1) or 1)
    unlimited = False
    for a in argv_tail:
        raw = str(a).strip().lstrip("-").lower()
        if raw in _UNLIMITED_TOKENS:
            unlimited = True
            count = 0
            break
        if raw.isdigit():
            n = int(raw)
            if n <= 0:
                unlimited = True
                count = 0
            else:
                count = n
                config["register_count"] = count
            break
    return count, unlimited


def _print_cli_banner(mode: str, count: int, unlimited: bool) -> None:
    """启动前打印美化横幅；失败则静默跳过。"""
    try:
        import importlib.util

        banner_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scripts", "launch_banner.py"
        )
        if not os.path.isfile(banner_path):
            return
        spec = importlib.util.spec_from_file_location("launch_banner", banner_path)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        banner_mode = "loop" if unlimited else "start"
        count_override = "" if unlimited else str(count)
        mod.print_banner(banner_mode, count_override, launching=True)
    except Exception:
        pass


def main_cli():
    """CLI 入口。

    - python grok_register_ttk.py start [count]     直接开始
    - python grok_register_ttk.py start 0|forever  不限量一直注册
    - python grok_register_ttk.py loop|forever     不限量一直注册
    - python grok_register_ttk.py cli              交互确认后再开始
    """
    load_config()
    argv = [str(a).strip() for a in sys.argv[1:] if str(a).strip()]
    mode = (argv[0].lower() if argv else "cli")
    count, unlimited = _parse_cli_count_args(argv[1:])
    # 一级子命令直接表示不限量
    if mode in ("loop", "forever", "--loop", "--forever"):
        unlimited = True
        count = 0
        mode = "start"
    count_label = "不限量(∞)" if unlimited else str(count)

    # start / --start / loop：bat 或脚本一键直跑
    if mode in ("start", "--start"):
        _print_cli_banner(mode, count, unlimited)
        cli_log("[*] CLI 已加载配置")
        cli_log(
            f"[*] 邮箱={config.get('email_provider', 'duckmail')} | "
            f"数量={count_label} | 并发={int(config.get('concurrent_count', 1) or 1)} | "
            f"CPA_API={'on' if (config.get('cpa_api_base') and config.get('cpa_api_key')) else 'off'}"
        )
        if unlimited:
            cli_log("[*] 不限量模式：将一直注册，直到 Ctrl+C 停止")
        else:
            cli_log("[*] 自动开始注册；Ctrl+C 强制停止")
        run_registration_cli(count, unlimited=unlimited)
        return

    cli_log("[*] CLI 已加载配置")
    cli_log(
        f"[*] 邮箱={config.get('email_provider', 'duckmail')} | "
        f"数量={count_label} | 并发={int(config.get('concurrent_count', 1) or 1)} | "
        f"CPA_API={'on' if (config.get('cpa_api_base') and config.get('cpa_api_key')) else 'off'}"
    )
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    _print_cli_banner(mode, count, unlimited)
    run_registration_cli(count, unlimited=unlimited)


def main():
    """CLI-only entry. Default: interactive cli; or start/loop/forever."""
    if len(sys.argv) <= 1:
        # no args -> interactive CLI (type start)
        sys.argv.append("cli")
    main_cli()


if __name__ == "__main__":
    main()
