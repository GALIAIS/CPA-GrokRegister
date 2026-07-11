#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-side control plane + real-time WebUI (SSE) for grok-register.

Endpoints:
  GET  /                 WebUI (SSE live status + logs + settings)
  GET  /health
  GET  /status?token=
  GET  /logs?token=&lines=
  GET  /config?token=    editable config (secrets masked)
  POST /config?token=    update config.json fields
  GET  /events?token=    SSE stream: snapshot / status / log / ping
  POST /start?token=     body: {"mode":"loop"|"start","count":N}
  POST /stop?token=

Env:
  REGISTER_CONTROL_HOST / PORT / TOKEN / UNIT / APP_DIR
  REGISTER_CONTROL_PUBLIC_URL  optional public base for panel EventSource
  REGISTER_SSE_INTERVAL_SEC    default 1.0
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("REGISTER_CONTROL_HOST", "0.0.0.0")
PORT = int(os.environ.get("REGISTER_CONTROL_PORT", "18927") or 18927)
TOKEN = str(os.environ.get("REGISTER_CONTROL_TOKEN", "") or "").strip()
UNIT = str(os.environ.get("REGISTER_UNIT", "grok-register") or "grok-register").strip()
APP_DIR = str(
    os.environ.get("REGISTER_APP_DIR", "/opt/grok-auto-register") or "/opt/grok-auto-register"
)
PUBLIC_URL = str(os.environ.get("REGISTER_CONTROL_PUBLIC_URL", "") or "").strip().rstrip("/")
SSE_INTERVAL = float(os.environ.get("REGISTER_SSE_INTERVAL_SEC", "1.0") or 1.0)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# Fields allowed to edit from WebUI / plugin panel
EDITABLE_FIELDS = [
    "email_provider",
    "shiromail_api_base",
    "shiromail_api_key",
    "shiromail_domain",
    "shiromail_expires_in_hours",
    "proxy",
    "proxy_bypass",
    "cpa_proxy",
    "cpa_api_base",
    "cpa_api_key",
    "cpa_export_enabled",
    "cpa_protocol_mint",
    "cpa_hotload_dir",
    "cpa_copy_to_hotload",
    "concurrent_count",
    "mail_pool_size",
    "register_mode",
    "browser_engine",
    "browser_headless",
    "log_level",
    "register_count",
]
SECRET_FIELDS = frozenset({"shiromail_api_key", "cpa_api_key", "yyds_api_key", "yyds_jwt"})

_lock = threading.Lock()
_log_cursor_lock = threading.Lock()
_log_cursor = {"boot_id": "", "cursor": ""}  # journalctl --cursor


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def unit_active() -> str:
    code, out, _ = _run(["systemctl", "is-active", UNIT], timeout=8)
    s = (out or "").strip() or "unknown"
    if code != 0 and s in ("", "unknown"):
        return "inactive"
    return s


def unit_status_text() -> str:
    _, out, err = _run(["systemctl", "status", UNIT, "--no-pager", "-l"], timeout=12)
    return (out or err or "").strip()


def unit_logs(lines: int = 80) -> str:
    n = max(10, min(int(lines or 80), 800))
    _, out, err = _run(
        ["journalctl", "-u", UNIT, "-n", str(n), "--no-pager", "-o", "cat"],
        timeout=15,
    )
    return (out or err or "").strip()


def unit_logs_since_cursor(max_lines: int = 200) -> tuple[str, str]:
    """Return (new_log_text, new_cursor). Uses journalctl cursor for incremental logs."""
    global _log_cursor
    n = max(20, min(int(max_lines or 200), 500))
    with _log_cursor_lock:
        cur = str(_log_cursor.get("cursor") or "")
    cmd = ["journalctl", "-u", UNIT, "--no-pager", "-o", "cat", "-n", str(n)]
    if cur:
        cmd = [
            "journalctl",
            "-u",
            UNIT,
            "--no-pager",
            "-o",
            "cat",
            "--after-cursor",
            cur,
            "-n",
            str(n),
        ]
    code, out, err = _run(cmd, timeout=12)
    text = (out or err or "").strip()
    # refresh cursor
    _, show, _ = _run(
        ["journalctl", "-u", UNIT, "-n", "1", "--no-pager", "--show-cursor"],
        timeout=8,
    )
    new_cur = cur
    for line in (show or "").splitlines():
        if line.startswith("-- cursor: "):
            new_cur = line.split("-- cursor: ", 1)[-1].strip()
            break
    with _log_cursor_lock:
        if new_cur:
            _log_cursor["cursor"] = new_cur
    # if first poll with cursor empty, avoid dumping entire history as "new"
    if not cur:
        return "", new_cur
    return text, new_cur


def count_auth_files() -> int:
    d = os.path.join(APP_DIR, "register_output", "auths")
    try:
        return sum(1 for n in os.listdir(d) if n.startswith("xai-") and n.endswith(".json"))
    except Exception:
        return 0


def count_full_accounts() -> int:
    d = os.path.join(APP_DIR, "register_output", "full")
    try:
        return sum(1 for n in os.listdir(d) if n.endswith(".json"))
    except Exception:
        return 0


def parse_success_fail_from_logs(text: str) -> dict:
    ok = fail = None
    for line in reversed((text or "").splitlines()):
        m = re.search(r"成功\s*(\d+)\s*\|\s*失败\s*(\d+)", line)
        if m:
            ok, fail = int(m.group(1)), int(m.group(2))
            break
        m2 = re.search(r"success[=\s]+(\d+).*fail[=\s]+(\d+)", line, re.I)
        if m2:
            ok, fail = int(m2.group(1)), int(m2.group(2))
            break
    return {"success": ok, "fail": fail}


def parse_last_email(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        m = re.search(r"已创建邮箱:\s*(\S+@\S+)", line)
        if m:
            return m.group(1)
        m = re.search(r"注册成功:\s*(\S+@\S+)", line)
        if m:
            return m.group(1)
    return ""


def _mask_secret(val: str) -> str:
    s = str(val or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * max(4, len(s) - 8) + s[-4:]


def load_config_raw() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def config_payload(*, reveal: bool = False) -> dict:
    raw = load_config_raw()
    fields = []
    values = {}
    for key in EDITABLE_FIELDS:
        val = raw.get(key, "")
        secret = key in SECRET_FIELDS
        if secret and not reveal:
            display = _mask_secret(str(val) if val is not None else "")
            set_flag = bool(str(val or "").strip())
        else:
            display = val
            set_flag = bool(str(val or "").strip()) if secret else None
        values[key] = display
        fields.append(
            {
                "key": key,
                "secret": secret,
                "type": "bool"
                if isinstance(val, bool)
                else ("number" if isinstance(val, (int, float)) and not isinstance(val, bool) else "string"),
                "set": set_flag,
            }
        )
    return {
        "ok": True,
        "path": CONFIG_PATH,
        "fields": fields,
        "values": values,
        "editable": EDITABLE_FIELDS,
        "ts": time.time(),
    }


def save_config_patch(patch: dict, *, restart_worker: bool = False) -> dict:
    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "empty patch"}
    with _lock:
        raw = load_config_raw()
        changed = []
        for key, val in patch.items():
            if key not in EDITABLE_FIELDS:
                continue
            # skip masked placeholders that user didn't change
            if key in SECRET_FIELDS and isinstance(val, str):
                if not val.strip():
                    # empty string clears secret
                    raw[key] = ""
                    changed.append(key)
                    continue
                if "*" in val and val == _mask_secret(str(raw.get(key) or "")):
                    continue
                if set(val) <= {"*"}:
                    continue
            # type coercion
            old = raw.get(key)
            if isinstance(old, bool) or key in (
                "browser_headless",
                "cpa_export_enabled",
                "cpa_protocol_mint",
                "cpa_copy_to_hotload",
            ):
                if isinstance(val, str):
                    raw[key] = val.strip().lower() in ("1", "true", "yes", "on")
                else:
                    raw[key] = bool(val)
            elif isinstance(old, int) and not isinstance(old, bool):
                try:
                    raw[key] = int(val)
                except Exception:
                    raw[key] = old
            elif isinstance(old, float):
                try:
                    raw[key] = float(val)
                except Exception:
                    raw[key] = old
            else:
                # numeric-looking keys
                if key in (
                    "concurrent_count",
                    "mail_pool_size",
                    "register_count",
                    "shiromail_expires_in_hours",
                ):
                    try:
                        raw[key] = int(val)
                    except Exception:
                        raw[key] = val
                else:
                    raw[key] = val
            changed.append(key)
        if not changed:
            return {"ok": True, "changed": [], "message": "no effective changes"}
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    result = {
        "ok": True,
        "changed": sorted(set(changed)),
        "path": CONFIG_PATH,
        "ts": time.time(),
        "restarted": False,
    }
    if restart_worker and unit_active() == "active":
        # reload worker to pick new config
        code, out, err = _run(["systemctl", "restart", UNIT], timeout=30)
        result["restarted"] = code == 0
        result["restart_code"] = code
        result["restart_stderr"] = (err or out or "")[:300]
    return result


def status_payload(log_lines: int = 80) -> dict:
    logs = unit_logs(log_lines)
    stats = parse_success_fail_from_logs(logs)
    cfg = config_payload(reveal=False)
    return {
        "ok": True,
        "ts": time.time(),
        "unit": UNIT,
        "active": unit_active(),
        "app_dir": APP_DIR,
        "auth_files": count_auth_files(),
        "full_accounts": count_full_accounts(),
        "stats": stats,
        "last_email": parse_last_email(logs),
        "status_text": unit_status_text()[:4000],
        "logs_tail": logs[-12000:],
        "public_url": PUBLIC_URL or f"http://127.0.0.1:{PORT}",
        "config_summary": {
            "email_provider": cfg["values"].get("email_provider"),
            "shiromail_domain": cfg["values"].get("shiromail_domain"),
            "shiromail_api_key_set": any(
                f.get("key") == "shiromail_api_key" and f.get("set") for f in cfg["fields"]
            ),
            "proxy": cfg["values"].get("proxy"),
            "cpa_api_base": cfg["values"].get("cpa_api_base"),
            "concurrent_count": cfg["values"].get("concurrent_count"),
            "mail_pool_size": cfg["values"].get("mail_pool_size"),
        },
    }


def start_unit(mode: str = "loop", count: int | None = None) -> dict:
    mode = (mode or "loop").strip().lower()
    if mode not in ("loop", "start", "forever"):
        mode = "loop"
    if mode == "forever":
        mode = "loop"

    py = os.path.join(APP_DIR, ".venv", "bin", "python")
    if not os.path.isfile(py):
        py = "python3"
    script = os.path.join(APP_DIR, "grok_register_ttk.py")
    if mode == "start":
        n = max(1, int(count or 1))
        exec_start = f"{py} {script} start {n}"
    else:
        exec_start = f"{py} {script} loop"

    drop_in_dir = f"/etc/systemd/system/{UNIT}.service.d"
    drop_in = os.path.join(drop_in_dir, "override.conf")
    content = (
        "[Service]\n"
        f"WorkingDirectory={APP_DIR}\n"
        "ExecStart=\n"
        f"ExecStart={exec_start}\n"
    )
    with _lock:
        os.makedirs(drop_in_dir, exist_ok=True)
        with open(drop_in, "w", encoding="utf-8") as f:
            f.write(content)
        _run(["systemctl", "daemon-reload"], timeout=20)
        code, out, err = _run(["systemctl", "restart", UNIT], timeout=30)
    # reset log cursor so next SSE doesn't replay huge history as "new"
    with _log_cursor_lock:
        _log_cursor["cursor"] = ""
    return {
        "ok": code == 0,
        "action": "start",
        "mode": mode,
        "exec_start": exec_start,
        "active": unit_active(),
        "stdout": out.strip(),
        "stderr": err.strip(),
        "code": code,
        "ts": time.time(),
    }


def stop_unit() -> dict:
    with _lock:
        code, out, err = _run(["systemctl", "stop", UNIT], timeout=30)
    return {
        "ok": code == 0,
        "action": "stop",
        "active": unit_active(),
        "stdout": out.strip(),
        "stderr": err.strip(),
        "code": code,
        "ts": time.time(),
    }


def _ui_html() -> str:
    # token injected client-side from query ?token=
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Grok Register Live</title>
<style>
:root{--bg:#0b1220;--card:#121a2b;--fg:#e8eef8;--muted:#93a0b5;--line:#243047;--ok:#22c55e;--bad:#ef4444;--acc:#3b82f6;--warn:#f59e0b}
*{box-sizing:border-box}
body{margin:0;font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;background:radial-gradient(1200px 600px at 10% -10%,#1e3a5f55,transparent),var(--bg);color:var(--fg)}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{margin:0 0 6px;font-size:22px;letter-spacing:.2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.card{background:linear-gradient(180deg,#162033,#121a2b);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.v{font-size:22px;font-weight:750;margin-top:6px}
.row{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0;align-items:center}
button,.btn{border:0;border-radius:10px;padding:10px 14px;font-weight:650;cursor:pointer;background:var(--acc);color:#fff}
button.stop{background:var(--bad)} button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px;font-weight:650}
.badge.ok{color:var(--ok);border-color:#166534} .badge.bad{color:var(--bad);border-color:#7f1d1d} .badge.warn{color:var(--warn);border-color:#92400e}
#log{background:#070d18;border:1px solid var(--line);border-radius:14px;padding:12px;height:52vh;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.meta{color:var(--muted);font-size:12px}
input,select{background:#0d1524;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px}
.err{background:#3f1d1d;color:#fecaca;border:1px solid #7f1d1d;padding:8px 10px;border-radius:10px;margin:8px 0;display:none}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Grok Register · Live</h1>
  <div class="sub">SSE 实时状态 / 日志 · control plane for systemd worker</div>
  <div id="err" class="err"></div>
  <div class="row">
    <span id="badge" class="badge">connecting…</span>
    <span class="meta" id="conn">SSE: …</span>
    <span class="meta" id="ts"></span>
  </div>
  <div class="grid">
    <div class="card"><div class="k">State</div><div class="v" id="active">-</div></div>
    <div class="card"><div class="k">Auth files</div><div class="v" id="auth">-</div></div>
    <div class="card"><div class="k">Full accounts</div><div class="v" id="full">-</div></div>
    <div class="card"><div class="k">Success</div><div class="v" id="ok">-</div></div>
    <div class="card"><div class="k">Fail</div><div class="v" id="fail">-</div></div>
  </div>
  <div class="row">
    <button id="btnLoop">Start unlimited</button>
    <button id="btn10">Start 10</button>
    <button class="stop" id="btnStop">Stop</button>
    <button class="ghost" id="btnClear">Clear log view</button>
    <span class="meta" id="email"></span>
  </div>
  <div class="card" style="margin-top:8px">
    <div class="k">Settings (config.json)</div>
    <div class="row" style="margin-top:8px">
      <label class="meta">ShiroMail API Base<br/><input id="f_shiromail_api_base" style="width:280px"/></label>
      <label class="meta">ShiroMail API Key<br/><input id="f_shiromail_api_key" style="width:280px" placeholder="leave masked = keep"/></label>
      <label class="meta">ShiroMail Domain<br/><input id="f_shiromail_domain" style="width:180px"/></label>
      <label class="meta">Mail pool<br/><input id="f_mail_pool_size" style="width:80px" type="number" min="0"/></label>
    </div>
    <div class="row">
      <label class="meta">Proxy (browser/WARP)<br/><input id="f_proxy" style="width:280px" placeholder="socks5://127.0.0.1:1080"/></label>
      <label class="meta">CPA API Base<br/><input id="f_cpa_api_base" style="width:240px"/></label>
      <label class="meta">CPA API Key<br/><input id="f_cpa_api_key" style="width:240px" placeholder="leave masked = keep"/></label>
      <label class="meta">Concurrent<br/><input id="f_concurrent_count" style="width:80px" type="number" min="1"/></label>
    </div>
    <div class="row">
      <label class="meta">Email provider<br/>
        <select id="f_email_provider">
          <option value="shiromail">shiromail</option>
          <option value="cloudflare">cloudflare</option>
          <option value="duckmail">duckmail</option>
          <option value="yyds">yyds</option>
        </select>
      </label>
      <label class="meta">Register mode<br/>
        <select id="f_register_mode">
          <option value="hybrid">hybrid</option>
          <option value="browser">browser</option>
          <option value="protocol">protocol</option>
        </select>
      </label>
      <label class="meta">Browser engine<br/>
        <select id="f_browser_engine">
          <option value="cloak">cloak</option>
          <option value="drission">drission</option>
        </select>
      </label>
      <label class="meta">Headless<br/>
        <select id="f_browser_headless"><option value="true">true</option><option value="false">false</option></select>
      </label>
      <label class="meta">Log level<br/>
        <select id="f_log_level"><option>info</option><option>debug</option><option>quiet</option></select>
      </label>
    </div>
    <div class="row">
      <label class="meta"><input type="checkbox" id="f_restart"/> 保存后重启注册进程</label>
      <button id="btnSave">Save settings</button>
      <button class="ghost" id="btnReloadCfg">Reload form</button>
      <span class="meta" id="cfgMsg"></span>
    </div>
  </div>
  <div class="card" style="margin-top:8px">
    <div class="k">Live logs</div>
    <div id="log"></div>
  </div>
</div>
<script>
const qs = new URLSearchParams(location.search);
const token = qs.get('token') || localStorage.getItem('reg_token') || '';
if (qs.get('token')) localStorage.setItem('reg_token', qs.get('token'));
const headers = token ? {'X-Register-Token': token, 'Content-Type':'application/json'} : {'Content-Type':'application/json'};
const logEl = document.getElementById('log');
const errEl = document.getElementById('err');
let es = null, seenLines = new Set(), reconnects = 0;

function showErr(m){ if(!m){errEl.style.display='none';errEl.textContent='';return;} errEl.style.display='block'; errEl.textContent=m; }
function setBadge(active){
  const b=document.getElementById('badge');
  b.className='badge';
  const a=(active||'').toLowerCase();
  if(a==='active'){ b.classList.add('ok'); b.textContent='state: active'; }
  else if(a==='activating'||a==='reloading'){ b.classList.add('warn'); b.textContent='state: '+a; }
  else { b.classList.add('bad'); b.textContent='state: '+(active||'unknown'); }
}
function applyStatus(s){
  if(!s) return;
  document.getElementById('active').textContent = s.active || '-';
  document.getElementById('auth').textContent = s.auth_files ?? '-';
  document.getElementById('full').textContent = s.full_accounts ?? '-';
  const st = s.stats || {};
  document.getElementById('ok').textContent = st.success ?? '-';
  document.getElementById('fail').textContent = st.fail ?? '-';
  document.getElementById('email').textContent = s.last_email ? ('last: '+s.last_email) : '';
  document.getElementById('ts').textContent = s.ts ? new Date(s.ts*1000).toLocaleTimeString() : '';
  setBadge(s.active);
  if (s.logs_tail) appendLogs(s.logs_tail, true);
}
function appendLogs(text, replace=false){
  if(!text) return;
  if(replace){
    logEl.textContent = text;
    logEl.scrollTop = logEl.scrollHeight;
    seenLines = new Set(text.split(/\r?\n/).slice(-300));
    return;
  }
  const lines = text.split(/\r?\n/).filter(Boolean);
  let added=false;
  for(const ln of lines){
    if(seenLines.has(ln)) continue;
    seenLines.add(ln);
    logEl.textContent += (logEl.textContent.endsWith('\n')||!logEl.textContent?'':'\n') + ln;
    added=true;
  }
  // cap memory
  if(seenLines.size > 2000){
    const all = logEl.textContent.split(/\r?\n/);
    logEl.textContent = all.slice(-800).join('\n');
    seenLines = new Set(all.slice(-800));
  }
  if(added) logEl.scrollTop = logEl.scrollHeight;
}
async function api(path, opts){
  const u = path + (path.includes('?')?'&':'?') + (token?('token='+encodeURIComponent(token)):'');
  const r = await fetch(u, Object.assign({headers}, opts||{}));
  const j = await r.json().catch(()=>({ok:false,error:'bad json'}));
  if(!r.ok) throw new Error(j.error||('http '+r.status));
  return j;
}
function connectSSE(){
  if(es){ try{es.close()}catch(e){} }
  const url = '/events?token=' + encodeURIComponent(token||'');
  es = new EventSource(url);
  document.getElementById('conn').textContent = 'SSE: connecting';
  es.addEventListener('snapshot', e => { try{ applyStatus(JSON.parse(e.data)); showErr(''); }catch(err){} });
  es.addEventListener('status', e => { try{ applyStatus(JSON.parse(e.data)); }catch(err){} });
  es.addEventListener('log', e => {
    try{
      const j=JSON.parse(e.data);
      appendLogs(j.text||j.line||'');
    }catch(err){ appendLogs(e.data); }
  });
  es.addEventListener('ping', () => { document.getElementById('conn').textContent = 'SSE: live · r='+reconnects; });
  es.onopen = () => { document.getElementById('conn').textContent = 'SSE: live'; showErr(''); };
  es.onerror = () => {
    document.getElementById('conn').textContent = 'SSE: reconnecting…';
    reconnects++;
    // browser auto-reconnects EventSource
  };
}
const CFG_KEYS = [
  'shiromail_api_base','shiromail_api_key','shiromail_domain','mail_pool_size',
  'proxy','cpa_api_base','cpa_api_key','concurrent_count',
  'email_provider','register_mode','browser_engine','browser_headless','log_level'
];
const SECRET_KEYS = new Set(['shiromail_api_key','cpa_api_key']);
let loadedSecrets = {}; // original masked values
function fillConfig(cfg){
  if(!cfg || !cfg.values) return;
  for(const k of CFG_KEYS){
    const el = document.getElementById('f_'+k);
    if(!el) continue;
    let v = cfg.values[k];
    if(v === undefined || v === null) v = '';
    if(typeof v === 'boolean') el.value = v ? 'true' : 'false';
    else el.value = String(v);
    if(SECRET_KEYS.has(k)) loadedSecrets[k] = String(v||'');
  }
}
async function loadConfig(){
  try{
    const cfg = await api('/config');
    fillConfig(cfg);
    document.getElementById('cfgMsg').textContent = 'config loaded';
  }catch(e){ document.getElementById('cfgMsg').textContent = e.message; }
}
document.getElementById('btnLoop').onclick = async () => {
  try{ showErr(''); await api('/start',{method:'POST',body:JSON.stringify({mode:'loop'})}); }catch(e){ showErr(e.message); }
};
document.getElementById('btn10').onclick = async () => {
  try{ showErr(''); await api('/start',{method:'POST',body:JSON.stringify({mode:'start',count:10})}); }catch(e){ showErr(e.message); }
};
document.getElementById('btnStop').onclick = async () => {
  try{ showErr(''); await api('/stop',{method:'POST',body:'{}'}); }catch(e){ showErr(e.message); }
};
document.getElementById('btnClear').onclick = () => { logEl.textContent=''; seenLines=new Set(); };
document.getElementById('btnReloadCfg').onclick = () => loadConfig();
document.getElementById('btnSave').onclick = async () => {
  try{
    showErr('');
    const patch = {};
    for(const k of CFG_KEYS){
      const el = document.getElementById('f_'+k);
      if(!el) continue;
      let v = el.value;
      if(SECRET_KEYS.has(k)){
        // unchanged masked value -> skip
        if(v && loadedSecrets[k] && v === loadedSecrets[k]) continue;
      }
      if(k === 'browser_headless') v = (v === 'true');
      if(['mail_pool_size','concurrent_count'].includes(k)) v = parseInt(v||'0',10);
      patch[k] = v;
    }
    const restart = document.getElementById('f_restart').checked;
    const r = await api('/config',{method:'POST', body: JSON.stringify({patch, restart})});
    document.getElementById('cfgMsg').textContent = 'saved: '+(r.changed||[]).join(', ') + (r.restarted?' (restarted)':'');
    await loadConfig();
  }catch(e){ showErr(e.message); document.getElementById('cfgMsg').textContent=e.message; }
};
connectSSE();
loadConfig();
// fallback poll if SSE blocked
setInterval(async ()=>{
  if(document.getElementById('conn').textContent.includes('live')) return;
  try{ const s=await api('/status?lines=120'); applyStatus(s); }catch(e){}
}, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RegisterControl/2.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[register-control] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Register-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _auth_ok(self, qs: dict) -> bool:
        if not TOKEN:
            host = self.client_address[0]
            return host in ("127.0.0.1", "::1", "localhost")
        hdr = self.headers.get("X-Register-Token") or self.headers.get("Authorization") or ""
        if hdr.lower().startswith("bearer "):
            hdr = hdr[7:].strip()
        q = ""
        if "token" in qs and qs["token"]:
            q = qs["token"][0]
        return (hdr == TOKEN) or (q == TOKEN)

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path in ("/", "/ui", "/index.html"):
            # UI itself is open; APIs still auth. Token via query for SSE.
            self._html(200, _ui_html())
            return
        if path == "/health":
            self._json(200, {"ok": True, "service": "register-control", "active": unit_active(), "ts": time.time()})
            return

        if not self._auth_ok(qs):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/status":
            lines = 80
            try:
                lines = int((qs.get("lines") or ["80"])[0])
            except Exception:
                pass
            self._json(200, status_payload(lines))
            return
        if path == "/config":
            reveal = str((qs.get("reveal") or ["0"])[0]).lower() in ("1", "true", "yes")
            self._json(200, config_payload(reveal=reveal))
            return
        if path == "/logs":
            lines = 120
            try:
                lines = int((qs.get("lines") or ["120"])[0])
            except Exception:
                pass
            self._json(200, {"ok": True, "logs": unit_logs(lines), "ts": time.time()})
            return
        if path == "/events":
            self._sse_loop()
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        if not self._auth_ok(qs):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._read_json()
        if path == "/start":
            mode = str(body.get("mode") or (qs.get("mode") or ["loop"])[0] or "loop")
            count = body.get("count")
            if count is None and qs.get("count"):
                try:
                    count = int(qs["count"][0])
                except Exception:
                    count = None
            self._json(200, start_unit(mode=mode, count=count))
            return
        if path == "/stop":
            self._json(200, stop_unit())
            return
        if path == "/config":
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
            restart = bool(body.get("restart") or body.get("restart_worker"))
            # also accept form-style flat body
            if not isinstance(patch, dict):
                patch = {}
            self._json(200, save_config_patch(patch, restart_worker=restart))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _sse_loop(self) -> None:
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(event: str, data: dict | str) -> None:
            if isinstance(data, (dict, list)):
                payload = json.dumps(data, ensure_ascii=False)
            else:
                payload = str(data)
            # SSE multi-line data
            lines = payload.splitlines() or [""]
            msg = f"event: {event}\n" + "".join(f"data: {ln}\n" for ln in lines) + "\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()

        # initial snapshot
        try:
            snap = status_payload(120)
            send_event("snapshot", snap)
            # seed cursor after snapshot so subsequent logs are incremental
            unit_logs_since_cursor(5)
        except Exception as exc:
            send_event("error", {"ok": False, "error": str(exc)})

        last_status = time.time()
        try:
            while True:
                now = time.time()
                # incremental logs
                try:
                    new_logs, _ = unit_logs_since_cursor(200)
                    if new_logs:
                        send_event("log", {"text": new_logs, "ts": now})
                except Exception as exc:
                    send_event("error", {"ok": False, "error": f"log: {exc}"})

                # status every interval
                if now - last_status >= max(SSE_INTERVAL, 0.5):
                    try:
                        st = status_payload(40)
                        # avoid huge logs every tick in status event
                        st = dict(st)
                        st.pop("logs_tail", None)
                        st.pop("status_text", None)
                        send_event("status", st)
                    except Exception as exc:
                        send_event("error", {"ok": False, "error": f"status: {exc}"})
                    last_status = now
                    send_event("ping", {"ts": now})

                time.sleep(max(SSE_INTERVAL, 0.4))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:
            return


def main() -> None:
    if not TOKEN:
        print("[register-control] WARN: REGISTER_CONTROL_TOKEN empty; only localhost allowed")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"[register-control] listening on {HOST}:{PORT} unit={UNIT} app={APP_DIR} sse={SSE_INTERVAL}s"
    )
    print(f"[register-control] webui: http://127.0.0.1:{PORT}/?token=***")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
