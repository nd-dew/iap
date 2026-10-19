# iap

Run Odoo's AI service (IAP) on your machine, so your local Odoo databases can use AI.

![iap start, then iap connect --all](docs/demo.gif)

```mermaid
flowchart LR
    base["Odoo 19.4"] --> iap{{"iap"}}
    apps["iap-apps"] --> iap
    iap --> odoo["your local Odoo"]
```

`iap` runs the iap-apps AI service on Odoo 19.4, and points your local Odoo at it.

## Install

```bash
uv tool install git+ssh://git@github.com/nd-dew/iap.git
```

## Usage

```bash
iap start           # start the local IAP service
iap connect --all   # point your local Odoo databases at it
iap logs -f         # watch it work
```

See `iap --help` for the rest.

## How it works

The IAP service is just an Odoo module from iap-apps. `iap start` runs it on its own
Odoo 19.4 server in the background. It uses your own AI keys (`OPENAI_API_KEY`,
`GEAP_SERVICE_ACCOUNT`), and skips credit checks.

`iap connect` tells your local databases to send their AI requests to this server
instead of Odoo's.

## Configuration

All optional, via environment variables:

| Variable | Default |
| --- | --- |
| `IAP_ODOO_DIR` | `~/repos/Odoo/wt/saas-19.4` (Odoo the service runs on, with `odoo/` and `enterprise/`) |
| `IAP_APPS_DIR` | `~/repos/iap-apps` |
| `IAP_PYTHON` | `$IAP_ODOO_DIR/.venv/bin/python` |
| `IAP_PORT` | `8086` |
| `IAP_DB` | `iap` |

State (pid file, log) lives in `~/.local/state/iap/`.
`iap_ai`'s own Python requirements are layered on top of the Odoo venv for each run,
so that venv is never modified.
