# Enterprise Team Copilot Metrics Report

> Automated daily reporting of GitHub Copilot usage metrics for enterprise teams with optional email delivery.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
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
│  1. Request 28-day metrics report from Copilot Usage API             │
│  2. Download CSV report from provided URL                            │
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
| `metrics_interactions_28d` | Total number of user-initiated interactions with Copilot (prompts sent across all features — inline completions, chat, edit, agent) |
| `metrics_completions_28d` | Number of code-generation events: how many times Copilot generated code for the user |
| `metrics_acceptances_28d` | Number of times the user accepted a Copilot code suggestion |
| `metrics_acceptance_pct_28d` | Acceptance rate as a percentage: `(acceptances / completions) × 100`. Indicates how relevant Copilot's suggestions are to the user |
| `metrics_days_active_28d` | Number of distinct calendar days (UTC) the user had at least one Copilot interaction in the period |
| `metrics_loc_suggested_28d` | **Lines of Code (LOC) suggested** — total lines of code that Copilot proposed to the user across all features. Populated primarily by inline code-completion suggestions |
| `metrics_loc_added_28d` | **Lines of Code (LOC) added** — total lines of code that the user actually added from Copilot-generated content (i.e., accepted and applied suggestions/responses) |
| `metrics_loc_deleted_28d` | **Lines of Code (LOC) deleted** — total lines of code deleted by the user in Copilot-assisted edits during the period |
| `metrics_loc_suggested_inline_28d` | **Lines of Code (LOC) suggested (inline only)** — lines suggested by inline completions only, excluding Edit and Agent features. Use this for accurate acceptance rate calculations |
| `metrics_loc_added_inline_28d` | **Lines of Code (LOC) added (inline only)** — lines added from inline completions only, excluding Edit and Agent features. Use this for accurate acceptance rate calculations |
| `metrics_loc_acceptance_pct_inline_28d` | **LOC acceptance percentage (inline only)** — calculated as `(metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d) × 100`. This field already existed but now you can see the individual inline-only values used in its calculation |
| `metrics_premium_requests_28d` | **Premium requests** — number of interactions that consumed a premium (non-included) model. See [Premium Request Tracking](#premium-request-tracking) for calculation details |
| `metrics_top_model_28d` | The AI model that the user interacted with most often (e.g., `gpt-4o`, `claude-3.5-sonnet`) |
| `metrics_top_language_28d` | The programming language with the highest Copilot activity for this user (e.g., `python`, `typescript`) |
| `metrics_top_feature_28d` | The Copilot feature the user used most often (e.g., `Inline Chat`, `Agent`, `Ask`, `Edit`) |
| `metrics_loc_suggested_by_language_28d` | Per-language breakdown of LOC suggested, sorted by volume descending (e.g., `python 1250, java 560, typescript 320`). See [Per-Language Breakdown](#per-language-breakdown) for details on `unknown` and `others` values |
| `metrics_loc_added_by_language_28d` | Per-language breakdown of LOC added (accepted by the user), sorted by volume descending. See [Per-Language Breakdown](#per-language-breakdown) for details on `unknown` and `others` values |

---

## Understanding the Metrics

### LOC Metrics Explained

#### Why can `metrics_loc_suggested_28d` be *less than* `metrics_loc_added_28d`?

This is a common and expected observation. The two fields measure **different things**:

- **`loc_suggested`** (`loc_suggested_to_add_sum` in the GitHub API) counts lines of code that Copilot *proposed* in a suggestion — this is populated mainly for **inline code completions** where GitHub tracks what was displayed in the ghost-text editor overlay.

- **`loc_added`** (`loc_added_sum` in the GitHub API) counts lines of code that were *actually applied* from a Copilot response — this is aggregated across **all Copilot features**, including:
  - Inline completions
  - **Copilot Chat** (Ask)
  - **Copilot Edit**
  - **Copilot Agent**

For Chat, Edit, and Agent sessions, Copilot can generate and apply entire blocks of code. These responses contribute to `loc_added` even when `loc_suggested_to_add_sum` is `0` for those feature rows (because there is no traditional ghost-text suggestion for those features).

**Example:** A developer uses Copilot Agent to scaffold a 200-line file. `loc_added` increases by 200, but `loc_suggested` may not increase at all because the agent applied the code directly without a ghost-text suggestion step.

**Conclusion:** `loc_added ≥ loc_suggested` is the norm for heavy Chat/Edit/Agent users, and is not a data error.

### Calculating Acceptance Rates

**⚠️ Important:** Do NOT calculate acceptance percentage as `metrics_loc_added_28d / metrics_loc_suggested_28d`. This will give incorrect results.

#### The Problem

The total LOC metrics (`metrics_loc_*_28d`) include lines from ALL Copilot features:
- **Inline completions**: Shows ghost-text suggestions that users can accept/reject
- **Edit mode**: Applies code changes directly without traditional suggestions
- **Agent mode**: Generates and applies entire files without traditional suggestions

When you calculate `metrics_loc_added_28d / metrics_loc_suggested_28d`:
- The numerator includes lines from Edit and Agent (which add code directly)
- The denominator doesn't include Edit/Agent suggestions (they don't use ghost-text)
- Result: artificially low or >100% acceptance rates

#### The Solution

Use the **inline-only LOC metrics** which exclude Edit and Agent features:

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
| LOC Suggested | 1,000 | 1,000 |
| LOC Added | 1,500 | 800 |
| **Acceptance %** | **150%** ❌ Incorrect | **80%** ✅ Correct |

In this example:
- The user accepted 80% of inline completion suggestions
- They also used Edit/Agent features which added 700 additional lines
- The total `metrics_loc_added_28d` (1,500) includes both inline (800) + edit/agent (700)
- Calculating 1,500 / 1,000 = 150% is misleading
- The correct acceptance rate for inline completions is 800 / 1,000 = 80%

### Per-Language Breakdown

The `metrics_loc_suggested_by_language_28d` and `metrics_loc_added_by_language_28d` columns break down lines of code by the programming language detected by GitHub Copilot. Two special values can appear in this breakdown:

- **`unknown`** — The GitHub API returned a `null` or missing `language` field for that activity entry. This typically occurs when Copilot is used in a file whose language cannot be detected (e.g., a plain-text scratch buffer, an unsaved file, or a file type not recognized by the language detector). The report preserves these as `unknown` rather than discarding them so the LOC totals remain accurate.

- **`others`** — The GitHub API itself groups less common or unsupported languages under the label `others`. This is a server-side aggregation by GitHub; it is not applied by this report. It covers languages that are tracked by Copilot but not broken out individually in the API response.

Neither value represents an error. The per-language totals (including `unknown` and `others`) will sum to the overall `metrics_loc_suggested_28d` / `metrics_loc_added_28d` figure for that user.

### Premium Request Tracking

GitHub Copilot plans include a set of base models at no additional cost. Interactions that use any other (premium) model consume **premium requests**, which may be subject to additional billing depending on your plan.

#### Which models are included (non-premium)?

The following models are treated as included and do **not** add to `metrics_premium_requests_28d`:

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

> **Important caveat:** The estimation in step 3 counts *interactions*, not *billed request units*. Some premium models carry a multiplier greater than 1× (e.g., a single interaction may cost 10 premium requests). Because the GitHub API does not currently expose per-model multipliers in the usage feed, `metrics_premium_requests_28d` may **undercount** the actual number of billed premium request units for users who heavily use high-multiplier models. It remains accurate for models with a 1× multiplier.

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

**Last Updated:** 2025-01-15  
**Version:** 1.0  
**Maintained by:** GitHub Enterprise Team
