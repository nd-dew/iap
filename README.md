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

The IAP service is itself an Odoo module (`odoo_ai`, from iap-apps), so `iap start` runs it
in the background as a separate Odoo 19.4 server on `localhost:8086`, with its own `iap`
database (created on first start). It runs in test mode, so there are no credit checks, and
it calls OpenAI and Vertex / Gemini with the keys from your environment (`OPENAI_API_KEY`,
`GEAP_SERVICE_ACCOUNT`). `iap connect` then sets `ai.endpoint` in your local databases to
that address, so their AI requests go to your local service instead of Odoo's servers.

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
