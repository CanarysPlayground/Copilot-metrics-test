# Enterprise Team Copilot Metrics Report

> Automated daily reporting of GitHub Copilot usage metrics for enterprise teams with optional email delivery.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [GitHub API Endpoints](#github-api-endpoints)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [Installation](#installation)
  - [Configuration](#configuration)
    - [Required Settings](#required-settings)
    - [Team Filtering](#team-filtering)
    - [Advanced Configuration](#advanced-configuration)
    - [Debugging Options](#debugging-options)
    - [Email Delivery Settings](#email-delivery-settings)
- [Usage](#usage)
  - [Running Locally](#running-locally)
  - [GitHub Actions Automation](#github-actions-automation)
  - [Common Scenarios](#common-scenarios)
- [Output](#output)
  - [File Naming](#file-naming)
  - [Report Columns](#report-columns)
- [Understanding the Metrics](#understanding-the-metrics)
  - [LOC Metrics Explained](#loc-metrics-explained)
  - [Calculating Acceptance Rates](#calculating-acceptance-rates)
  - [Per-Language Breakdown](#per-language-breakdown)
  - [Premium Request Tracking](#premium-request-tracking)
  - [CLI Metrics](#cli-metrics)
- [Email Delivery](#email-delivery)
- [SCIM and User Information](#scim-and-user-information)
- [Troubleshooting](#troubleshooting)

---

## Overview

The **Enterprise Team Copilot Metrics Report** is a Python-based tool that generates comprehensive usage reports for GitHub Copilot across your enterprise teams. It collects metrics via GitHub's REST API, aggregates user activity data over a rolling 28-day window, and outputs detailed CSV reports for analysis and billing purposes.

**Key capabilities:**
- Automated daily metrics collection for all enterprise teams or specific team subsets
- Rolling 28-day activity windows with per-user and per-language breakdowns
- Support for team merging and flexible filtering
- Optional SMTP-based email delivery with team-specific recipients
- GitHub Actions integration for hands-off automation
- SCIM support for Enterprise Managed Users (EMU)

---

## Features

✅ **Comprehensive Metrics Tracking**
- User-level Copilot activity across all features (inline, chat, edit, agent)
- **CLI-specific metrics extraction** (interactions, completions, acceptances via GitHub CLI)
- Editor/IDE breakdown (CLI, VSCode, JetBrains, Neovim, etc.)
- Lines of code suggested, accepted, and deleted
- Acceptance rates and engagement metrics
- Premium model usage tracking
- Top language, model, and feature identification

✅ **Flexible Team Management**
- Process all enterprise teams or filter by specific team slugs
- Merge multiple teams into a single combined report
- Automatic deduplication when users belong to multiple teams

✅ **Automated Reporting**
- GitHub Actions workflow for scheduled daily execution
- Configurable output file naming
- Artifact retention and archival

✅ **Email Delivery**
- SMTP integration with STARTTLS encryption
- Team-specific recipient configuration
- Automatic attachment of CSV reports

✅ **Enterprise-Ready**
- SCIM API integration for EMU enterprises
- Fallback to GitHub Users API for standard enterprises
- Support for GitHub Enterprise Server via custom API base URLs
- Debug mode for API response inspection

---

## Architecture

The tool follows a straightforward data collection and aggregation flow:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CONFIGURATION & INPUT                         │
│  • Environment variables / .env file                                 │
│  • GitHub PAT with enterprise + billing scopes                       │
│  • Enterprise slug + optional team filters                           │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     TEAM & MEMBERSHIP DISCOVERY                      │
│  1. Fetch all enterprise teams via Teams API                         │
│  2. Filter teams based on ENTERPRISE_TEAM_SLUGS (if provided)        │
│  3. Retrieve team memberships for each selected team                 │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     USER DATA ENRICHMENT (SCIM)                      │
│  • For EMU: Fetch display names + emails from SCIM API               │
│  • For non-EMU: Use GitHub Users API as fallback                     │
│  • Map users to team memberships                                     │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    COPILOT BILLING & SEATS                           │
│  • Retrieve Copilot seat assignments via Billing API                 │
│  • Match seats to team members by login                              │
│  • Identify active vs inactive users (last 30 days)                  │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    USAGE METRICS COLLECTION                          │
│  1. Fetch metrics manifest from Copilot Metrics API                  │
│     (GET …/copilot/metrics/reports/users-28-day/latest)             │
│  2. Download ALL JSON report files listed in download_links          │
│  3. Parse metrics for each user: interactions, completions,          │
│     acceptances, LOC suggested/added/deleted, premium requests, etc. │
│  4. Aggregate per-language breakdowns                                │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    REPORT GENERATION & OUTPUT                        │
│  • Combine team, billing, and usage data into unified CSV            │
│  • Generate one CSV per team or team group                           │
│  • Calculate derived metrics (acceptance %, active status)           │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    EMAIL DELIVERY (OPTIONAL)                         │
│  • Resolve team-specific recipients from environment variables       │
│  • Send CSV reports via SMTP with STARTTLS                           │
│  • Support for multiple recipients per team                          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## GitHub API Endpoints

All data is fetched exclusively from **GitHub's public REST API** (`https://api.github.com` by default, or the value of `API_BASE` / `GITHUB_API_BASE` for GitHub Enterprise Server).

| Endpoint | Purpose | Auth scope |
|----------|---------|------------|
| `GET /enterprises/{slug}/teams` | List all enterprise teams | `read:enterprise` |
| `GET /enterprises/{slug}/teams/{team_slug}/memberships` | List team members | `read:enterprise` |
| `GET /enterprises/{slug}/copilot/billing/seats` | Copilot seat assignments & last-activity timestamps | `manage_billing:copilot` |
| `GET /enterprises/{slug}/settings/billing/premium_request/usage` | Per-user premium request counts & billed amounts (calendar month) | `manage_billing:copilot` |
| `GET /enterprises/{slug}/copilot/metrics/reports/users-28-day/latest` | 28-day rolling metrics manifest; returns `download_links` to one or more JSON report files | `manage_billing:copilot` |
| `GET /scim/v2/enterprises/{slug}/Users` | User display names and emails for EMU enterprises | SCIM access |
| `GET /users/{login}` | User display name and public email (non-EMU fallback) | public |

> **Note:** The metrics manifest endpoint returns a JSON payload containing `download_links`. The script downloads **all** linked JSON files and combines their rows so that no activity data is missed (e.g. a separate file for IDE completions vs. chat interactions).

---

## Prerequisites

- **Python 3.11+** (tested with Python 3.11)
- **GitHub Personal Access Token (PAT)** with the following scopes:
  - `read:enterprise` – required to read enterprise team memberships
  - `manage_billing:copilot` – required to read Copilot billing seats and usage metrics
  - `read:org` (optional) – may be required for some enterprise configurations
  - **SCIM (EMU only):** For Enterprise Managed Users, the token must also have SCIM access to retrieve user names and emails from the identity provider
- **SMTP server access** (optional) – required only for email delivery functionality

---

## Setup

### Installation

1. **Clone or download this repository:**
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

   The tool requires:
   - `requests` – for GitHub API interactions
   - `python-dotenv` – for environment variable management

### Configuration

#### Required Settings

Create a `.env` file in the project root or set these environment variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `GITHUB_TOKEN` | Personal access token with `read:enterprise` and `manage_billing:copilot` scopes | ✅ Yes |
| `ENTERPRISE_SLUG` | The slug of your GitHub enterprise (e.g., `my-org`) | ✅ Yes |

**Example `.env` file:**
```bash
GITHUB_TOKEN=ghp_your_token_here
ENTERPRISE_SLUG=my-enterprise
```

#### Team Filtering

Control which teams are included in the report:

| Variable | Description | Default |
|----------|-------------|---------|
| `ENTERPRISE_TEAM_SLUGS` | Comma-separated team slugs to filter (e.g., `team-a,team-b`). Use pipe (`\|`) to merge teams into one report (e.g., `team-a\|team-b,team-c`). | All teams |

**Usage examples:**
```bash
# Process only specific teams (separate reports)
ENTERPRISE_TEAM_SLUGS=sales,engineering,marketing

# Merge multiple teams into one combined report
ENTERPRISE_TEAM_SLUGS=frontend|backend,devops

# Mixed: merge some teams, separate others
ENTERPRISE_TEAM_SLUGS=team-a|team-b,team-c,team-d
```

When left empty, all enterprise teams are processed into a single combined report.

#### Advanced Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `API_BASE` / `GITHUB_API_BASE` | Override the GitHub API base URL. Useful for GitHub Enterprise Server. | `https://api.github.com` |
| `GITHUB_API_VERSION` | GitHub API version header | `2022-11-28` |
| `OUTPUT_CSV` | Custom output filename | `enterprise_team_users_copilot_combined_YYYYMMDD.csv` |
| `LOGIN_SUFFIX` | Override the suffix token used for EMU login matching | Derived from enterprise slug |

**Example for GitHub Enterprise Server:**
```bash
API_BASE=https://github.mycompany.com/api/v3
```

#### Debugging Options

| Variable | Description | Default |
|----------|-------------|---------|
| `DEBUG_JSON` | Set to `1` to enable debug output of API responses and metrics parsing | Disabled |
| `DEBUG_FILE_PREFIX` | Prefix for debug output files | `copilot_metrics_debug` |

When debug mode is enabled, the following files are generated:
- `copilot_metrics_debug_latest_payload.json` – The metrics report manifest
- `copilot_metrics_debug_report_head.txt` – First 20KB of the downloaded report
- `copilot_metrics_debug_report_rows_first5.json` – First 5 parsed report rows

#### Email Delivery Settings

Configure SMTP for automated email delivery. All five settings must be provided for email functionality to work:

| Variable | Description | Required for Email |
|----------|-------------|-------------------|
| `SMTP_SERVER` | Hostname of the SMTP server | ✅ Yes |
| `SMTP_PORT` | SMTP port | ✅ Yes (default: 587) |
| `SMTP_USERNAME` | SMTP login username | ✅ Yes |
| `SMTP_PASSWORD` | SMTP login password | ✅ Yes |
| `SENDER_EMAIL` | From-address for outgoing emails | ✅ Yes |

**Per-team recipient configuration:**

Recipients are resolved using two naming schemes (first match wins):

**Scheme 1: Slug-derived (preferred)**
```bash
# Derive variable name from team slug:
# 1. Uppercase the slug
# 2. Replace hyphens/special chars with underscores
# 3. Append _TEAM_EMAIL

ACCELERATOR_COPILOT_TEAM_EMAIL=alice@example.com,bob@example.com
DELIVERY_COPILOT_TEAM_EMAIL=charlie@example.com
NT_COPILOT_TEAM_EMAIL=diane@example.com
```

**Scheme 2: Positional (legacy fallback)**
```bash
# Use numbered variables matching team position in ENTERPRISE_TEAM_SLUGS
TEAM1_HEAD_EMAIL=alice@example.com
TEAM2_HEAD_EMAIL=bob@example.com
```

**Multiple recipients:** Each variable may contain a single address or comma-separated list.

**Merged teams:** When teams are merged with `|`, recipients from all teams in the group are collected and deduplicated.

---

## Usage

### Running Locally

Execute the script directly with Python:

```bash
python enterprise_team_copilot_combined_report.py
```

The script will:
1. Authenticate with GitHub using your PAT
2. Discover teams based on your filter configuration
3. Fetch team memberships and user data
4. Collect Copilot billing and usage metrics
5. Generate CSV report(s)
6. Send emails if SMTP is configured

**Output:**
```
Processing teams for enterprise: my-enterprise
Found 3 teams matching filter
Fetching team members...
Collecting Copilot metrics (28-day window)...
✓ Generated: enterprise_team_sales_copilot_20250115.csv
✓ Generated: enterprise_team_engineering_copilot_20250115.csv
✓ Email sent to: sales-lead@example.com
✓ Email sent to: eng-lead@example.com
```

### GitHub Actions Automation

A pre-configured workflow is included at `.github/workflows/daily-copilot-report.yml`.

**Schedule:**
- **Daily at 02:00 UTC** (cron: `0 2 * * *`)
- **On-demand** via manual workflow dispatch

**Setup:**

1. **Configure repository secrets** at `Settings → Secrets and variables → Actions`:

   **Required:**
   - `GH_PAT_TOKEN` – GitHub PAT with required scopes
   - `ENTERPRISE_SLUG` – Your enterprise slug

   **Optional:**
   - `ENTERPRISE_TEAM_SLUGS` – Team filter configuration
   - `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SENDER_EMAIL`
   - Team email variables (e.g., `ACCELERATOR_COPILOT_TEAM_EMAIL`)

2. **Enable the workflow** (if not already enabled)

3. **Monitor executions** under the `Actions` tab

**Artifacts:**
- CSV reports are uploaded as workflow artifacts
- Retention: 90 days
- Artifact name: `copilot-metrics-report-<run_id>`

### Common Scenarios

#### Scenario 1: Generate report for all teams
```bash
# .env
GITHUB_TOKEN=ghp_xxx
ENTERPRISE_SLUG=my-enterprise
# ENTERPRISE_TEAM_SLUGS is not set

# Run
python enterprise_team_copilot_combined_report.py
```
**Output:** `enterprise_team_users_copilot_combined_20250115.csv`

#### Scenario 2: Generate separate reports for specific teams
```bash
# .env
GITHUB_TOKEN=ghp_xxx
ENTERPRISE_SLUG=my-enterprise
ENTERPRISE_TEAM_SLUGS=sales,engineering,marketing

# Run
python enterprise_team_copilot_combined_report.py
```
**Output:**
- `enterprise_team_sales_copilot_20250115.csv`
- `enterprise_team_engineering_copilot_20250115.csv`
- `enterprise_team_marketing_copilot_20250115.csv`

#### Scenario 3: Merge multiple teams into one report
```bash
# .env
GITHUB_TOKEN=ghp_xxx
ENTERPRISE_SLUG=my-enterprise
ENTERPRISE_TEAM_SLUGS=frontend|backend|devops

# Run
python enterprise_team_copilot_combined_report.py
```
**Output:** `enterprise_team_frontend_and_backend_and_devops_copilot_20250115.csv`

#### Scenario 4: Mixed - merge some, separate others
```bash
# .env
GITHUB_TOKEN=ghp_xxx
ENTERPRISE_SLUG=my-enterprise
ENTERPRISE_TEAM_SLUGS=frontend|backend,qa,sales|marketing

# Run
python enterprise_team_copilot_combined_report.py
```
**Output:**
- `enterprise_team_frontend_and_backend_copilot_20250115.csv`
- `enterprise_team_qa_copilot_20250115.csv`
- `enterprise_team_sales_and_marketing_copilot_20250115.csv`

#### Scenario 5: Enable debugging
```bash
# .env
DEBUG_JSON=1

# Run
python enterprise_team_copilot_combined_report.py
```
**Debug files generated:**
- `copilot_metrics_debug_latest_payload.json`
- `copilot_metrics_debug_report_head.txt`
- `copilot_metrics_debug_report_rows_first5.json`

---

## Output

### File Naming

The script generates CSV files with the following naming conventions:

| Mode | Filename Pattern | Example |
|------|------------------|---------|
| All teams (default) | `enterprise_team_users_copilot_combined_YYYYMMDD.csv` | `enterprise_team_users_copilot_combined_20250115.csv` |
| Single team filter | `enterprise_team_<slug>_copilot_YYYYMMDD.csv` | `enterprise_team_sales_copilot_20250115.csv` |
| Merged teams (pipe) | `enterprise_team_<slug1>_and_<slug2>_copilot_YYYYMMDD.csv` | `enterprise_team_sales_and_marketing_copilot_20250115.csv` |

> **Note:** If your team slug contains "copilot" (e.g., `accelerator-copilot`), the filename will include "copilot" twice: `enterprise_team_accelerator-copilot_copilot_20250115.csv`. This is expected behavior.

**Team merging behavior:**
- Each comma-separated entry produces a separate CSV file
- Teams joined with `|` are merged into a single CSV with rows deduplicated by login (first occurrence wins)

### Report Columns

The CSV report contains the following columns:

#### Identity & Team

| Column | Description |
|--------|-------------|
| `enterprise` | The GitHub enterprise slug the data was collected for |
| `team_name` | Display name of the Copilot team the user belongs to |
| `login` | GitHub username (login) of the user |
| `name` | Display name of the user (from SCIM or GitHub profile) |
| `email` | Email address of the user (from SCIM or GitHub profile, if available) |

#### Seat & Billing

| Column | Description |
|--------|-------------|
| `copilot_assigned` | Whether the user currently has a Copilot seat assigned (`yes` / `no`) |
| `plan_type` | The Copilot plan the seat is on (e.g., `copilot_enterprise`, `copilot_business`) |
| `last_activity_at` | ISO-8601 timestamp of the user's most recent Copilot activity |
| `active_status` | Whether the user is considered active (`active` if `last_activity_at` is within the last 30 days, otherwise `inactive`) |

#### Metrics — Rolling 28-Day Window

All `_28d` columns aggregate the user's Copilot activity over the 28 days preceding the report date.

| Column | Description |
|--------|-------------|
| `metrics_interactions_28d` | Number of prompts the user sent in Chat or Agent mode (e.g., Copilot Chat Ask/Edit/Agent/Plan panel). Does **not** include ghost-text inline completions — those are tracked separately in `metrics_completions_28d` |
| `metrics_completions_28d` | Number of inline ghost-text code suggestions that Copilot showed to the user in the IDE editor (`code_completion` feature). Does **not** include Chat/Agent prompts — those are tracked in `metrics_interactions_28d` |
| `metrics_acceptances_28d` | Number of inline ghost-text suggestions the user accepted (e.g., pressed Tab). Does **not** include Chat/Agent interactions |
| `metrics_acceptance_pct_28d` | Acceptance rate as a percentage: `(acceptances / completions) × 100`. Indicates how relevant Copilot's suggestions are to the user |
| `metrics_days_active_28d` | Number of distinct calendar days (UTC) the user had at least one Copilot interaction in the period |
| `metrics_loc_suggested_28d` | **Lines of Code (LOC) suggested** — total lines of code that Copilot proposed to the user across all features. Equals the sum of `metrics_loc_suggested_inline_28d` + `metrics_loc_suggested_chat_28d` + `metrics_loc_suggested_edit_28d` + `metrics_loc_suggested_agent_28d` |
| `metrics_loc_added_28d` | **Lines of Code (LOC) added** — total lines of code that the user actually added from Copilot-generated content. Equals the sum of the four feature-level `metrics_loc_added_*_28d` columns |
| `metrics_loc_deleted_28d` | **Lines of Code (LOC) deleted** — total lines of code deleted by the user in Copilot-assisted edits during the period |
| `metrics_loc_suggested_inline_28d` | **Lines of Code (LOC) suggested (inline only)** — lines suggested by inline ghost-text completions (`code_completion` feature) only. Chat, Edit, and Agent are tracked separately. Use this for accurate acceptance rate calculations |
| `metrics_loc_added_inline_28d` | **Lines of Code (LOC) added (inline only)** — lines added from inline ghost-text completions only. Use this for accurate acceptance rate calculations |
| `metrics_loc_acceptance_pct_inline_28d` | **LOC acceptance percentage (inline only)** — calculated as `(metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d) × 100` |
| `metrics_loc_suggested_chat_28d` | **LOC suggested (chat only)** — lines proposed in Chat features (`chat_panel_ask_mode`, `chat_inline`, `chat_panel_unknown_mode`) |
| `metrics_loc_added_chat_28d` | **LOC added (chat only)** — lines applied from Chat suggestions |
| `metrics_loc_suggested_edit_28d` | **LOC suggested (edit only)** — lines that Copilot proposed in Edit mode (`chat_panel_edit_mode`, `edit`, `edit_mode`). Reflects code blocks shown in the edit-mode chat panel before the user applies them |
| `metrics_loc_added_edit_28d` | **LOC added (edit only)** — lines the user applied from Edit-mode suggestions |
| `metrics_loc_suggested_agent_28d` | **LOC suggested (agent only)** — lines proposed by Agent/Plan/Custom mode features (`chat_panel_agent_mode`, `chat_panel_plan_mode`, `chat_panel_custom_mode`, `agent`). `agent_edit` (direct file writes) contributes 0 because the GitHub API does not populate `loc_suggested_to_add_sum` for direct file writes |
| `metrics_loc_added_agent_28d` | **LOC added (agent only)** — lines applied in Agent/Plan mode, including all file writes via `agent_edit` |
| `metrics_top_model_28d` | Most frequently used AI model by interaction count (e.g., `gpt-4o`, `claude-3.5-sonnet`). Note: This differs from `premium_requests_by_model_month` which reflects billing-weighted premium request consumption |
| `metrics_top_language_28d` | The programming language with the highest Copilot activity for this user (e.g., `python`, `typescript`) |
| `metrics_top_feature_28d` | The Copilot feature the user used most often (e.g., `Inline Chat`, `Agent`, `Ask`, `Edit`) |
| `metrics_loc_suggested_by_language_total_28d` | Per-language breakdown of LOC suggested across all features (inline + chat + edit + agent; `agent_edit` contributes 0 — direct file writes have no suggestion UI), sorted by volume descending (e.g., `python 1250, java 560, typescript 320`). See [Per-Language Breakdown](#per-language-breakdown) for details on `unknown` and `others` values |
| `metrics_loc_added_by_language_total_28d` | Per-language breakdown of LOC added (accepted by the user) across all features (inline + chat + edit + agent + `agent_edit`), sorted by volume descending. See [Per-Language Breakdown](#per-language-breakdown) for details on `unknown` and `others` values |
| `metrics_loc_suggested_by_language_inline_28d` | Per-language breakdown of LOC suggested via inline (code_completion ghost-text) suggestions only |
| `metrics_loc_added_by_language_inline_28d` | Per-language breakdown of LOC added from inline suggestions only |
| `metrics_loc_suggested_by_language_agent_28d` | Per-language breakdown of LOC suggested by Agent/Plan mode features (`chat_panel_agent_mode`, `chat_panel_plan_mode`, `chat_panel_custom_mode`, `agent`). `agent_edit` is excluded — direct file writes always return 0 for `loc_suggested_to_add_sum` |
| `metrics_loc_added_by_language_agent_28d` | Per-language breakdown of LOC applied in Agent/Plan mode (including `agent_edit` direct file writes) |

#### Billing — Calendar Month

These columns reflect usage for the **full calendar month** (day 1 through last day). The default is the current month; override with `REPORT_YEAR` + `REPORT_MONTH` environment variables.

| Column | Description |
|--------|-------------|
| `billing_period` | The billing month queried, formatted `YYYY-MM` (e.g., `2026-03` for March 2026). Empty when the billing API is unavailable |
| `premium_requests_complete_month` | **Premium requests (complete month)** — total premium (non-base-model) requests used in the full calendar month (`grossQuantity` from billing API). Source: `GET /enterprises/{ent}/settings/billing/premium_request/usage`. Empty when the billing API is unavailable. See [Premium Request Tracking](#premium-request-tracking) for details |
| `billed_amount_month` | **Billed amount (month)** — amount actually charged for premium requests after the included-request quota is deducted (`netAmount` from billing API). Matches the "Billed amount" column in the GitHub billing UI. `0` when usage is within the included quota; empty when the billing API is unavailable |
| `premium_requests_by_model_month` | **Per-model premium requests (month)** — breakdown of total premium requests consumed per AI model this month (combined included + billed, from `grossQuantity`). Format: `claude-sonnet-4 - 10, gpt-5.1 - 3` sorted by count descending. Empty when the billing API is unavailable or the response lacks model information |

---

## Understanding the Metrics

### LOC Metrics Explained

#### How do the LOC columns relate to each other?

The four feature-level breakdown columns always sum to the total:

```
metrics_loc_suggested_28d = metrics_loc_suggested_inline_28d
                           + metrics_loc_suggested_chat_28d
                           + metrics_loc_suggested_edit_28d
                           + metrics_loc_suggested_agent_28d

metrics_loc_added_28d     = metrics_loc_added_inline_28d
                           + metrics_loc_added_chat_28d
                           + metrics_loc_added_edit_28d
                           + metrics_loc_added_agent_28d
```

Each breakdown column covers a distinct set of Copilot features (no overlap):

| Column suffix | Features covered |
|---|---|
| `_inline_` | `code_completion` — ghost-text suggestions in the editor |
| `_chat_` | `chat_panel_ask_mode`, `chat_inline`, `chat_panel_unknown_mode` |
| `_edit_` | `chat_panel_edit_mode`, `edit`, `edit_mode` |
| `_agent_` | `chat_panel_agent_mode`, `chat_panel_plan_mode`, `chat_panel_custom_mode`, `agent`, `agent_edit` |

#### Why can `metrics_loc_suggested_28d` be *less than* `metrics_loc_added_28d`?

This is expected for heavy Agent users. The `agent_edit` feature covers direct file writes where Copilot writes changes straight into files, bypassing the suggestion UI. The GitHub API returns 0 for `loc_suggested_to_add_sum` for these writes, so the report faithfully records 0 for `agent_edit`'s contribution to `metrics_loc_suggested_agent_28d`. Meanwhile, those same file writes still count toward `loc_added`, so `loc_added` in agent mode can be very large when Copilot scaffolds entire files.

**Example:** A developer uses Copilot Agent to scaffold a 200-line file directly via file writes (`agent_edit`). `loc_added` increases by 200, but `loc_suggested` for that component is 0 (the API reports no suggestion UI was shown). Ghost-text inline suggestions may only account for 30 lines suggested. The result: `loc_suggested_28d = 30`, `loc_added_28d = 200 + inline_added`.

**Conclusion:** `loc_added ≥ loc_suggested` is the norm for heavy Agent users, and is not a data error.

### Calculating Acceptance Rates

**⚠️ Important:** Do NOT calculate acceptance percentage as `metrics_loc_added_28d / metrics_loc_suggested_28d`. This will give incorrect results.

#### The Problem

The `_agent_` LOC columns involve two distinct data sources:

- **Inline / Chat / Edit / Agent chat-panel features**: `loc_suggested` = `loc_suggested_to_add_sum` (lines Copilot displayed as a suggestion, from the API directly)
- **`agent_edit` (direct file writes)**: `loc_suggested` = 0 (the GitHub API does not populate `loc_suggested_to_add_sum` for direct file writes; the report records this 0 faithfully)

When you calculate `metrics_loc_added_28d / metrics_loc_suggested_28d`:
- The numerator (`loc_added`) for agent_edit is large (all lines written directly to files)
- The denominator (`loc_suggested`) for agent_edit is 0 — the two don't cancel cleanly across features
- Result: acceptance rates are unreliable when a mix of inline + agent activity is present

#### The Solution

Use the **inline-only LOC metrics** which measure only the traditional ghost-text suggestion → accept flow:

**Option 1: Use the pre-calculated field**
```
metrics_loc_acceptance_pct_inline_28d
```
This field correctly calculates: `(metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d) × 100`

**Option 2: Calculate manually**
```
Acceptance % = (metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d) × 100
```

#### Example

| Metric | Total (All Features) | Inline Only |
|--------|---------------------|-------------|
| LOC Suggested | 1,250 | 1,000 |
| LOC Added | 1,500 | 800 |
| **Acceptance %** | **120%** ❌ Misleading | **80%** ✅ Correct |

In this example:
- The user accepted 80% of inline completion suggestions
- They also used Edit/Agent features which added 700 additional lines
- The total `metrics_loc_added_28d` (1,500) includes both inline (800) + edit/agent (700)
- Calculating 1,500 / 1,250 = 120% is misleading
- The correct acceptance rate for inline completions is 800 / 1,000 = 80%

### Per-Language Breakdown

The `metrics_loc_suggested_by_language_total_28d` and `metrics_loc_added_by_language_total_28d` columns break down lines of code by the programming language detected by GitHub Copilot. Two special values can appear in this breakdown:

- **`unknown`** — The GitHub API returned a `null` or missing `language` field for that activity entry. This typically occurs when Copilot is used in a file whose language cannot be detected (e.g., a plain-text scratch buffer, an unsaved file, or a file type not recognized by the language detector). The report preserves these as `unknown` rather than discarding them so the LOC totals remain accurate.

- **`others`** — The GitHub API itself groups less common or unsupported languages under the label `others`. This is a server-side aggregation by GitHub; it is not applied by this report. It covers languages that are tracked by Copilot but not broken out individually in the API response.

Neither value represents an error. The per-language totals (including `unknown` and `others`) in the `_total_28d` columns will sum to the overall `metrics_loc_suggested_28d` / `metrics_loc_added_28d` figure for that user. The `_inline_28d` and `_agent_28d` columns apply the same special values but are scoped to their respective feature groups.

### Premium Request Tracking

GitHub Copilot plans include a set of base models at no additional cost. Interactions that use any other (premium) model consume **premium requests**, which may be subject to additional billing depending on your plan.

#### Which models are included (non-premium)?

The following models are treated as included and do **not** add to `premium_requests_complete_month`:

| Model prefix | Examples |
|-------------|----------|
| `gpt-4o` | `gpt-4o`, `gpt-4o-mini`, `gpt-4o-2024-*` |
| `gpt-4.1` | `gpt-4.1`, `gpt-4.1-2025-*` |
| `gpt-5-mini` / `gpt-5mini` | `gpt-5-mini` |
| `default` | The plan's default base-model slot |

Any other model name (e.g., `claude-3.5-sonnet`, `o3`, `gemini-2.5-pro`) is considered **premium**.

#### How the count is derived

The report tries three sources in order for each activity row, stopping at the first that provides a value:

1. **Explicit top-level API field** — if the GitHub API returns a dedicated premium-request count field (e.g., `copilot_premium_requests`, `total_premium_requests_count`), that value is used directly.
2. **Explicit per-model API field** — if a per-model breakdown (`totals_by_model_feature`) includes a dedicated premium count field, those values are summed.
3. **Model-based estimation (fallback)** — when neither explicit field is present, the report counts every interaction with a non-included model as one premium request. This is the primary path for current API responses.

> **Important caveat:** The estimation in step 3 counts *interactions*, not *billed request units*. Some premium models carry a multiplier greater than 1× (e.g., a single interaction may cost 10 premium requests). Because the GitHub API does not currently expose per-model multipliers in the usage feed, the internal model-based estimate may **undercount** the actual number of billed premium request units for users who heavily use high-multiplier models. The `premium_requests_complete_month` column uses billing API data and therefore reflects the true billed count for the complete calendar month.

### CLI Metrics

GitHub Copilot can be used through multiple tools: IDEs (VSCode, JetBrains, Neovim) and the **GitHub CLI**. The report now extracts and tracks CLI-specific metrics to help you understand how developers are using Copilot through the command line interface.

#### What are CLI metrics?

CLI metrics measure GitHub Copilot usage specifically through the GitHub CLI tool (`gh copilot`). These metrics are tracked separately from IDE usage to provide visibility into:
- Developers using Copilot in terminal environments
- Teams adopting CLI workflows
- Command-line assistance patterns
- CLI vs IDE adoption trends

#### How are CLI metrics calculated and fetched?

The script fetches data from the **GitHub Copilot Metrics API** using this endpoint:

```
GET /enterprises/{enterprise}/copilot/metrics/reports/users-28-day/latest
```

**Step-by-step process:**

1. **API Request**: The script calls the metrics endpoint with your enterprise slug
2. **Download Links**: The API returns `download_links` — URLs to NDJSON (newline-delimited JSON) files
3. **Data Download**: The script downloads **all** NDJSON files (there can be multiple files for completions, chat, etc.)
4. **Parsing**: Each line in the NDJSON files is parsed as a JSON object containing:
   ```json
   {
     "user_login": "developer-username",
     "day": "2026-05-10",
     "editor": "cli",  // or "vscode", "jetbrains", "neovim", etc.
     "totals_by_editor": [
       {
         "editor": "cli",
         "user_initiated_interaction_count": 15,
         "code_generation_activity_count": 42,
         "code_acceptance_activity_count": 38,
         "loc_suggested_to_add_sum": 250,
         "loc_added_sum": 230
       }
     ]
   }
   ```

5. **Editor Extraction**: The script looks for the `editor` field (or within `totals_by_editor` array)
6. **CLI Filtering**: Rows where `editor` contains `"cli"` (case-insensitive) are identified, including variations like `"gh-cli"`, `"copilot_cli"`, etc.
7. **Aggregation**: All CLI metrics are summed across the 28-day window per user
8. **Output**: CLI-specific columns are added to the CSV report

#### Data freshness and rolling window

- **Rolling 28-day window**: The API provides data for the past 28 days, updated daily
- **Update frequency**: GitHub updates the data approximately every 1-2 hours
- **Historical data**: Only the past 28 days are available (older data is not retained)
- **Granularity**: Daily breakdowns are available, but the report aggregates to 28-day totals

#### CLI-specific columns in the report

The following columns track CLI usage exclusively:

| Column | Description | Calculation |
|--------|-------------|-------------|
| `metrics_cli_interactions_28d` | Number of CLI prompts/interactions | Sum of `user_initiated_interaction_count` where `editor='cli'` |
| `metrics_cli_completions_28d` | Code completions shown via CLI | Sum of `code_generation_activity_count` where `editor='cli'` |
| `metrics_cli_acceptances_28d` | CLI completions accepted | Sum of `code_acceptance_activity_count` where `editor='cli'` |
| `metrics_cli_loc_suggested_28d` | Lines of code suggested via CLI | Sum of `loc_suggested_to_add_sum` where `editor='cli'` |
| `metrics_cli_loc_added_28d` | Lines of code accepted via CLI | Sum of `loc_added_sum` where `editor='cli'` |
| `metrics_cli_acceptance_pct_28d` | CLI acceptance rate | `(metrics_cli_acceptances_28d / metrics_cli_completions_28d) × 100` |

**Editor breakdown columns** (show usage across all tools):

| Column | Description |
|--------|-------------|
| `metrics_editors_used_28d` | Comma-separated list of all editors used (e.g., `"cli, vscode, jetbrains"`) |
| `metrics_top_editor_28d` | Editor with the highest interaction count (e.g., `"vscode"` or `"cli"`) |

#### Understanding the data

**When will CLI metrics be 0?**
- User has never used GitHub Copilot via the CLI
- User only uses IDEs (VSCode, JetBrains, etc.)
- The `editor` field is not included in older API responses (rare)

**CLI vs IDE usage patterns:**
- Developers using `gh copilot suggest` or `gh copilot explain` commands will show CLI metrics
- Terminal-based workflows (bash scripting, SSH sessions, headless environments) typically use CLI
- IDE users will have 0 CLI metrics but positive values in other editor metrics

**Example interpretation:**

```csv
login,metrics_cli_interactions_28d,metrics_editors_used_28d,metrics_top_editor_28d
alice,120,cli,cli
bob,0,"vscode, jetbrains",vscode
charlie,45,"cli, vscode",vscode
```

- **alice**: Primarily uses CLI (120 interactions, top editor is CLI)
- **bob**: Never uses CLI, only IDE tools (VSCode and JetBrains)
- **charlie**: Uses both CLI and IDE, but prefers VSCode (45 CLI interactions, but VSCode is the top editor)

#### API field mappings

The script handles multiple API response formats:

**Nested format** (preferred):
```json
{
  "totals_by_editor": [
    {
      "editor": "cli",
      "user_initiated_interaction_count": 15,
      "code_generation_activity_count": 42
    }
  ]
}
```

**Flat format** (fallback):
```json
{
  "editor": "cli",
  "user_initiated_interaction_count": 15,
  "code_generation_activity_count": 42
}
```

**Alternative field names** (for compatibility):
- `editor`, `client`, or `ide` field names are all recognized
- All values are normalized to lowercase (e.g., `"CLI"` → `"cli"`)
- **CLI variations are automatically detected**: `"cli"`, `"gh-cli"`, `"copilot_cli"`, `"github-cli"`, etc. are all treated as CLI

#### Important notes

1. **CLI metrics are additive** — they're a subset of your total Copilot usage, not separate
2. **Editor field is case-insensitive** — `"CLI"`, `"cli"`, `"Cli"` all count as CLI
3. **CLI variations supported** — `"gh-cli"`, `"copilot_cli"`, and any editor value containing `"cli"` is recognized
4. **Multiple editors per user** — users can appear in both CLI and IDE metrics if they use both
5. **Data retention** — only 28-day rolling window is available
5. **API rate limits** — the script respects GitHub API rate limits with automatic retries

---

## Email Delivery

If SMTP settings are configured, the report is emailed as a CSV attachment to the team's configured recipient(s).

**Requirements:**
- All five SMTP configuration variables must be set (see [Email Delivery Settings](#email-delivery-settings))
- At least one team-specific recipient variable must be configured
- The script uses SMTP with STARTTLS encryption

**Email format:**
- **Subject:** `Copilot Metrics Report - <team_name> - <date>`
- **Body:** Summary information about the report period and CSV attachment
- **Attachment:** The generated CSV report file

**Recipient resolution:**

Per-team recipient addresses are resolved using two naming schemes (first match wins):

**Scheme 1: Slug-derived (preferred)**

Derive the secret name from the team slug by:
1. Uppercasing the slug
2. Replacing hyphens and special characters with underscores
3. Appending `_TEAM_EMAIL`

| Team Slug | Environment Variable |
|-----------|---------------------|
| `accelerator-copilot` | `ACCELERATOR_COPILOT_TEAM_EMAIL` |
| `delivery-copilot` | `DELIVERY_COPILOT_TEAM_EMAIL` |
| `nt-copilot` | `NT_COPILOT_TEAM_EMAIL` |

**Scheme 2: Positional (legacy fallback)**

Uses `TEAM1_HEAD_EMAIL`, `TEAM2_HEAD_EMAIL`, etc., where the number corresponds to the team's position in `ENTERPRISE_TEAM_SLUGS`.

**Multiple recipients:** Each variable may contain a single address or a comma-separated list of addresses:
```bash
ACCELERATOR_COPILOT_TEAM_EMAIL="alice@example.com,bob@example.com"
```

**Merged teams:** When teams are merged with `|`, email recipients from all teams in the group are collected and deduplicated.

---

## SCIM and User Information

The script attempts to retrieve user display names and email addresses from multiple sources:

### Enterprise Managed Users (EMU)

For EMU enterprises, the script fetches user information from the SCIM API, which provides:
- Display name from the identity provider
- Email address from the identity provider

If SCIM is unavailable (returns 401, 403, 404, or 501), the script falls back to the GitHub Users API.

**Token requirements:** For EMU enterprises, ensure your GitHub PAT has SCIM access enabled.

### Non-EMU Enterprises

For standard enterprises, user information is retrieved from:
1. **Copilot billing seat data** – includes the user's GitHub profile name and public email
2. **GitHub Users API** – as a fallback when seat data is incomplete

> **Note:** For non-EMU enterprises, the email field is populated only when the user has set a publicly-visible email on their GitHub profile. For complete email coverage, consider using an EMU enterprise or ask users to set a public email in their profile settings.

---

## Troubleshooting

### Team Slugs Not Found

**Problem:** The script reports that team slugs were not found.

**Solution:** The script will list all available enterprise teams with their slugs. Use the exact slug from this list in your `ENTERPRISE_TEAM_SLUGS` configuration.

The script performs flexible matching:
- Case-insensitive matching
- Normalized slugs (special characters replaced with hyphens)
- Matches against both full slugs (`ent:team-name`) and local slugs (`team-name`)

**Example output:**
```
Available enterprise teams:
  - accelerator-copilot
  - delivery-copilot
  - nt-copilot
  - sales-team
```

### Missing SCIM Data

**Problem:** SCIM data is unavailable for EMU enterprise.

**Solution:** 
- The script will log a warning and continue
- User names and emails will be populated from the GitHub Users API instead
- Some users may have blank email fields if they haven't set a public email

**Check:** Ensure your GitHub PAT has SCIM access enabled if you're using an EMU enterprise.

### Debugging API Responses

**Problem:** Need to inspect API responses for troubleshooting.

**Solution:** Enable debug mode:
```bash
DEBUG_JSON=1 python enterprise_team_copilot_combined_report.py
```

This creates debug files:
- `copilot_metrics_debug_latest_payload.json` – The metrics report manifest
- `copilot_metrics_debug_report_head.txt` – First 20KB of the downloaded report
- `copilot_metrics_debug_report_rows_first5.json` – First 5 parsed report rows

### Email Delivery Not Working

**Problem:** Emails are not being sent.

**Checklist:**
- ✅ All five SMTP variables are set (`SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SENDER_EMAIL`)
- ✅ At least one team email variable is configured (e.g., `ACCELERATOR_COPILOT_TEAM_EMAIL` or `TEAM1_HEAD_EMAIL`)
- ✅ SMTP server allows connections from your IP/environment
- ✅ SMTP credentials are correct
- ✅ SMTP port 587 is not blocked by firewall

**Test SMTP connectivity:**
```bash
telnet your-smtp-server.com 587
```

### GitHub API Rate Limits

**Problem:** Script fails with rate limit errors.

**Solution:**
- Ensure you're using a GitHub PAT (not OAuth app token)
- PATs have higher rate limits (5,000 requests/hour)
- For very large enterprises, consider running reports less frequently
- GitHub Actions has higher rate limits than personal runners

### Missing Metrics for Active Users

**Problem:** User has a Copilot seat but no metrics in the report.

**Possible causes:**
- User hasn't used Copilot in the last 28 days
- User's activity is outside the reporting window
- User's IDE extension may not be properly authenticated

**Check:** Look at the `last_activity_at` column to see when the user last used Copilot.

---

## License

This project is provided as-is for GitHub Enterprise customers. Modify and distribute as needed for your organization's requirements.

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Enable [debug mode](#debugging-options) to inspect API responses
3. Review GitHub's [Copilot API documentation](https://docs.github.com/en/rest/copilot)
4. Contact your GitHub account team for enterprise-specific questions

---

**Last Updated:** 2026-05-06  
**Version:** 1.1  
**Maintained by:** GitHub Enterprise Team
