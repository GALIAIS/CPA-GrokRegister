# Grok Register Management Plugin

CPA Management panel for controlling the **host-side** xAI auto-register worker
(`grok-auto-register` + `systemd grok-register`).

The plugin does **not** embed browser registration. It calls a small HTTP
control plane on the host:

```text
CPA panel (plugin)  -->  host:18927 register-control  -->  systemctl grok-register
```

## Resource

Menu: **Grok Register**

```text
/v0/resource/plugins/grok-register/panel
```

Actions (query):

- `?op=status` — status + logs
- `?op=start&mode=loop` — unlimited registration
- `?op=start&mode=start&count=10` — fixed count
- `?op=stop` — stop worker

Settings (via host control API, also in panel form):

- `GET /config` — editable fields (secrets masked)
- `POST /config` — body `{"patch":{"shiromail_api_key":"..."},"restart":true}`

Editable keys include: `shiromail_api_base`, `shiromail_api_key`, `shiromail_domain`,
`proxy`, `cpa_api_base`, `cpa_api_key`, `concurrent_count`, `mail_pool_size`,
`email_provider`, `register_mode`, `browser_engine`, `browser_headless`, `log_level`.

## Build (Linux amd64, for OVH docker host)

```bash
cd examples/plugin/grok-register/go
GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build -buildmode=c-shared -o grok-register.so .
# place as: <plugins_dir>/grok-register.so
```

On Windows with a Linux cross toolchain, prefer building **on the OVH server**.

## Host control plane (grok-auto-register)

```bash
# on host
cp deploy/linux/register-control.service /etc/systemd/system/
cat >/etc/default/register-control <<EOF
REGISTER_CONTROL_TOKEN=change-me
REGISTER_CONTROL_PORT=18927
REGISTER_APP_DIR=/opt/grok-auto-register
REGISTER_UNIT=grok-register
EOF
systemctl daemon-reload
systemctl enable --now register-control
```

## CPA config

```yaml
plugins:
  enabled: true
  dir: "plugins"   # or absolute path mounted into container
  configs:
    grok-register:
      enabled: true
      priority: 10
```

Docker environment for the plugin process (recommended):

```yaml
# docker-compose environment for cli-proxy-api
environment:
  GROK_REGISTER_CONTROL_URL: "http://172.17.0.1:18927"
  GROK_REGISTER_CONTROL_TOKEN: "change-me"
```

If the compose network is not `docker0`, replace `172.17.0.1` with the host
gateway IP from the container (`ip route | awk '/default/{print $3}'`).

## Security

- Bind `register-control` carefully; use a strong `REGISTER_CONTROL_TOKEN`.
- Prefer host firewall: only docker bridge / localhost can reach `:18927`.
- Management panel itself still requires CPA management key.
