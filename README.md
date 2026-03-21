# Enterprise Team Copilot Metrics Report

Generates a daily CSV report of GitHub Copilot usage metrics for each user across enterprise teams, and optionally delivers the report by email.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set the required environment variables (or create a `.env` file):
   | Variable | Description |
   |---|---|
   | `GITHUB_TOKEN` | Personal access token with `read:enterprise` and `manage_billing:copilot` scopes |
   | `ENTERPRISE_SLUG` | The slug of your GitHub enterprise (e.g. `my-org`) |
   | `ENTERPRISE_TEAM_SLUGS` | Optional comma-separated team slugs to filter; pipe (`\|`) merges teams |

3. Run the script:
   ```bash
   python enterprise_team_copilot_combined_report.py
   ```

---

## Report Columns

The CSV report contains the following columns:

### Identity & Team

| Column | Description |
|---|---|
| `enterprise` | The GitHub enterprise slug the data was collected for |
| `team_name` | Display name of the Copilot team the user belongs to |
| `login` | GitHub username (login) of the user |
| `name` | Display name of the user (from SCIM or GitHub profile) |
| `email` | Email address of the user (from SCIM or GitHub profile, if available) |

### Seat & Billing

| Column | Description |
|---|---|
| `copilot_assigned` | Whether the user currently has a Copilot seat assigned (`yes` / `no`) |
| `plan_type` | The Copilot plan the seat is on (e.g. `copilot_enterprise`, `copilot_business`) |
| `last_activity_at` | ISO-8601 timestamp of the user's most recent Copilot activity |
| `active_status` | Whether the user is considered active (`active` if `last_activity_at` is within the last 30 days, otherwise `inactive`) |

### Metrics — rolling 28-day window

All `_28d` columns aggregate the user's Copilot activity over the 28 days preceding the report date.

| Column | Description |
|---|---|
| `metrics_interactions_28d` | Total number of user-initiated interactions with Copilot (prompts sent across all features — inline completions, chat, edit, agent) |
| `metrics_completions_28d` | Number of code-generation events: how many times Copilot generated code for the user |
| `metrics_acceptances_28d` | Number of times the user accepted a Copilot code suggestion |
| `metrics_acceptance_pct_28d` | Acceptance rate as a percentage: `(acceptances / completions) × 100`. Indicates how relevant Copilot's suggestions are to the user |
| `metrics_days_active_28d` | Number of distinct calendar days (UTC) the user had at least one Copilot interaction in the period |
| `metrics_loc_suggested_28d` | **Lines of Code (LOC) suggested** — total lines of code that Copilot proposed to the user across all features. Populated primarily by inline code-completion suggestions |
| `metrics_loc_added_28d` | **Lines of Code (LOC) added** — total lines of code that the user actually added from Copilot-generated content (i.e. accepted and applied suggestions/responses) |
| `metrics_loc_deleted_28d` | **Lines of Code (LOC) deleted** — total lines of code deleted by the user in Copilot-assisted edits during the period |
| `metrics_top_model_28d` | The AI model that the user interacted with most often (e.g. `gpt-4o`, `claude-3.5-sonnet`) |
| `metrics_top_language_28d` | The programming language with the highest Copilot activity for this user (e.g. `python`, `typescript`) |
| `metrics_top_feature_28d` | The Copilot feature the user used most often (e.g. `Inline Chat`, `Agent`, `Ask`, `Edit`) |

---

## Why can `metrics_loc_suggested_28d` be *less than* `metrics_loc_added_28d`?

This is a common and expected observation. The two fields measure **different things**:

- **`loc_suggested`** (`loc_suggested_to_add_sum` in the GitHub API) counts lines of code that Copilot *proposed* in a suggestion — this is populated mainly for **inline code completions** where GitHub tracks what was displayed in the ghost-text editor overlay.

- **`loc_added`** (`loc_added_sum` in the GitHub API) counts lines of code that were *actually applied* from a Copilot response — this is aggregated across **all Copilot features**, including:
  - Inline completions
  - **Copilot Chat** (Ask)
  - **Copilot Edit**
  - **Copilot Agent**

For Chat, Edit, and Agent sessions, Copilot can generate and apply entire blocks of code. These responses contribute to `loc_added` even when `loc_suggested_to_add_sum` is `0` for those feature rows (because there is no traditional ghost-text suggestion for those features).

**Example:** A developer uses Copilot Agent to scaffold a 200-line file. `loc_added` increases by 200, but `loc_suggested` may not increase at all because the agent applied the code directly without a ghost-text suggestion step.

This means **`loc_added ≥ loc_suggested` is the norm for heavy Chat/Edit/Agent users**, and is not a data error.

---

## Email Delivery

If SMTP settings are configured, the report is emailed as a CSV attachment to the team's configured recipient(s):

| Variable | Description |
|---|---|
| `SMTP_SERVER` | Hostname of the SMTP server |
| `SMTP_PORT` | SMTP port (default: 587) |
| `SMTP_USERNAME` | SMTP login username |
| `SMTP_PASSWORD` | SMTP login password |
| `SENDER_EMAIL` | From-address for outgoing emails |

Per-team recipient addresses are resolved from env vars named `<TEAM_SLUG_UPPERCASE>_TEAM_EMAIL` (e.g. `ACCELERATOR_COPILOT_TEAM_EMAIL`), falling back to positional vars `TEAM1_HEAD_EMAIL`, `TEAM2_HEAD_EMAIL`, etc.
