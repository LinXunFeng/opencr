# OpenCR - Automated Code Review System

An automated code review solution based on GitLab Webhooks + OpenAI API, designed for deployment on macOS.

Language: English | [中文](https://github.com/LinXunFeng/opencr/blob/main/README-zh.md)

**Highlights:** Supports project-level `config.yaml` configuration (with `config.example.yaml`), and the installer can interactively confirm and complete required settings.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Project Structure](#project-structure)
3. [Architecture Overview](#architecture-overview)
4. [Installation](#installation)
5. [GitLab Configuration](#gitlab-configuration)
6. [Admin Console](#admin-console)
7. [Operations and Maintenance](#operations-and-maintenance)
8. [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
# 1. Clone or download this project
cd opencr

# 2. Prepare configuration (required fields are in config.example.yaml)
cp config.example.yaml config.yaml

# 3. Edit config.yaml as needed, then run one-click installation
chmod +x install.sh
./install.sh

# 4. Installation complete. Check your Webhook URL
./quick-test.sh
```

---

## Project Structure

```text
opencr/
├── install.sh              # One-click install script
├── uninstall.sh            # Uninstall script
├── quick-test.sh           # Quick verification tool
├── config.example.yaml     # Config template (copy to config.yaml)
├── README.md               # English documentation
├── README-zh.md            # Chinese documentation
├── skills/                 # Review skills
│   └── review/             # Skill bundles (name/SKILL.md, references, scripts, assets)
├── Dockerfile              # Container image
├── docker-compose.yml      # Container orchestration (recommended deployment)
├── CONTEXT.md              # Domain glossary
├── docs/adr/               # Architecture decision records
├── backend/                # Backend source (Python package)
│   ├── review_server.py    # Flask routing and thread scheduling
│   ├── wsgi.py             # WSGI production entry
│   ├── review/             # Review execution, settlement, GitLab client
│   │   ├── runner.py       # Unified ReviewRun entry point
│   │   └── settlement.py   # Acceptance verdict logic
│   ├── storage/            # Persistence (SQLAlchemy)
│   ├── migrations/         # Alembic migration scripts
│   ├── alembic.ini         # Migration config
│   └── admin/              # Admin routes and pages
└── .gitignore

# Generated after installation
~/opencr/
├── backend/                # Copied source
├── skills/                 # Copied skills for auto skill routing
├── data/                   # SQLite database (review history and acceptance stats)
├── logs/                   # Log directory
├── venv/                   # Python virtual environment
├── config.yaml             # Runtime configuration file
├── start.sh                # Production startup script
└── start-dev.sh            # Development startup script
```

---

## Architecture Overview

```text
┌─────────────┐     Webhook      ┌─────────────────┐     ┌─────────────┐
│   GitLab    │ ───────────────> │  Review Server  │ --> │ OpenAI API  │
│  (Private)  │                  │   (Mac Runner)  │     │ (Compatible)│
└─────────────┘                  └─────────────────┘     └─────────────┘
       ^                                                        |
       |                                                        |
       └────────────────  MR Comment <──────────────────────────┘
```

### Workflow

1. Developer creates or updates an MR and GitLab sends a Webhook event.
2. Review Server receives the event and fetches MR diff content.
3. The service calls the OpenAI API to review the code changes.
4. Review results are posted back to the MR as comments.

---

## Installation

Two options are supported: **Docker** (recommended, cross-platform) and the
**macOS one-click script** (launchd, for running it on your own Mac).

### Method 1: Docker (Recommended)

```bash
# 1. Prepare the config (required first - step 2 exits immediately without it)
cp config.example.yaml config.yaml
# Edit config.yaml, especially the openai and code_platform sections.
# For the admin console, also set admin.enabled to true and fill in admin.token.

# 2. Build and start
docker compose up -d

# 3. Check logs and health
docker compose logs -f
curl http://localhost:9034/health
```

`docker-compose.yml` defines four mounts:

| Mount | Purpose |
|-------|---------|
| `./config.yaml` -> `/app/config.yaml` | Read-only. **Required** - the container exits with a message if it is missing |
| `./skills` -> `/app/skills` | Read-only. **Replaces** the default skills baked into the image; changing a review rule needs no rebuild |
| `opencr-data` -> `/app/data` | Review history and acceptance statistics. **Deleting it resets all statistics, and they cannot be backfilled** |
| `opencr-logs` -> `/app/logs` | Runtime logs |

Database migrations (`alembic upgrade head`) run automatically at container start,
so upgrading the image needs no manual step.

> **Note**: scripts under `skills/*/scripts/` execute **inside the container**, and the
> image only guarantees `python3`. If your custom skill scripts need another runtime
> (Node, Dart, ...), build your own image on top of this one.

### Method 2: macOS One-click Installer

```bash
# Add execute permission and run installer
chmod +x install.sh
./install.sh
```

The installer automatically:
- Checks system requirements (Python3, pip, curl)
- Reads project `config.yaml` (if present) and interactively confirms config values
- Copies source code and `skills/` to `~/opencr/`
- Creates a Python virtual environment and installs dependencies
- Generates startup scripts and launchd configuration
- Starts and verifies the service

### Method 2: Manual Installation

#### 1. Requirements

| Item | Requirement |
|------|-------------|
| macOS | 12.0+ |
| Python | 3.9+ |
| curl | For GitLab/API connectivity checks |

#### 2. Install Dependencies

```bash
# Create install directory
mkdir -p ~/opencr && cd ~/opencr

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Copy dependency manifest
cp /path/to/opencr/requirements.txt ./

# Install dependencies
pip install -r requirements.txt

# Copy source code
cp -r /path/to/opencr/src ./
cp -r /path/to/opencr/skills ./
```

#### 3. Configure `config.yaml`

Create `~/opencr/config.yaml`:

```yaml
openai:
  base_url: "https://api.openai.com/v1"
  api_key: "sk-your-key"
  model: "gpt-4.1"
  reasoning_effort: "medium"

code_platform:
  type: "gitlab"
  url: "https://gitlab.your-company.com"
  token: "glpat-your-token"
  webhook_secret: "your-secret-token"

server:
  host: "0.0.0.0"
  port: 9034
  log_level: "INFO"

review:
  max_diff_size: 50000
  timeout: 180
  skills_dir: "skills"
  skill_scripts_enabled: true
  skill_scripts_timeout: 10
```

#### 4. Start Service

```bash
# Development mode
source venv/bin/activate
cd src && python3 review_server.py

# Or use Gunicorn
gunicorn --bind 0.0.0.0:9034 --chdir src "review_server:app"
```

---

## GitLab Configuration

### 1. Create Access Token

1. Go to GitLab -> User Settings -> Access Tokens.
2. Create a new token with:
   - `api` - Access GitLab APIs
   - `write_repository` - Read/write repository
3. Save the generated token.

### 2. Configure Webhook

Go to Project -> Settings -> Webhooks:

| Field | Value |
|------|-------|
| URL | `http://<YourMacIP>:9034/webhook` |
| Secret Token | Optional. If set, it must match `GITLAB_WEBHOOK_SECRET` |
| Trigger | Enable **Merge request events** |
| SSL Verification | Disable only if using internal plain HTTP |

> The installer prints your detected Webhook URL at the end.

### 3. Test Webhook

After saving the webhook, click **Test** -> **Merge requests**.

---

## Admin Console

The console is a Vue 3 single-page app (Element Plus + ECharts) served by Flask itself at
`/admin`. It loads no CDN resources, so it renders correctly on an isolated intranet.

### Enabling it

The console is **disabled by default**. To enable it, set the following in `config.yaml`:

```yaml
admin:
  enabled: true
  username: "admin"
  # Write the password in PLAIN TEXT. On first start the service replaces it in place
  # with a scrypt hash and leaves a comment above the line. To change the password,
  # put a plain-text value back on that line and restart.
  password: "replace-with-a-strong-password"
  # The webhook port is usually reachable from the intranet.
  # Turn this on if you only access the console from the host itself.
  bind_local_only: false
```

> If `admin.enabled` is `true` but `password` is empty, the service **refuses to start** -
> otherwise you would be exposing an unauthenticated admin panel on an intranet-reachable port.

> **Upgrading from 0.4.0**: `admin.token` has been removed in favour of `username` + `password`.
> If the old field is present, the service refuses to start and tells you exactly what to change.

Once enabled, open `http://localhost:9034/admin`.

### Guest browsing

The console supports **guest browsing**, enabled by default: colleagues can view review status
without an account.

Guests see run status, progress, error statistics and acceptance statistics. They do **not** see
finding bodies, because a body contains the AI's concrete description of code in a private
repository (file path, line, problem, suggested fix), and the webhook port is typically reachable
from the intranet.

The switch lives in the console under System Settings, takes effect immediately, and is the only
configuration item in this project that is not in `config.yaml` - its use is inherently temporary
("visitors on site today, turn it off for now"), and requiring a file edit plus a restart would
mean nobody ever uses it.

The full trade-off is recorded in [ADR-0002](./docs/adr/0002-guest-read-scope.md).

### Modules

| Module | Contents | Guest |
|--------|----------|:---:|
| Dashboard | Key metrics, running reviews, run trend chart, acceptance distribution | ✅ |
| Review runs | Run list (filter by status, search project/MR) and run detail | ✅ (no bodies) |
| Findings | Cross-run finding list, filter by project / verdict / severity / window | ✅ (no bodies) |
| Statistics | Acceptance analysis (rate, coverage, by delivery) and error analysis | ✅ |
| Skills | Loaded skills and their recent hit counts | ✅ |
| Settings | Effective configuration (secrets shown only as "set") and the writable subset | ❌ |

### Read these definitions before reading the numbers

**Progress**: `overall` mode is a single model call with no observable midpoint, so it
reports a phase and no percentage. `file` mode iterates over changed files and adds a
`3/17` file counter. There is no invented progress bar anywhere.

**"In progress" vs "stale"**: the service runs multiple processes, and no single process
can assert that another process's review has died. A heartbeat timeout
(600s by default, `storage.stale_after_seconds`) therefore only **labels** a run as stale;
it never rewrites it to failed - the run may simply be stuck in an unusually slow model call.

**Failed / degraded / skipped are three different things**:

- **Failed**: the review did not complete (model or platform call failed, uncaught exception)
- **Degraded**: the run finished but quality suffered - an inline comment failed to post and was
  downgraded to a plain comment (the content still reached the user), or the diff was truncated
- **Skipped**: Draft/WIP, dependabot, merge-commit updates. **This is not an error**

**The acceptance rate is an approximation.** It is derived from GitLab discussion resolved
state and 👍/👎 reactions. This version does **not** verify whether the code actually changed,
so "resolved" counts as accepted even though in practice it sometimes only means "I read it".
The full trade-off is recorded in
[ADR-0001](./docs/adr/0001-suggestion-acceptance-via-discussion-state.md).

**Coverage must be read alongside the acceptance rate.** Only findings published as GitLab
discussions can be settled. The single summary comment produced by `overall` mode goes through
a **plain note, which cannot be resolved**, and so do inline comments that failed and were
downgraded. Both still count toward total output, which is why the console shows both
"9 of 12 accepted" and "40 findings produced, 12 of them trackable".

### Data retention

Review records are kept for 90 days by default (`storage.retention_days`) and purged by a
background task. **Acceptance data cannot be backfilled** - MRs from before this feature
shipped will never have verdicts, and purged data is equally unrecoverable.

### Working on the front end

The front end lives in `web/` as a separate build unit:

```bash
cd web
pnpm install
pnpm dev      # dev mode, proxies /api/admin to a local server on 9034
pnpm build    # builds into backend/admin/static/
```

**The build output is not committed** - the repository stays clean, and each deployment path
produces it:

- **Docker**: compiled in a multi-stage build, so Node never reaches the final image
- **launchd**: compiled by `install.sh` at install time. If Node/pnpm is missing the step is
  skipped with a warning - the review service still works, `/admin` just returns a short
  "build output missing" message. Install Node and re-run `./install.sh` to enable it.

So changing the front end means committing **source only**. See [`web/README.md`](./web/README.md).

---

## Operations and Maintenance

### Service Management

```bash
# Check status
launchctl list | grep opencr

# Start service
launchctl start com.opencr.server

# Stop service
launchctl stop com.opencr.server

# Check logs
tail -f ~/opencr/logs/server.log
tail -f ~/opencr/logs/launchd.err.log
```

### Quick Verification

```bash
# Run built-in quick test
./quick-test.sh

# Manual checks
curl http://localhost:9034/health

# Trigger a manual review (asynchronous: returns 202 with a run_uid)
curl -X POST http://localhost:9034/manual-review \
  -H "Content-Type: application/json" \
  -d '{"project_id": 123, "mr_iid": 456, "review_mode": "file"}'
# => {"message":"Review started","run_uid":"...","status":"processing", ...}

# Check progress and results for that run
curl -H "X-Admin-Token: YOUR_TOKEN" http://localhost:9034/api/admin/runs/<run_uid>
```

> **Since 0.4.0 `/manual-review` is asynchronous**: it no longer returns the review
> content synchronously, but a `202` with a `run_uid`. This puts it on the same
> execution path as the webhook, so the state machine exists in exactly one place.

### Update Deployment

Docker:

```bash
git pull && docker compose up -d --build
# Migrations run automatically at container start
```

macOS launchd:

```bash
# Just re-run the installer. The database migrates automatically and existing data is kept.
./install.sh
```

---

## Configuration Details

### OpenAI Configuration Loading Order

The service loads OpenAI configuration in this order:

1. Project `config.yaml` (recommended)
2. Environment variable override (optional)
   - `OPENAI_BASE_URL`
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL`
   - `OPENAI_REASONING_EFFORT`
3. If values are still missing, fallback to `~/.codex/config.toml` + `~/.codex/auth.json`

Recommended: generate project `config.yaml` from `config.example.yaml` and manage config there.

### Review Policy

The service skips review automatically when:
- MR title contains `WIP`, `Draft`, or `skip-review`
- Source branch matches `dependabot/*`

MR event to review mode mapping:
- `open`: overall + file-level review
- `update` with new commit: file-level review for incremental commit diff only (inline comments)
- `reopen`: ignored (no review)

Review strategy per MR is determined by webhook event and manual API:
- Mode is controlled by MR event (`open/update`) or manual API `review_mode`
- Skill is auto-selected by AI based on:
  - `skills/<name>/SKILL.md` metadata, or legacy `skills/<name>.md` descriptions
  - changed file paths and diff content
- If no skill matches, that review branch is skipped

---

## Troubleshooting

### Common Issues

#### 1. Service fails to start

```bash
# Check logs
tail -f ~/opencr/logs/launchd.err.log

# Check port usage
lsof -i :9034

# Start manually for debugging
cd ~/opencr && ./start-dev.sh
```

#### 2. Code platform API 403

- Verify `CODE_PLATFORM_TOKEN` is valid
- Confirm token scopes are correct (GitLab: `api`, GitHub: `repo`)
- Confirm project permissions

#### 3. API call failure

```bash
# Check runtime config file
cat ~/opencr/config.yaml

# Test API connectivity
curl -H "Authorization: Bearer sk-your-key" \
  http://your-openai-compatible-domain:port/v1/models
```

#### 4. Webhook not reachable

```bash
# Check service listener
netstat -an | grep 9034

# Test from another machine
curl http://<YourMacIP>:9034/health

# Check macOS firewall
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --list
```

#### 5. Large MR review timeout

Edit `~/opencr/config.yaml`:

```yaml
review:
  timeout: 300
  max_diff_size: 30000
  skill_scripts_timeout: 10
```

Then restart the service.

### Full Reinstall

```bash
# 1. Uninstall
./uninstall.sh

# 2. Reinstall
./install.sh
```

---

## Source Code Guide

### `backend/review_server.py`

Main modules:

| Function | Description |
|---------|-------------|
| `load_openai_config()` | Loads `config.yaml` first, then env override, then falls back to `~/.codex` |
| `truncate_diff()` | Truncates oversized diffs with priority heuristics |
| `call_codex_review()` | Calls OpenAI API for review generation |
| `handle_webhook()` | Handles GitLab Webhook events |
| `should_review_mr()` | Decides whether an MR should be reviewed |

### `backend/wsgi.py`

Gunicorn production entry point.

---

## Security Notes

1. Token security
   - Set `~/opencr/config.yaml` file permission to `600`
   - Never commit tokens to the repository

2. Webhook security
   - Configure `WEBHOOK_SECRET` to verify request source
   - Prefer internal network or VPN access

3. API key security
   - Set project `config.yaml` permission to `600`
   - Never commit `config.yaml` with real secrets to the repository

---

## Customization

### Customize Review Skills

Place standard skill bundles under `skills`.
Each bundle uses `SKILL.md` as the entry point and may include `references/`, `scripts/`, and `assets/`.
The service auto-selects one or more matching skills for each review based on skill metadata and code changes.
Executable files under `scripts/` receive review context as JSON on stdin and may print extra context for the model.
If no skill matches, this review branch is skipped.

Example:

```text
skills/flutter/SKILL.md
skills/flutter/references/lifecycle.md
skills/asset/SKILL.md
skills/asset/scripts/summarize_assets.py
skills/security/SKILL.md
```

Legacy `skills/<name>.md` files are still supported, but bundle directories unlock the full skill resource model.

### Add Custom Skip Rules

Edit `should_review_mr`:

```python
def should_review_mr(data: dict) -> tuple:
    # Add your own filtering logic
    attrs = data.get("object_attributes", {})
    title = (attrs.get("title") or "").lower()
    if "[skip-review]" in title:
        return False, "Skip MR by title keyword", ""
    return True, "Review conditions met", "overall"
```

---

## References

- [GitLab Webhook Events](https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html)
- [GitLab API - Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [OpenAI API Documentation](https://platform.openai.com/docs/api-reference)

---

## License

This project is licensed under the Apache License 2.0.  
See [LICENSE](./LICENSE) for details.

---

If anything fails, check logs first or run `./quick-test.sh` for diagnostics.
