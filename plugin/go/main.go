package main

/*
#include <stdint.h>
#include <stdlib.h>

typedef struct {
	void* ptr;
	size_t len;
} cliproxy_buffer;

typedef int (*cliproxy_host_call_fn)(void*, const char*, const uint8_t*, size_t, cliproxy_buffer*);
typedef void (*cliproxy_host_free_fn)(void*, size_t);

typedef struct {
	uint32_t abi_version;
	void* host_ctx;
	cliproxy_host_call_fn call;
	cliproxy_host_free_fn free_buffer;
} cliproxy_host_api;

typedef int (*cliproxy_plugin_call_fn)(char*, uint8_t*, size_t, cliproxy_buffer*);
typedef void (*cliproxy_plugin_free_fn)(void*, size_t);
typedef void (*cliproxy_plugin_shutdown_fn)(void);

typedef struct {
	uint32_t abi_version;
	cliproxy_plugin_call_fn call;
	cliproxy_plugin_free_fn free_buffer;
	cliproxy_plugin_shutdown_fn shutdown;
} cliproxy_plugin_api;

extern int cliproxyPluginCall(char*, uint8_t*, size_t, cliproxy_buffer*);
extern void cliproxyPluginFree(void*, size_t);
extern void cliproxyPluginShutdown(void);

static const cliproxy_host_api* stored_host;

static void store_host_api(const cliproxy_host_api* host) {
	stored_host = host;
}
*/
import "C"

import (
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"github.com/router-for-me/CLIProxyAPI/v7/sdk/pluginabi"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/pluginapi"
)

const (
	pluginName          = "grok-register"
	resourcePath        = "/panel"
	resourceContentType = "text/html; charset=utf-8"
)

// Plugin config (plugins.configs.grok-register) is injected via env by operators;
// also accept query overrides for control URL/token during testing.
var (
	controlBase  = envOr("GROK_REGISTER_CONTROL_URL", "http://172.17.0.1:18927")
	controlToken = envOr("GROK_REGISTER_CONTROL_TOKEN", "")
	httpClient   = &http.Client{Timeout: 25 * time.Second}
)

type envelope struct {
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *envelopeError  `json:"error,omitempty"`
}

type envelopeError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type registration struct {
	SchemaVersion uint32                   `json:"schema_version"`
	Metadata      pluginapi.Metadata       `json:"metadata"`
	Capabilities  registrationCapabilities `json:"capabilities"`
}

type registrationCapabilities struct {
	ManagementAPI bool `json:"management_api"`
}

type managementRegistration struct {
	Resources []managementResource `json:"resources,omitempty"`
}

type managementResource struct {
	Path        string `json:"Path"`
	Menu        string `json:"Menu"`
	Description string `json:"Description"`
}

type managementRequest struct {
	Method  string
	Path    string
	Headers http.Header
	Query   url.Values
	Body    []byte
}

type managementResponse struct {
	StatusCode int         `json:"StatusCode"`
	Headers    http.Header `json:"Headers"`
	Body       []byte      `json:"Body"`
}

func main() {}

//export cliproxy_plugin_init
func cliproxy_plugin_init(host *C.cliproxy_host_api, plugin *C.cliproxy_plugin_api) C.int {
	if plugin == nil {
		return 1
	}
	C.store_host_api(host)
	plugin.abi_version = C.uint32_t(pluginabi.ABIVersion)
	plugin.call = C.cliproxy_plugin_call_fn(C.cliproxyPluginCall)
	plugin.free_buffer = C.cliproxy_plugin_free_fn(C.cliproxyPluginFree)
	plugin.shutdown = C.cliproxy_plugin_shutdown_fn(C.cliproxyPluginShutdown)
	return 0
}

//export cliproxyPluginCall
func cliproxyPluginCall(method *C.char, request *C.uint8_t, requestLen C.size_t, response *C.cliproxy_buffer) C.int {
	if response != nil {
		response.ptr = nil
		response.len = 0
	}
	if method == nil {
		writeResponse(response, errorEnvelope("invalid_method", "method is required"))
		return 1
	}
	var requestBytes []byte
	if request != nil && requestLen > 0 {
		requestBytes = C.GoBytes(unsafe.Pointer(request), C.int(requestLen))
	}
	raw, errHandle := handleMethod(C.GoString(method), requestBytes)
	if errHandle != nil {
		writeResponse(response, errorEnvelope("plugin_error", errHandle.Error()))
		return 1
	}
	writeResponse(response, raw)
	return 0
}

//export cliproxyPluginFree
func cliproxyPluginFree(ptr unsafe.Pointer, len C.size_t) {
	if ptr != nil {
		C.free(ptr)
	}
	_ = len
}

//export cliproxyPluginShutdown
func cliproxyPluginShutdown() {}

func handleMethod(method string, request []byte) ([]byte, error) {
	switch method {
	case pluginabi.MethodPluginRegister, pluginabi.MethodPluginReconfigure:
		return okEnvelope(pluginRegistration())
	case pluginabi.MethodManagementRegister:
		return okEnvelope(managementRegistration{
			Resources: []managementResource{{
				Path:        resourcePath,
				Menu:        "Grok Register",
				Description: "Start/stop unlimited xAI registration worker and view progress.",
			}},
		})
	case pluginabi.MethodManagementHandle:
		return handleManagement(request)
	default:
		return errorEnvelope("unknown_method", "unknown method: "+method), nil
	}
}

func pluginRegistration() registration {
	return registration{
		SchemaVersion: pluginabi.SchemaVersion,
		Metadata: pluginapi.Metadata{
			Name:             pluginName,
			Version:          "0.1.0",
			Author:           "local",
			GitHubRepository: "https://github.com/router-for-me/CLIProxyAPI",
			Logo:             "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/main/docs/logo.png",
			ConfigFields: []pluginapi.ConfigField{
				{Name: "control_url", Type: pluginapi.ConfigFieldTypeString, Description: "Host register-control base URL (docker->host e.g. http://172.17.0.1:18927)"},
				{Name: "control_token", Type: pluginapi.ConfigFieldTypeString, Description: "Shared token for register-control API"},
			},
		},
		Capabilities: registrationCapabilities{ManagementAPI: true},
	}
}

func handleManagement(raw []byte) ([]byte, error) {
	var req managementRequest
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, fmt.Errorf("decode management request: %w", err)
		}
	}
	op := "status"
	mode := "loop"
	count := 1
	if req.Query != nil {
		if v := strings.TrimSpace(req.Query.Get("op")); v != "" {
			op = strings.ToLower(v)
		}
		if v := strings.TrimSpace(req.Query.Get("mode")); v != "" {
			mode = strings.ToLower(v)
		}
		if v := strings.TrimSpace(req.Query.Get("count")); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				count = n
			}
		}
		if v := strings.TrimSpace(req.Query.Get("control_url")); v != "" {
			controlBase = strings.TrimRight(v, "/")
		}
		if v := strings.TrimSpace(req.Query.Get("token")); v != "" {
			controlToken = v
		}
	}
	var actionResult map[string]any
	var actionErr error
	switch op {
	case "start":
		actionResult, actionErr = controlPOST("/start", map[string]any{"mode": mode, "count": count})
	case "stop":
		actionResult, actionErr = controlPOST("/stop", map[string]any{})
	case "status", "logs", "":
		// fallthrough to status
	default:
		actionErr = fmt.Errorf("unknown op %q", op)
	}
	status, statusErr := controlGET("/status?lines=60")
	if status == nil {
		status = map[string]any{}
	}
	errMsg := ""
	if actionErr != nil {
		errMsg = actionErr.Error()
	}
	if statusErr != nil && errMsg == "" {
		errMsg = statusErr.Error()
	}
	page := renderPanel(op, mode, count, status, actionResult, errMsg)
	return okEnvelope(htmlResponse(http.StatusOK, page))
}

func controlGET(path string) (map[string]any, error) {
	u := strings.TrimRight(controlBase, "/") + path
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	if controlToken != "" {
		req.Header.Set("X-Register-Token", controlToken)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("decode status: %w body=%s", err, truncate(string(body), 200))
	}
	if resp.StatusCode >= 300 {
		return out, fmt.Errorf("control GET %s http=%d", path, resp.StatusCode)
	}
	return out, nil
}

func controlPOST(path string, payload map[string]any) (map[string]any, error) {
	u := strings.TrimRight(controlBase, "/") + path
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequest(http.MethodPost, u, strings.NewReader(string(raw)))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if controlToken != "" {
		req.Header.Set("X-Register-Token", controlToken)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("decode action: %w body=%s", err, truncate(string(body), 200))
	}
	if resp.StatusCode >= 300 {
		return out, fmt.Errorf("control POST %s http=%d", path, resp.StatusCode)
	}
	return out, nil
}

func renderPanel(op, mode string, count int, status, action map[string]any, errMsg string) string {
	// Prefer host public URL for browser EventSource (must be reachable from user browser)
	public := strVal(status["public_url"])
	if public == "" {
		public = controlBase
	}
	// If public is docker-gateway only, still embed; UI will try controlBase and show error.
	jsBase, _ := json.Marshal(public)
	jsToken, _ := json.Marshal(controlToken)
	jsControl, _ := json.Marshal(controlBase)
	initLogs := strVal(status["logs_tail"])
	jsInitLogs, _ := json.Marshal(initLogs)
	jsStatus, _ := json.Marshal(status)

	errHTML := ""
	if errMsg != "" {
		errHTML = fmt.Sprintf(`<div class="err" id="bootErr">%s</div>`, html.EscapeString(errMsg))
	}
	actionHTML := ""
	if action != nil {
		b, _ := json.MarshalIndent(action, "", "  ")
		actionHTML = fmt.Sprintf(`<div class="meta">last action</div><pre class="mini">%s</pre>`, html.EscapeString(string(b)))
	}

	// NOTE: %% for literal percent in fmt.Sprintf
	return fmt.Sprintf(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Grok Register Live</title>
<style>
:root{--bg:#0b1220;--card:#121a2b;--fg:#e8eef8;--muted:#93a0b5;--line:#243047;--ok:#22c55e;--bad:#ef4444;--acc:#3b82f6;--warn:#f59e0b}
*{box-sizing:border-box}
body{margin:0;font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;background:radial-gradient(1000px 500px at 8%% -10%%,#1e3a5f55,transparent),var(--bg);color:var(--fg)}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
h1{margin:0 0 6px;font-size:20px}
.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.card{background:linear-gradient(180deg,#162033,#121a2b);border:1px solid var(--line);border-radius:12px;padding:12px}
.k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.v{font-size:20px;font-weight:750;margin-top:6px}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0;align-items:center}
button{border:0;border-radius:10px;padding:9px 12px;font-weight:650;cursor:pointer;background:var(--acc);color:#fff}
button.stop{background:var(--bad)} button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
.badge{display:inline-flex;padding:4px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px;font-weight:650}
.badge.ok{color:var(--ok);border-color:#166534}.badge.bad{color:var(--bad);border-color:#7f1d1d}.badge.warn{color:var(--warn)}
#log{background:#070d18;border:1px solid var(--line);border-radius:12px;padding:10px;height:48vh;overflow:auto;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap}
.meta{color:var(--muted);font-size:12px}
.err{background:#3f1d1d;color:#fecaca;border:1px solid #7f1d1d;padding:8px 10px;border-radius:10px;margin:8px 0}
pre.mini{background:#070d18;border:1px solid var(--line);border-radius:10px;padding:8px;max-height:120px;overflow:auto;font-size:11px}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Grok Register · Live</h1>
  <div class="sub">CPA panel · SSE from host control · op=%s</div>
  %s
  <div class="row">
    <span id="badge" class="badge">…</span>
    <span class="meta" id="conn">SSE: …</span>
    <span class="meta" id="ts"></span>
    <span class="meta" id="email"></span>
  </div>
  <div class="grid">
    <div class="card"><div class="k">State</div><div class="v" id="active">-</div></div>
    <div class="card"><div class="k">Auth</div><div class="v" id="auth">-</div></div>
    <div class="card"><div class="k">Full</div><div class="v" id="full">-</div></div>
    <div class="card"><div class="k">OK</div><div class="v" id="ok">-</div></div>
    <div class="card"><div class="k">Fail</div><div class="v" id="fail">-</div></div>
  </div>
  <div class="row">
    <button id="btnLoop">Start unlimited</button>
    <button id="btn10">Start 10</button>
    <button class="stop" id="btnStop">Stop</button>
    <button class="ghost" id="btnClear">Clear log</button>
    <a class="meta" id="openUi" href="#" target="_blank" rel="noopener">Open host WebUI</a>
  </div>
  %s
  <div class="card" style="margin:10px 0">
    <div class="k">Settings · ShiroMail / Proxy / CPA</div>
    <div class="row">
      <label class="meta">API Base<br/><input id="f_shiromail_api_base" style="width:220px"/></label>
      <label class="meta">API Key<br/><input id="f_shiromail_api_key" style="width:220px" placeholder="masked=keep"/></label>
      <label class="meta">Domain<br/><input id="f_shiromail_domain" style="width:140px"/></label>
      <label class="meta">Pool<br/><input id="f_mail_pool_size" type="number" min="0" style="width:70px"/></label>
    </div>
    <div class="row">
      <label class="meta">Proxy<br/><input id="f_proxy" style="width:220px"/></label>
      <label class="meta">CPA Base<br/><input id="f_cpa_api_base" style="width:200px"/></label>
      <label class="meta">CPA Key<br/><input id="f_cpa_api_key" style="width:200px" placeholder="masked=keep"/></label>
      <label class="meta">Workers<br/><input id="f_concurrent_count" type="number" min="1" style="width:70px"/></label>
    </div>
    <div class="row">
      <label class="meta">Provider
        <select id="f_email_provider"><option>shiromail</option><option>cloudflare</option><option>duckmail</option><option>yyds</option></select>
      </label>
      <label class="meta">Mode
        <select id="f_register_mode"><option>hybrid</option><option>browser</option><option>protocol</option></select>
      </label>
      <label class="meta">Engine
        <select id="f_browser_engine"><option>cloak</option><option>drission</option></select>
      </label>
      <label class="meta">Headless
        <select id="f_browser_headless"><option value="true">true</option><option value="false">false</option></select>
      </label>
      <label class="meta"><input type="checkbox" id="f_restart"/> restart worker after save</label>
      <button id="btnSave">Save settings</button>
      <button class="ghost" id="btnReloadCfg">Reload</button>
      <span class="meta" id="cfgMsg"></span>
    </div>
  </div>
  <div class="card"><div class="k">Live logs</div><div id="log"></div></div>
</div>
<script>
const PUBLIC = %s;
const CONTROL = %s;
const TOKEN = %s;
const INIT_STATUS = %s;
const INIT_LOGS = %s;
const headers = TOKEN ? {'X-Register-Token': TOKEN, 'Content-Type':'application/json'} : {'Content-Type':'application/json'};
const logEl = document.getElementById('log');
let seen = new Set();
function setBadge(a){
  const b=document.getElementById('badge'); b.className='badge';
  a=(a||'').toLowerCase();
  if(a==='active'){b.classList.add('ok');b.textContent='state: active';}
  else if(a==='activating'||a==='reloading'){b.classList.add('warn');b.textContent='state: '+a;}
  else {b.classList.add('bad');b.textContent='state: '+(a||'unknown');}
}
function apply(s){
  if(!s) return;
  document.getElementById('active').textContent = s.active || '-';
  document.getElementById('auth').textContent = s.auth_files ?? '-';
  document.getElementById('full').textContent = s.full_accounts ?? '-';
  const st=s.stats||{};
  document.getElementById('ok').textContent = st.success ?? '-';
  document.getElementById('fail').textContent = st.fail ?? '-';
  document.getElementById('email').textContent = s.last_email ? ('last: '+s.last_email) : '';
  document.getElementById('ts').textContent = s.ts ? new Date(s.ts*1000).toLocaleTimeString() : '';
  setBadge(s.active);
  if(s.logs_tail) append(s.logs_tail, true);
}
function append(text, replace){
  if(!text) return;
  if(replace){ logEl.textContent=text; logEl.scrollTop=logEl.scrollHeight; seen=new Set(text.split(/\r?\n/).slice(-400)); return; }
  let add=false;
  for(const ln of text.split(/\r?\n/).filter(Boolean)){
    if(seen.has(ln)) continue; seen.add(ln);
    logEl.textContent += (logEl.textContent.endsWith('\n')||!logEl.textContent?'':'\n') + ln; add=true;
  }
  if(seen.size>2500){ const all=logEl.textContent.split(/\r?\n/); logEl.textContent=all.slice(-900).join('\n'); seen=new Set(all.slice(-900)); }
  if(add) logEl.scrollTop=logEl.scrollHeight;
}
async function call(path, opts){
  const base = PUBLIC || CONTROL;
  const u = base.replace(/\/$/,'') + path + (path.includes('?')?'&':'?') + 'token=' + encodeURIComponent(TOKEN||'');
  const r = await fetch(u, Object.assign({headers, mode:'cors'}, opts||{}));
  const j = await r.json().catch(()=>({ok:false,error:'bad json'}));
  if(!r.ok) throw new Error(j.error||('http '+r.status));
  return j;
}
function connect(){
  const base = PUBLIC || CONTROL;
  const url = base.replace(/\/$/,'') + '/events?token=' + encodeURIComponent(TOKEN||'');
  document.getElementById('openUi').href = base.replace(/\/$/,'') + '/?token=' + encodeURIComponent(TOKEN||'');
  const es = new EventSource(url);
  document.getElementById('conn').textContent='SSE: connecting ' + base;
  es.addEventListener('snapshot', e=>{ try{apply(JSON.parse(e.data)); document.getElementById('conn').textContent='SSE: live';}catch(err){} });
  es.addEventListener('status', e=>{ try{const s=JSON.parse(e.data); apply(s);}catch(err){} });
  es.addEventListener('log', e=>{ try{const j=JSON.parse(e.data); append(j.text||'');}catch(err){append(e.data);} });
  es.addEventListener('ping', ()=>{ document.getElementById('conn').textContent='SSE: live'; });
  es.onerror = ()=>{ document.getElementById('conn').textContent='SSE: reconnecting / fallback poll'; };
  // fallback poll
  setInterval(async()=>{
    if(document.getElementById('conn').textContent.includes('live')) return;
    try{ apply(await call('/status?lines=100')); }catch(e){ document.getElementById('conn').textContent='SSE/poll fail: '+e.message; }
  }, 2500);
}
const CFG_KEYS=['shiromail_api_base','shiromail_api_key','shiromail_domain','mail_pool_size','proxy','cpa_api_base','cpa_api_key','concurrent_count','email_provider','register_mode','browser_engine','browser_headless'];
const SECRETS=new Set(['shiromail_api_key','cpa_api_key']);
let loadedSecrets={};
function fillCfg(cfg){
  if(!cfg||!cfg.values) return;
  for(const k of CFG_KEYS){
    const el=document.getElementById('f_'+k); if(!el) continue;
    let v=cfg.values[k]; if(v===undefined||v===null) v='';
    if(typeof v==='boolean') el.value=v?'true':'false'; else el.value=String(v);
    if(SECRETS.has(k)) loadedSecrets[k]=String(v||'');
  }
}
async function loadCfg(){
  try{ const cfg=await call('/config'); fillCfg(cfg); document.getElementById('cfgMsg').textContent='config loaded'; }
  catch(e){ document.getElementById('cfgMsg').textContent=e.message; }
}
document.getElementById('btnLoop').onclick=async()=>{ try{await call('/start',{method:'POST',body:JSON.stringify({mode:'loop'})});}catch(e){alert(e.message);} };
document.getElementById('btn10').onclick=async()=>{ try{await call('/start',{method:'POST',body:JSON.stringify({mode:'start',count:10})});}catch(e){alert(e.message);} };
document.getElementById('btnStop').onclick=async()=>{ try{await call('/stop',{method:'POST',body:'{}'});}catch(e){alert(e.message);} };
document.getElementById('btnClear').onclick=()=>{ logEl.textContent=''; seen=new Set(); };
document.getElementById('btnReloadCfg').onclick=()=>loadCfg();
document.getElementById('btnSave').onclick=async()=>{
  try{
    const patch={};
    for(const k of CFG_KEYS){
      const el=document.getElementById('f_'+k); if(!el) continue;
      let v=el.value;
      if(SECRETS.has(k) && loadedSecrets[k] && v===loadedSecrets[k]) continue;
      if(k==='browser_headless') v=(v==='true');
      if(k==='mail_pool_size'||k==='concurrent_count') v=parseInt(v||'0',10);
      patch[k]=v;
    }
    const restart=document.getElementById('f_restart').checked;
    const r=await call('/config',{method:'POST',body:JSON.stringify({patch,restart})});
    document.getElementById('cfgMsg').textContent='saved: '+(r.changed||[]).join(', ')+(r.restarted?' (restarted)':'');
    await loadCfg();
  }catch(e){ alert(e.message); document.getElementById('cfgMsg').textContent=e.message; }
};
if(INIT_STATUS) apply(INIT_STATUS);
if(INIT_LOGS) append(INIT_LOGS, true);
connect();
loadCfg();
</script>
</div>
</body>
</html>`,
		html.EscapeString(op),
		errHTML,
		actionHTML,
		string(jsBase),
		string(jsControl),
		string(jsToken),
		string(jsStatus),
		string(jsInitLogs),
	)
}

func htmlResponse(code int, page string) managementResponse {
	h := make(http.Header)
	h.Set("Content-Type", resourceContentType)
	return managementResponse{StatusCode: code, Headers: h, Body: []byte(page)}
}

func okEnvelope(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return json.Marshal(envelope{OK: true, Result: raw})
}

func errorEnvelope(code, message string) []byte {
	raw, _ := json.Marshal(envelope{OK: false, Error: &envelopeError{Code: code, Message: message}})
	return raw
}

func writeResponse(response *C.cliproxy_buffer, raw []byte) {
	if response == nil || len(raw) == 0 {
		return
	}
	ptr := C.CBytes(raw)
	if ptr == nil {
		return
	}
	response.ptr = ptr
	response.len = C.size_t(len(raw))
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func strVal(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

func anyVal(v any) any {
	if v == nil {
		return "-"
	}
	return v
}

func mapVal(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return map[string]any{}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
