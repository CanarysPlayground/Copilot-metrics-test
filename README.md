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
| `metrics_premium_requests_28d` | **Premium requests** — number of interactions that consumed a premium (non-included) model. See note below for how this is calculated and which models count as premium |
| `metrics_top_model_28d` | The AI model that the user interacted with most often (e.g. `gpt-4o`, `claude-3.5-sonnet`) |
| `metrics_top_language_28d` | The programming language with the highest Copilot activity for this user (e.g. `python`, `typescript`) |
| `metrics_top_feature_28d` | The Copilot feature the user used most often (e.g. `Inline Chat`, `Agent`, `Ask`, `Edit`) |
| `metrics_loc_suggested_by_language_28d` | Per-language breakdown of LOC suggested, sorted by volume descending (e.g. `python 1250, java 560, typescript 320`). See note below about `unknown` and `others` values |
| `metrics_loc_added_by_language_28d` | Per-language breakdown of LOC added (accepted by the user), sorted by volume descending. See note below about `unknown` and `others` values |

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

## What are `unknown` and `others` in the per-language LOC columns?

The `metrics_loc_suggested_by_language_28d` and `metrics_loc_added_by_language_28d` columns break down lines of code by the programming language detected by GitHub Copilot. Two special values can appear in this breakdown:

- **`unknown`** — The GitHub API returned a `null` or missing `language` field for that activity entry. This typically occurs when Copilot is used in a file whose language cannot be detected (e.g. a plain-text scratch buffer, an unsaved file, or a file type not recognised by the language detector). The report preserves these as `unknown` rather than discarding them so the LOC totals remain accurate.

- **`others`** — The GitHub API itself groups less common or unsupported languages under the label `others`. This is a server-side aggregation by GitHub; it is not applied by this report. It covers languages that are tracked by Copilot but not broken out individually in the API response.

Neither value represents an error. The per-language totals (including `unknown` and `others`) will sum to the overall `metrics_loc_suggested_28d` / `metrics_loc_added_28d` figure for that user.

---

## How is `metrics_premium_requests_28d` calculated?

GitHub Copilot plans include a set of base models at no additional cost. Interactions that use any other (premium) model consume **premium requests**, which may be subject to additional billing depending on your plan.

### Which models are included (non-premium)?

The following models are treated as included and do **not** add to `metrics_premium_requests_28d`:

| Model prefix | Examples |
|---|---|
| `gpt-4o` | `gpt-4o`, `gpt-4o-mini`, `gpt-4o-2024-*` |
| `gpt-4.1` | `gpt-4.1`, `gpt-4.1-2025-*` |
| `gpt-5-mini` / `gpt-5mini` | `gpt-5-mini` |
| `default` | The plan's default base-model slot |

Any other model name (e.g. `claude-3.5-sonnet`, `o3`, `gemini-2.5-pro`) is considered **premium**.

### How the count is derived

The report tries three sources in order for each activity row, stopping at the first that provides a value:

1. **Explicit top-level API field** — if the GitHub API returns a dedicated premium-request count field (e.g. `copilot_premium_requests`, `total_premium_requests_count`), that value is used directly.
2. **Explicit per-model API field** — if a per-model breakdown (`totals_by_model_feature`) includes a dedicated premium count field, those values are summed.
3. **Model-based estimation (fallback)** — when neither explicit field is present, the report counts every interaction with a non-included model as one premium request. This is the primary path for current API responses.

> **Important caveat:** The estimation in step 3 counts *interactions*, not *billed request units*. Some premium models carry a multiplier greater than 1× (e.g. a single interaction may cost 10 premium requests). Because the GitHub API does not currently expose per-model multipliers in the usage feed, `metrics_premium_requests_28d` may **undercount** the actual number of billed premium request units for users who heavily use high-multiplier models. It remains accurate for models with a 1× multiplier.

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
