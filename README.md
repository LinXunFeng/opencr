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
6. [Operations and Maintenance](#operations-and-maintenance)
7. [Troubleshooting](#troubleshooting)

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
│   └── review/             # Skill markdowns (general/flutter/ts...)
├── src/                    # Source code
│   ├── __init__.py
│   ├── review_server.py    # Flask main service
│   └── wsgi.py             # WSGI production entry
└── .gitignore

# Generated after installation
~/opencr/
├── src/                    # Copied source
├── skills/                 # Copied skills for auto skill routing
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

### Method 1: One-click Installer (Recommended)

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

# Install dependencies
pip install openai flask gunicorn python-dotenv requests

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
  port: 5000
  log_level: "INFO"

review:
  max_diff_size: 50000
  timeout: 180
  skills_dir: "skills/review"
```

#### 4. Start Service

```bash
# Development mode
source venv/bin/activate
cd src && python3 review_server.py

# Or use Gunicorn
gunicorn --bind 0.0.0.0:5000 --chdir src "review_server:app"
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
| URL | `http://<YourMacIP>:5000/webhook` |
| Secret Token | Optional. If set, it must match `GITLAB_WEBHOOK_SECRET` |
| Trigger | Enable **Merge request events** |
| SSL Verification | Disable only if using internal plain HTTP |

> The installer prints your detected Webhook URL at the end.

### 3. Test Webhook

After saving the webhook, click **Test** -> **Merge requests**.

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
curl http://localhost:5000/health

# Trigger manual review
curl -X POST http://localhost:5000/manual-review \
  -H "Content-Type: application/json" \
  -d '{"project_id": 123, "mr_iid": 456, "review_mode": "file"}'
```

### Update Deployment

```bash
cd ~/opencr

# Copy updated source files
cp -r /path/to/new/src/* src/
cp -r /path/to/new/skills/* skills/

# Restart service
launchctl stop com.opencr.server
launchctl start com.opencr.server
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
  - `skills/review/*.md` descriptions
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
lsof -i :5000

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
netstat -an | grep 5000

# Test from another machine
curl http://<YourMacIP>:5000/health

# Check macOS firewall
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --list
```

#### 5. Large MR review timeout

Edit `~/opencr/config.yaml`:

```yaml
review:
  timeout: 300
  max_diff_size: 30000
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

### `src/review_server.py`

Main modules:

| Function | Description |
|---------|-------------|
| `load_openai_config()` | Loads `config.yaml` first, then env override, then falls back to `~/.codex` |
| `truncate_diff()` | Truncates oversized diffs with priority heuristics |
| `call_codex_review()` | Calls OpenAI API for review generation |
| `handle_webhook()` | Handles GitLab Webhook events |
| `should_review_mr()` | Decides whether an MR should be reviewed |

### `src/wsgi.py`

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

Place skill prompt files under `skills/review`.
The service auto-selects one or more matching skills for each review based on skill descriptions and code changes.
If no skill matches, this review branch is skipped.

Example:

```text
skills/review/flutter.md
skills/review/ts.md
skills/review/security.md
```

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
