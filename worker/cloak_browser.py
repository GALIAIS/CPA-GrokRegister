"""CloakBrowser adapter with a small DrissionPage-compatible surface.

Used by the registration flow so existing run_js / get / cookies / ele
call sites can run on CloakBrowser (Playwright API) with minimal rewrites.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import threading
import time
from typing import Any, Optional


class PageDisconnectedError(Exception):
    """Raised when the underlying Playwright page is gone."""


def _selector_to_css(selector: str) -> str:
    """Translate a subset of DrissionPage locators to CSS."""
    s = str(selector or "").strip()
    if not s:
        return "*"
    if s.startswith("@") and "=" in s:
        # @name=value  /  @id=x
        key, _, val = s[1:].partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        return f'[{key}="{val}"]'
    if s.lower().startswith("tag:"):
        return s.split(":", 1)[1].strip() or "*"
    if s.startswith("css:"):
        return s[4:].strip()
    if s.startswith("text:"):
        # Playwright text= engine is separate; keep as :text for our helper
        return s
    return s


class _WaitProxy:
    def __init__(self, page: "CloakPage"):
        self._page = page

    def doc_loaded(self, timeout: float = 30) -> bool:
        try:
            self._page._pw.wait_for_load_state(
                "domcontentloaded", timeout=int(timeout * 1000)
            )
            return True
        except Exception as exc:
            raise PageDisconnectedError(str(exc)) from exc


class CloakElement:
    """Minimal element wrapper for Turnstile / form helpers."""

    def __init__(self, page: "CloakPage", handle: Any, selector: str = ""):
        self._page = page
        self._handle = handle
        self._selector = selector

    def parent(self) -> Optional["CloakElement"]:
        try:
            handle = self._handle.evaluate_handle("el => el.parentElement")
            if handle is None:
                return None
            # evaluate_handle may return JSHandle; try as_element
            el = handle.as_element() if hasattr(handle, "as_element") else handle
            if el is None:
                return None
            return CloakElement(self._page, el, "parent")
        except Exception:
            return None

    @property
    def shadow_root(self) -> Optional["CloakElement"]:
        try:
            handle = self._handle.evaluate_handle("el => el.shadowRoot")
            el = handle.as_element() if hasattr(handle, "as_element") else handle
            if el is None:
                return None
            return CloakElement(self._page, el, "shadow")
        except Exception:
            return None

    def ele(self, selector: str, timeout: float = 0.5) -> Optional["CloakElement"]:
        css = _selector_to_css(selector)
        try:
            # Query within this element
            if css.startswith("text:"):
                text = css[5:]
                loc = self._handle.locator(f"text={text}").first
            else:
                loc = self._handle.locator(css).first
            loc.wait_for(state="attached", timeout=int(max(timeout, 0.1) * 1000))
            handle = loc.element_handle(timeout=int(max(timeout, 0.1) * 1000))
            if handle is None:
                return None
            return CloakElement(self._page, handle, css)
        except Exception:
            return None

    def click(self, by_js: bool = False) -> None:
        try:
            if by_js:
                self._handle.evaluate("el => el.click()")
            else:
                self._handle.click(timeout=3000)
        except Exception:
            try:
                self._handle.evaluate("el => el.click()")
            except Exception:
                pass

    def run_js(self, script: str, *args: Any) -> Any:
        body = str(script or "").strip()
        if args:
            return self._handle.evaluate(
                f"(el, ...args) => {{ {body} }}", list(args)
            )
        return self._handle.evaluate(f"(el) => {{ {body} }}")


class CloakPage:
    """DrissionPage-like page API over Playwright Page."""

    def __init__(self, pw_page: Any, browser: "CloakBrowser"):
        self._pw = pw_page
        self.browser = browser
        self.wait = _WaitProxy(self)
        self._speed_applied = False
        try:
            # Tighter default action timeout for faster fail/retry loops
            self._pw.set_default_timeout(int(getattr(browser, "action_timeout_ms", 12000) or 12000))
        except Exception:
            pass
        if getattr(browser, "block_heavy_assets", False) or getattr(
            browser, "speed_mode", False
        ):
            if getattr(browser, "block_heavy_assets", True):
                self.apply_speed_tweaks()

    def apply_speed_tweaks(self) -> None:
        """Block heavy static assets (keep scripts/XHR for Turnstile/CF)."""
        if self._speed_applied:
            return
        self._speed_applied = True
        pw = self._pw

        def _handler(route: Any) -> None:
            try:
                req = route.request
                rtype = str(getattr(req, "resource_type", "") or "")
                url = str(getattr(req, "url", "") or "").lower()
                # Never block CF / turnstile / auth critical hosts
                if any(
                    k in url
                    for k in (
                        "cloudflare",
                        "turnstile",
                        "challenges.",
                        "accounts.x.ai",
                        "auth.x.ai",
                        "grok.com",
                        "x.ai",
                    )
                ):
                    route.continue_()
                    return
                if rtype in ("image", "media", "font"):
                    route.abort()
                    return
                route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            pw.route("**/*", _handler)
        except Exception:
            self._speed_applied = False

    @property
    def url(self) -> str:
        try:
            return str(self._pw.url or "")
        except Exception as exc:
            raise PageDisconnectedError(str(exc)) from exc

    @property
    def html(self) -> str:
        try:
            return str(self._pw.content() or "")
        except Exception as exc:
            raise PageDisconnectedError(str(exc)) from exc

    def get(self, url: str, **kwargs: Any) -> Any:
        timeout_ms = int(float(kwargs.get("timeout", 45)) * 1000)
        # commit is faster; fall back to domcontentloaded on flaky navigations
        wait_until = kwargs.get("wait_until")
        if not wait_until:
            wait_until = "commit" if getattr(self.browser, "speed_mode", False) else "domcontentloaded"
        try:
            self._pw.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return self
        except Exception as exc:
            msg = str(exc).lower()
            if wait_until == "commit":
                try:
                    self._pw.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    return self
                except Exception as exc2:
                    exc = exc2
                    msg = str(exc2).lower()
            if "about:blank" in str(url):
                try:
                    self._pw.goto("about:blank", wait_until="commit", timeout=8000)
                    return self
                except Exception as exc2:
                    raise PageDisconnectedError(str(exc2)) from exc2
            if "target closed" in msg or "has been closed" in msg:
                raise PageDisconnectedError(str(exc)) from exc
            raise

    def run_js(self, script: str, *args: Any) -> Any:
        """Execute JS like DrissionPage: classic function body + arguments[n]."""
        body = str(script or "").strip()
        if not body:
            return None
        # DrissionPage injects args as classic-function `arguments`; keep that contract.
        runner = """
(args) => {
  return (function () {
    %s
  }).apply(null, args || []);
}
""" % body
        try:
            return self._pw.evaluate(runner, list(args) if args else [])
        except Exception as exc:
            msg = str(exc).lower()
            if "target closed" in msg or "has been closed" in msg:
                raise PageDisconnectedError(str(exc)) from exc
            # Fallback: expression form without wrapper (rare)
            try:
                if args:
                    return self._pw.evaluate(body, args[0] if len(args) == 1 else list(args))
                return self._pw.evaluate(body)
            except Exception as exc2:
                raise PageDisconnectedError(str(exc2)) from exc2

    def cookies(
        self, all_domains: bool = True, all_info: bool = True
    ) -> list[dict[str, Any]]:
        try:
            ctx = self._pw.context
            raw = ctx.cookies()
            out: list[dict[str, Any]] = []
            for c in raw or []:
                if isinstance(c, dict):
                    out.append(dict(c))
            return out
        except Exception as exc:
            raise PageDisconnectedError(str(exc)) from exc

    def set_cookies(self, cookies: Any) -> None:
        """If cookies is False/None, clear all. If list, add them."""
        try:
            ctx = self._pw.context
            if cookies is False or cookies is None:
                ctx.clear_cookies()
                return
            if isinstance(cookies, list):
                # Playwright expects url or domain+path
                normalized = []
                for c in cookies:
                    if not isinstance(c, dict):
                        continue
                    item = {
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": c.get("domain") or ".x.ai",
                        "path": c.get("path") or "/",
                    }
                    if c.get("url"):
                        item = {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "url": c.get("url"),
                        }
                    if item.get("name"):
                        normalized.append(item)
                if normalized:
                    ctx.add_cookies(normalized)
        except Exception:
            pass

    def ele(self, selector: str, timeout: float = 1.0) -> Optional[CloakElement]:
        css = _selector_to_css(selector)
        try:
            if css.startswith("text:"):
                loc = self._pw.locator(f"text={css[5:]}").first
            else:
                loc = self._pw.locator(css).first
            loc.wait_for(state="attached", timeout=int(max(timeout, 0.1) * 1000))
            handle = loc.element_handle(timeout=int(max(timeout, 0.1) * 1000))
            if handle is None:
                return None
            return CloakElement(self, handle, css)
        except Exception:
            return None

    def get_screenshot(self, path: str | None = None, **kwargs: Any) -> Any:
        try:
            if path:
                self._pw.screenshot(path=path, full_page=bool(kwargs.get("full_page", False)))
                return path
            return self._pw.screenshot(full_page=bool(kwargs.get("full_page", False)))
        except Exception as exc:
            raise PageDisconnectedError(str(exc)) from exc

    def read_turnstile_token(self) -> str:
        """Read cf-turnstile-response / turnstile.getResponse if present."""
        try:
            token = self._pw.evaluate(
                """() => {
  try {
    const byInput = String(
      (document.querySelector('input[name="cf-turnstile-response"]') || {}).value || ''
    ).trim();
    if (byInput) return byInput;
    if (window.turnstile && typeof turnstile.getResponse === 'function') {
      return String(turnstile.getResponse() || '').trim();
    }
    const any = document.querySelector('textarea[name="cf-turnstile-response"], input[name*="turnstile"]');
    if (any && any.value) return String(any.value).trim();
    return '';
  } catch (e) { return ''; }
}"""
            )
            return str(token or "").strip()
        except Exception:
            return ""

    def _cdp_click(self, x: float, y: float) -> bool:
        """Trusted mouse click via CDP Input domain (isTrusted=true)."""
        try:
            session = self._pw.context.new_cdp_session(self._pw)
            try:
                for etype in ("mouseMoved", "mousePressed", "mouseReleased"):
                    params = {
                        "type": etype,
                        "x": float(x),
                        "y": float(y),
                        "button": "left",
                        "buttons": 1 if etype != "mouseReleased" else 0,
                        "clickCount": 1,
                    }
                    if etype == "mouseMoved":
                        params.pop("button", None)
                        params.pop("buttons", None)
                        params.pop("clickCount", None)
                    session.send("Input.dispatchMouseEvent", params)
                    time.sleep(random.uniform(0.02, 0.06))
                return True
            finally:
                try:
                    session.detach()
                except Exception:
                    pass
        except Exception:
            return False

    def _human_mouse_click(self, x: float, y: float) -> bool:
        """CDP trusted click first; then Cloak humanized mouse."""
        if self._cdp_click(x, y):
            return True
        try:
            # Keep steps low so careful preset does not take seconds per click
            self._pw.mouse.move(x, y, steps=4)
            time.sleep(random.uniform(0.04, 0.12))
            self._pw.mouse.click(x, y, delay=random.randint(30, 90))
            return True
        except TypeError:
            try:
                self._pw.mouse.move(x, y)
                self._pw.mouse.click(x, y)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _iter_turnstile_boxes(self) -> list[dict[str, float]]:
        """Collect bounding boxes for turnstile widgets / challenge iframes."""
        pw = self._pw
        boxes: list[dict[str, float]] = []

        # From Playwright frames (most reliable for CF challenge URL)
        try:
            for frame in list(pw.frames or []):
                url = str(getattr(frame, "url", "") or "")
                if not any(
                    k in url
                    for k in (
                        "challenges.cloudflare.com",
                        "turnstile",
                        "cf-chl",
                        "cdn-cgi/challenge",
                    )
                ):
                    continue
                try:
                    el = frame.frame_element()
                    if el is None:
                        continue
                    try:
                        el.scroll_into_view_if_needed(timeout=800)
                    except Exception:
                        pass
                    box = el.bounding_box()
                    if box and box.get("width", 0) >= 16 and box.get("height", 0) >= 16:
                        boxes.append(box)
                except Exception:
                    continue
        except Exception:
            pass

        # From host DOM selectors (instant count, no long waits)
        selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[src*="cf-chl"]',
            'iframe[src*="cdn-cgi/challenge"]',
            "div.cf-turnstile iframe",
            "[data-sitekey] iframe",
            "div.cf-turnstile",
            "[data-sitekey]",
        ]
        for sel in selectors:
            try:
                loc = pw.locator(sel)
                n = int(loc.count() or 0)
                for i in range(min(n, 3)):
                    item = loc.nth(i)
                    try:
                        item.scroll_into_view_if_needed(timeout=500)
                    except Exception:
                        pass
                    box = item.bounding_box()
                    if box and box.get("width", 0) >= 16 and box.get("height", 0) >= 16:
                        boxes.append(box)
            except Exception:
                continue

        # de-dup roughly by integer coords
        uniq: list[dict[str, float]] = []
        seen = set()
        for b in boxes:
            key = (int(b["x"]), int(b["y"]), int(b["width"]), int(b["height"]))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(b)
        return uniq

    def click_turnstile_widget(self, log: Any = None, aggressive: bool = False) -> str:
        """Fast real pointer click on managed Turnstile checkbox zone."""
        pw = self._pw
        actions: list[str] = []
        boxes = self._iter_turnstile_boxes()
        if not boxes:
            try:
                n = int(pw.locator("iframe").count() or 0)
                for i in range(min(n, 8)):
                    item = pw.locator("iframe").nth(i)
                    box = item.bounding_box()
                    if not box:
                        continue
                    if box["width"] < 120 or box["height"] < 40 or box["height"] > 120:
                        continue
                    boxes.append(box)
            except Exception:
                pass

        if not boxes:
            return "no-target"

        for box in boxes:
            # First hit the classic checkbox hot-zone; escalate only if aggressive
            offsets = [
                (28, 0.50),
                (24, 0.52),
                (32, 0.48),
            ]
            if aggressive:
                offsets.extend([(36, 0.50), (20, 0.50), (40, 0.55)])
            for ox, oy in offsets:
                x = box["x"] + ox
                y = box["y"] + box["height"] * oy
                if self._human_mouse_click(x, y):
                    actions.append(f"box-click@{int(x)},{int(y)}")
                    time.sleep(random.uniform(0.08, 0.18))
                    # After first good click, wait briefly for token instead of spam
                    if not aggressive:
                        break
            if actions:
                break

        # Frame-local checkbox (short timeouts only)
        try:
            for frame in list(pw.frames or []):
                url = str(getattr(frame, "url", "") or "")
                if not any(
                    k in url for k in ("challenges.cloudflare.com", "turnstile", "cf-chl")
                ):
                    continue
                positions = ((26, 30), (30, 32)) if not aggressive else (
                    (26, 30),
                    (30, 32),
                    (22, 28),
                    (28, 28),
                )
                for pos in positions:
                    try:
                        frame.click(
                            "body",
                            position={"x": pos[0], "y": pos[1]},
                            force=True,
                            timeout=350,
                        )
                        actions.append(f"frame-pos:{pos[0]},{pos[1]}")
                        if not aggressive:
                            break
                    except Exception:
                        continue
                for sel in ('input[type="checkbox"]', "label", ".cb-lb"):
                    try:
                        frame.click(sel, force=True, timeout=350)
                        actions.append(f"frame-sel:{sel}")
                        break
                    except Exception:
                        continue
                if actions and not aggressive:
                    break
        except Exception:
            pass

        return "|".join(actions[:6]) if actions else "no-click"

    def solve_turnstile(
        self,
        *,
        timeout: float = 45,
        poll_interval: float = 0.45,
        log: Any = None,
        cancel: Any = None,
        min_token_len: int = 80,
    ) -> str:
        """Wait for Turnstile token; actively pointer-click managed checkbox.

        Does NOT call turnstile.reset() — resetting often forces interactive mode.
        Optimized for headless: wait for widget mount, then 1–2 precise clicks
        before escalating to multi-hit.
        """
        deadline = time.time() + max(timeout, 5)
        last_click_at = 0.0
        click_count = 0
        speed = bool(getattr(self.browser, "speed_mode", False))
        # Allow silent/non-interactive score a short window first
        first_click_after = time.time() + (0.25 if speed else 0.8)
        # Wait up to ~4s for iframe to mount before hammering
        widget_wait_deadline = time.time() + (2.2 if speed else 4.0)
        click_gap = 0.55 if speed else 0.95
        poll_interval = min(float(poll_interval or 0.45), 0.28 if speed else 0.45)

        while time.time() < deadline:
            if cancel and cancel():
                raise PageDisconnectedError("cancelled")

            token = self.read_turnstile_token()
            if len(token) >= min_token_len:
                if log:
                    log(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            now = time.time()
            boxes = self._iter_turnstile_boxes()
            if not boxes and now < widget_wait_deadline:
                time.sleep(0.15 if speed else 0.25)
                continue

            if now >= first_click_after and now - last_click_at >= click_gap:
                aggressive = click_count >= (1 if speed else 2)
                status = self.click_turnstile_widget(log=log, aggressive=aggressive)
                click_count += 1
                last_click_at = now
                if log and (click_count <= 6 or click_count % 3 == 0):
                    log(f"[*] Turnstile 自动点击 #{click_count}: {status}")
                # Post-click settle: CF scoring often finishes in 0.5–2s
                settle_end = time.time() + (
                    (1.05 if click_count <= 2 else 0.55)
                    if speed
                    else (1.6 if click_count <= 2 else 0.9)
                )
                while time.time() < settle_end and time.time() < deadline:
                    token = self.read_turnstile_token()
                    if len(token) >= min_token_len:
                        if log:
                            log(f"[*] Turnstile 已通过，token长度={len(token)}")
                        return token
                    time.sleep(0.12 if speed else 0.2)
            else:
                time.sleep(poll_interval)

        raise Exception(
            f"Turnstile 在 {int(timeout)}s 内未拿到 token（已自动点击 {click_count} 次）"
        )


class CloakBrowser:
    """Browser/context wrapper with Drission-like tab helpers."""

    def __init__(
        self,
        context: Any,
        user_data_path: str | None = None,
        playwright_browser: Any = None,
        speed_mode: bool = True,
        block_heavy_assets: bool = True,
        action_timeout_ms: int = 12000,
    ):
        self._context = context
        self._pw_browser = playwright_browser
        self.user_data_path = user_data_path
        self.speed_mode = bool(speed_mode)
        self.block_heavy_assets = bool(block_heavy_assets)
        self.action_timeout_ms = int(action_timeout_ms or 12000)
        self._pages: list[CloakPage] = []
        self._lock = threading.Lock()
        # Seed existing pages
        try:
            for p in list(getattr(context, "pages", []) or []):
                self._pages.append(CloakPage(p, self))
        except Exception:
            pass
        if not self._pages:
            try:
                pw = context.new_page()
                self._pages.append(CloakPage(pw, self))
            except Exception:
                pass

    def get_tabs(self) -> list[CloakPage]:
        with self._lock:
            # Resync with live context pages
            live = []
            try:
                for p in list(self._context.pages or []):
                    wrapped = next((w for w in self._pages if w._pw is p), None)
                    if wrapped is None:
                        wrapped = CloakPage(p, self)
                    live.append(wrapped)
            except Exception:
                live = list(self._pages)
            self._pages = live
            return list(self._pages)

    def new_tab(self, url: str = "about:blank") -> CloakPage:
        with self._lock:
            pw = self._context.new_page()
            page = CloakPage(pw, self)
            self._pages.append(page)
            if url:
                try:
                    page.get(url)
                except Exception:
                    pass
            return page

    @property
    def latest_tab(self) -> Optional[CloakPage]:
        tabs = self.get_tabs()
        return tabs[-1] if tabs else None

    def set_cookies(self, cookies: Any) -> None:
        page = self.latest_tab
        if page is not None:
            page.set_cookies(cookies)
        elif cookies is False or cookies is None:
            try:
                self._context.clear_cookies()
            except Exception:
                pass

    def cookies(self) -> list[dict[str, Any]]:
        page = self.latest_tab
        if page is not None:
            return page.cookies()
        try:
            return list(self._context.cookies() or [])
        except Exception:
            return []

    def soft_reset(self) -> "CloakPage":
        """Ultra-fast session cleanup: clear cookies + fresh page (keep Chromium)."""
        try:
            self._context.clear_cookies()
        except Exception:
            pass
        # Prefer a brand-new page (avoids residual SPA state / service workers)
        try:
            old_pages = list(self._context.pages or [])
            fresh = self._context.new_page()
            page = CloakPage(fresh, self)
            for p in old_pages:
                try:
                    p.close()
                except Exception:
                    pass
            try:
                fresh.goto("about:blank", wait_until="commit", timeout=5000)
            except Exception:
                pass
            self._pages = [page]
            return page
        except Exception:
            try:
                pages = list(self._context.pages or [])
                keep = pages[0] if pages else self._context.new_page()
                for p in pages[1:]:
                    try:
                        p.close()
                    except Exception:
                        pass
                try:
                    keep.goto("about:blank", wait_until="commit", timeout=5000)
                except Exception:
                    pass
                try:
                    keep.evaluate(
                        "() => { try{localStorage.clear()}catch(e){} try{sessionStorage.clear()}catch(e){} }"
                    )
                except Exception:
                    pass
                page = CloakPage(keep, self)
                self._pages = [page]
                return page
            except Exception as exc:
                raise PageDisconnectedError(f"soft_reset failed: {exc}") from exc

    def quit(self, del_data: bool = True) -> None:
        path = self.user_data_path
        try:
            self._context.close()
        except Exception:
            pass
        if self._pw_browser is not None:
            try:
                self._pw_browser.close()
            except Exception:
                pass
        self._pages.clear()
        if del_data and path:
            try:
                root = os.path.abspath(
                    os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), ".browser_profiles"
                    )
                )
                abs_profile = os.path.abspath(str(path))
                if abs_profile.startswith(root) and os.path.isdir(abs_profile):
                    shutil.rmtree(abs_profile, ignore_errors=True)
            except Exception:
                pass


def _make_profile_dir(worker_id: int = 0) -> str:
    profile_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".browser_profiles"
    )
    os.makedirs(profile_root, exist_ok=True)
    return os.path.join(
        profile_root,
        f"cloak_w{worker_id}_{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000) % 1000000}",
    )


def start_cloak_browser(
    *,
    proxy: str = "",
    headless: bool = False,
    humanize: bool = True,
    human_preset: str = "default",
    user_agent: str | None = None,
    extension_paths: list[str] | None = None,
    worker_id: int = 0,
    locale: str = "en-US",
    timezone: str = "America/New_York",
    viewport: dict | None = None,
    speed_mode: bool = True,
    block_heavy_assets: bool = True,
    log_callback: Any = None,
) -> tuple[CloakBrowser, CloakPage]:
    """Launch CloakBrowser persistent context and return (browser, page).

    Headless notes:
    - Fixed 1920x1080 viewport (None viewport is headed-only / less reliable).
    - Prefer human_preset=default for speed; careful is slower and rarely helps headless CF.
    - humanize still on so page.click/mouse carry Bezier motion for Turnstile.
    """
    from cloakbrowser import launch_persistent_context

    profile_dir = _make_profile_dir(worker_id)
    headless = bool(headless)
    preset = human_preset if human_preset in ("default", "careful") else "default"
    # Headless: always pin a desktop viewport. Headed: real window (None) is fine.
    if headless:
        vp = viewport or {"width": 1920, "height": 1080}
    else:
        vp = viewport  # None => OS window

    extra_args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US",
    ]
    # Linux root / container / systemd: Chromium needs no-sandbox
    if os.name == "posix":
        extra_args.extend(
            [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ]
        )
    if headless:
        # New headless is less bot-like than old headless shell
        extra_args.extend(
            [
                "--window-size=1920,1080",
                "--force-device-scale-factor=1",
            ]
        )
    # Normalize proxy for Chromium (socks5h -> socks5)
    if proxy:
        pl = str(proxy).strip().lower()
        if pl.startswith("socks5h://"):
            proxy = "socks5://" + str(proxy).strip()[len("socks5h://") :]

    kwargs: dict[str, Any] = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "humanize": bool(humanize),
        "human_preset": preset,
        "stealth_args": True,
        "viewport": vp,
        "locale": locale or "en-US",
        "timezone": timezone or "America/New_York",
        "color_scheme": "light",
        "args": extra_args,
    }
    proxy = str(proxy or "").strip()
    if proxy:
        kwargs["proxy"] = proxy
        # Align timezone/locale with proxy exit when geoip available (optional dep)
        kwargs["geoip"] = True
    if user_agent:
        kwargs["user_agent"] = user_agent
    paths = [p for p in (extension_paths or []) if p and os.path.exists(p)]
    if paths:
        kwargs["extension_paths"] = paths

    if log_callback:
        log_callback(
            f"[*] CloakBrowser 启动中 (headless={headless}, humanize={humanize}, "
            f"preset={preset}, proxy={'on' if proxy else 'off'})..."
        )

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            # geoip may fail if package missing — retry without it
            try:
                context = launch_persistent_context(**kwargs)
            except Exception as geo_exc:
                if kwargs.get("geoip"):
                    kwargs["geoip"] = False
                    if log_callback:
                        log_callback(f"[Debug] geoip 不可用，回退: {geo_exc}")
                    context = launch_persistent_context(**kwargs)
                else:
                    raise
            browser = CloakBrowser(
                context=context,
                user_data_path=profile_dir,
                speed_mode=bool(speed_mode),
                block_heavy_assets=bool(block_heavy_assets),
                action_timeout_ms=10000 if speed_mode else 15000,
            )
            page = browser.latest_tab or browser.new_tab()
            # Headless warm-up: one blank navigation so fingerprint seed settles
            try:
                page.get("about:blank")
            except Exception:
                pass
            if log_callback and attempt > 1:
                log_callback(f"[*] CloakBrowser 第 {attempt} 次启动成功")
            if log_callback:
                log_callback(f"[Debug] CloakBrowser profile: {profile_dir}")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] CloakBrowser 启动失败(第{attempt}/3次): {exc}")
            time.sleep(min(1.2 * attempt, 3))
    raise Exception(f"CloakBrowser 启动失败: {last_exc}")
