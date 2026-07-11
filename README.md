# CPA-GrokRegister

**CLIProxyAPI management plugin + host control plane** for an xAI (Grok) auto-registration worker.

> 本仓库是 **CPA 管理插件 + 主机控制面 + 一键安装脚本**。  
> **浏览器注册逻辑**在独立的 worker 项目中（`grok-auto-register`），不嵌入 CPA 进程。

```text
┌──────────────────────┐     HTTP/SSE      ┌─────────────────────────┐
│  CPA Management UI   │ ───────────────► │  register-control :18927 │
│  plugin: grok-register│                  │  (host Python WebUI)    │
└──────────────────────┘                   └───────────┬─────────────┘
                                                       │ systemctl
                                                       ▼
                                           ┌─────────────────────────┐
                                           │  grok-register worker   │
                                           │  (browser + mail + mint)│
                                           └───────────┬─────────────┘
                                                       │ auth-files / hotload
                                                       ▼
                                           ┌─────────────────────────┐
                                           │  CLIProxyAPI auths pool │
                                           └─────────────────────────┘
```

## Features

- **CPA 管理面板**：启停注册、看状态/日志  
- **主机 WebUI + SSE**：实时日志与指标  
- **可视化配置**：ShiroMail API Key / Domain、Proxy、CPA API、并发等  
- **一键安装脚本**：控制面 + 插件编译 + compose 挂载  
- **可选 MicroWARP**：Cloudflare WARP SOCKS5 出口  

## Requirements

| Component | Requirement |
|-----------|-------------|
| OS | Linux x86_64 (tested Ubuntu 22.04) |
| CPA | CLIProxyAPI with `plugins.enabled` |
| Docker | For CPA container + optional MicroWARP |
| Worker | `grok-auto-register` sources in `APP_DIR` |
| Mail | e.g. ShiroMail API |
| Go / golang image | To build `grok-register.so` (CGO) |

## Quick install

On the **same host** that runs CLIProxyAPI (or can reach its `auths` dir / Management API):

```bash
git clone https://github.com/GALIAIS/CPA-GrokRegister.git
cd CPA-GrokRegister
sudo bash scripts/install.sh
```

Or:

```bash
curl -fsSL https://raw.githubusercontent.com/GALIAIS/CPA-GrokRegister/main/scripts/install.sh | sudo bash
```

Optional env:

```bash
sudo \
  APP_DIR=/opt/grok-auto-register \
  CPA_DIR=/root/CLIProxyAPI \
  CONTROL_TOKEN='your-long-token' \
  bash scripts/install.sh
```

### After install

1. Place **worker** code into `/opt/grok-auto-register` if not already present  
   (`grok_register_ttk.py`, `cpa_xai/`, `requirements.txt`, …)
2. Edit `/opt/grok-auto-register/config.json`  
   - `shiromail_api_key` / `shiromail_domain`  
   - `cpa_api_base` / `cpa_api_key`  
   - `proxy` (e.g. `socks5://127.0.0.1:1080` for MicroWARP)
3. Open WebUI or CPA panel → **Start unlimited**

## Access

| UI | URL |
|----|-----|
| Host WebUI (SSE) | `http://<HOST_IP>:18927/?token=<CONTROL_TOKEN>` |
| CPA plugin panel | `/v0/resource/plugins/grok-register/panel` |
| Health | `GET http://127.0.0.1:18927/health` |
| Config API | `GET/POST http://127.0.0.1:18927/config?token=...` |
| SSE stream | `GET http://127.0.0.1:18927/events?token=...` |

Token file: `/etc/default/register-control`

## Editable settings (WebUI / panel)

| Key | Purpose |
|-----|---------|
| `shiromail_api_base` | ShiroMail base URL |
| `shiromail_api_key` | ShiroMail Bearer key (masked in UI) |
| `shiromail_domain` | Mail domain |
| `mail_pool_size` | Pre-create mailbox pool size |
| `proxy` / `cpa_proxy` | SOCKS/HTTP proxy for browser & mint |
| `cpa_api_base` / `cpa_api_key` | Upload auths to CPA |
| `concurrent_count` | Parallel workers |
| `email_provider` | shiromail / cloudflare / duckmail / yyds |
| `register_mode` | hybrid / browser / protocol |
| `browser_engine` / `browser_headless` | cloak, headless flag |
| `log_level` | info / debug / quiet |

Secrets are **masked** when read; leave the masked value unchanged to keep the old secret.

## Build plugin only

```bash
export CPA_DIR=/root/CLIProxyAPI
sudo bash scripts/build-plugin.sh
# -> $CPA_DIR/plugins/grok-register.so
```

CPA `config.yaml` example:

```yaml
plugins:
  enabled: true
  dir: plugins
  configs:
    grok-register:
      enabled: true
      priority: 10
```

Docker env (inside CPA container):

```yaml
environment:
  GROK_REGISTER_CONTROL_URL: "http://172.17.0.1:18927"   # host gateway
  GROK_REGISTER_CONTROL_TOKEN: "your-token"
```

Mount:

```yaml
volumes:
  - ./plugins:/CLIProxyAPI/plugins
```

## Repository layout

```text
CPA-GrokRegister/
├── README.md
├── LICENSE
├── control/                 # host control plane (SSE WebUI + API)
│   ├── register_control.py
│   ├── register-control.service
│   └── register-control.env.example
├── plugin/                  # CPA management plugin source
│   ├── go/main.go
│   └── README.md
├── host/                    # worker unit + MicroWARP helpers
│   ├── grok-register.service
│   ├── install_microwarp.sh
│   ├── config.linux.example.json
│   └── microwarp/docker-compose.yml
└── scripts/
    ├── install.sh           # one-click installer
    └── build-plugin.sh
```

## Security notes

- Do **not** expose `:18927` to the public internet without a strong token + firewall.
- Prefer binding to docker bridge / private network only.
- CPA management panel still requires the CPA **management key**.
- Never commit real `config.json` or tokens.

## Worker project

Registration browser logic lives in a separate project (not fully vendored here to keep this repo focused on CPA integration):

- Deploy worker sources under `APP_DIR` (default `/opt/grok-auto-register`)
- Or set `REGISTER_REPO=...` when running `install.sh` to clone automatically

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

For automation research, testing, and personal learning only.  
Comply with target site Terms of Service and local laws. Authors are not responsible for misuse.
