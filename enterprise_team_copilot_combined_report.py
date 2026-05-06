import os
import csv
import time
import json
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# Env / config
# -------------------------
API_BASE = os.getenv("API_BASE") or os.getenv("GITHUB_API_BASE") or "https://api.github.com"
API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ENTERPRISE_SLUG = os.getenv("ENTERPRISE_SLUG")

OUTPUT_CSV = os.getenv("OUTPUT_CSV") or f"enterprise_team_users_copilot_combined_{datetime.now().strftime('%Y%m%d')}.csv"

# Optional: comma-separated list of team slugs to process (e.g. "team-a,team-b,team-c").
# When set, only those teams are processed and each team gets its own CSV report.
# When unset, all enterprise teams are processed and written to OUTPUT_CSV.
#
# Use a pipe (|) within an entry to merge multiple teams into one combined report.
# Example: "delivery-copilot|accelerator-copilot,nt-copilot"
#   → one combined CSV for delivery-copilot + accelerator-copilot (rows deduplicated by login)
#   → one individual CSV for nt-copilot
# When merging teams, both teams' email secrets (e.g. DELIVERY_COPILOT_TEAM_EMAIL and
# ACCELERATOR_COPILOT_TEAM_EMAIL) are collected and used together as recipients.
_raw_team_slugs = os.getenv("ENTERPRISE_TEAM_SLUGS", "").strip()
# Parse into groups: comma separates groups, pipe (|) separates teams within a group.
ENTERPRISE_TEAM_SLUG_GROUPS: List[List[str]] = []
if _raw_team_slugs:
    for _grp in _raw_team_slugs.split(","):
        _slugs = [s.strip() for s in _grp.split("|") if s.strip()]
        if _slugs:
            ENTERPRISE_TEAM_SLUG_GROUPS.append(_slugs)
# Flat list of all individual slugs – used for team filtering and backward compatibility.
ENTERPRISE_TEAM_SLUGS: List[str] = [s for grp in ENTERPRISE_TEAM_SLUG_GROUPS for s in grp]

# Optional override if your suffix is not derived correctly from enterprise slug
LOGIN_SUFFIX = (os.getenv("LOGIN_SUFFIX") or "").strip().lower()

# -------------------------
# Billing report period
# -------------------------
# The billing premium-request API returns data for a full calendar month (year + month).
# By default the script targets the **current** calendar month.  Override with
# REPORT_YEAR + REPORT_MONTH to query any specific month
# (e.g. REPORT_YEAR=2026 REPORT_MONTH=2 for February 2026).
_now = datetime.now()
REPORT_YEAR: int = int(os.getenv("REPORT_YEAR") or _now.year)
REPORT_MONTH: int = int(os.getenv("REPORT_MONTH") or _now.month)
# Validate the parsed values to catch obviously wrong overrides early.
if not (1 <= REPORT_MONTH <= 12):
    raise SystemExit(f"[ERROR] REPORT_MONTH must be 1–12, got {REPORT_MONTH}.")
if not (_now.year - 5 <= REPORT_YEAR <= _now.year + 1):
    raise SystemExit(
        f"[ERROR] REPORT_YEAR {REPORT_YEAR} is outside the accepted range "
        f"{_now.year - 5}–{_now.year + 1}. Check REPORT_YEAR env var."
    )

# Debug for metrics report parsing
DEBUG = os.getenv("DEBUG_JSON", "0") == "1"
DEBUG_PREFIX = os.getenv("DEBUG_FILE_PREFIX", "copilot_metrics_debug")

# -------------------------
# Email / SMTP configuration
# -------------------------
# All settings are optional; if any required setting is absent, email is skipped.
SMTP_SERVER = os.getenv("SMTP_SERVER", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()

if not GITHUB_TOKEN:
    raise SystemExit("Missing GITHUB_TOKEN in environment (.env).")
if not ENTERPRISE_SLUG:
    raise SystemExit("Missing ENTERPRISE_SLUG in environment (.env).")

HEADERS_JSON = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": API_VERSION,
}
HEADERS_SCIM = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/scim+json",
    "X-GitHub-Api-Version": API_VERSION,
}

SESSION = requests.Session()

# Default feature name for rows with missing or invalid feature data
DEFAULT_FEATURE_NAME = "unknown"

# Feature categories for per-mode LOC reporting.
# Keys must match normalized (lowercase) feature names from the API.
# Inline completions: ghost-text code suggestions in the IDE editor.
_INLINE_FEATURES: frozenset[str] = frozenset({"code_completion"})
# Chat (Ask mode): user-initiated chat panel prompts and inline chat sessions.
_CHAT_FEATURES: frozenset[str] = frozenset({"chat_panel_ask_mode", "chat_inline", "chat_panel_unknown_mode"})
# Edit mode: chat-panel edit mode where Copilot proposes diffs for user review.
# "edit" and "edit_mode" are older/alternate API feature names for the same mode.
#
# NOTE: "agent_edit" is intentionally NOT included here.  Per the GitHub Copilot
# Metrics API docs, agent_edit captures direct file writes from BOTH edit mode AND
# agent mode (it cannot be split by mode).  GitHub's own dashboard classifies all
# agent_edit activity as "Agent contribution", so it is attributed to _AGENT_FEATURES
# below.  Including it in both sets would double-count the same LOC.
#
# loc_suggested_to_add_sum IS populated for chat_panel_edit_mode (code blocks shown
# in the edit-mode chat panel before the user applies them).  We therefore use the
# real feature_loc_suggested values for loc_suggested_edit and loc_added_edit instead
# of a proxy.
_EDIT_FEATURES: frozenset[str] = frozenset({"chat_panel_edit_mode", "edit", "edit_mode"})
# Agent mode: "chat_panel_agent_mode" is the primary API feature name for agent-mode
# chat panel interactions; "chat_panel_plan_mode" covers plan-mode (added March 2026,
# where Copilot creates a step-by-step plan before executing agent-like file writes);
# "chat_panel_custom_mode" covers custom-agent selections.
# "agent_edit" captures file edits written directly into the IDE by agent, plan, and
# edit modes.  Because the API cannot separate these per mode, and GitHub classifies
# all agent_edit writes as "agent-initiated", it is placed here only (not in
# _EDIT_FEATURES).  "agent" is kept as a fallback for older API shapes.
_AGENT_FEATURES: frozenset[str] = frozenset({
    "chat_panel_agent_mode",
    "chat_panel_plan_mode",
    "chat_panel_custom_mode",
    "agent",
    "agent_edit",
})
# Per the GitHub public API docs, loc_suggested_to_add_sum is explicitly 0 only for
# "agent_edit" (direct file writes that bypass the suggestion UI).  All other agent
# features — chat_panel_agent_mode, chat_panel_plan_mode, chat_panel_custom_mode, agent
# — use the chat panel and DO populate loc_suggested_to_add_sum with real data.
# The loc_added + loc_deleted proxy therefore applies only to agent_edit.
_AGENT_FILE_WRITE_FEATURES: frozenset[str] = frozenset({"agent_edit"})
_AGENT_CHAT_PANEL_FEATURES: frozenset[str] = _AGENT_FEATURES - _AGENT_FILE_WRITE_FEATURES

# -------------------------
# HTTP helpers
# -------------------------
def gh_get(url: str, headers: Dict[str, str], params=None, timeout=60) -> requests.Response:
    last = None
    for attempt in range(1, 7):
        resp = SESSION.get(url, headers=headers, params=params, timeout=timeout)
        last = resp

        if resp.status_code in (429, 500, 502, 503, 504):
            retry_after = resp.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else min(30, 2 * attempt)
            time.sleep(wait)
            continue

        return resp
    return last

def normalize_list_payload(payload, keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
    raise RuntimeError(f"Unsupported list payload shape: {type(payload)}")

def fetch_rest_list_paged(url, headers, keys, per_page=100, extra_params=None):
    out = []
    page = 1
    while True:
        params = dict(extra_params or {})
        params["per_page"] = per_page
        params["page"] = page

        resp = gh_get(url, headers=headers, params=params)
        if resp.status_code in (403, 404):
            raise requests.HTTPError(f"{resp.status_code} for {url}: {resp.text}", response=resp)

        resp.raise_for_status()
        items = normalize_list_payload(resp.json(), keys=keys)
        out.extend(items)

        if len(items) < per_page:
            break
        page += 1
    return out

# -------------------------
# SCIM helpers
# -------------------------
def fetch_all_scim_users():
    """Fetch all SCIM users for the enterprise.

    Returns an empty list (with a warning) when the enterprise does not support
    SCIM (e.g. non-EMU accounts return 404/501) or when the token lacks SCIM
    permission (401/403). The rest of the pipeline continues and falls back to
    the GitHub users API for display names.
    """
    url = f"{API_BASE}/scim/v2/enterprises/{ENTERPRISE_SLUG}/Users"

    start_index = 1
    count = 100
    users = []

    while True:
        resp = gh_get(url, headers=HEADERS_SCIM, params={"startIndex": start_index, "count": count})

        if resp.status_code in (401, 403, 404, 501):
            print(
                f"[WARN] SCIM endpoint returned {resp.status_code} – enterprise '{ENTERPRISE_SLUG}' "
                "does not appear to use Enterprise Managed Users (EMU), or the token lacks SCIM "
                "permission. Name/email fields will be populated from the GitHub users API instead."
            )
            return []

        resp.raise_for_status()

        payload = resp.json() or {}
        resources = payload.get("Resources") or []
        users.extend(resources)

        total_results = int(payload.get("totalResults") or 0)
        items_per_page = int(payload.get("itemsPerPage") or len(resources) or 0)

        if items_per_page <= 0:
            break

        start_index += items_per_page
        if start_index > total_results:
            break

    return users

def pick_scim_email(u: dict) -> str:
    emails = u.get("emails") or []
    if isinstance(emails, list) and emails:
        primary = next(
            (e for e in emails if isinstance(e, dict) and e.get("primary") is True and e.get("value")),
            None,
        )
        if primary:
            return str(primary.get("value") or "").strip()

        first = next((e for e in emails if isinstance(e, dict) and e.get("value")), None)
        if first:
            return str(first.get("value") or "").strip()

    user_name = str(u.get("userName") or "").strip()
    return user_name if "@" in user_name else ""

def pick_scim_name(u: dict) -> str:
    dn = str(u.get("displayName") or "").strip()
    if dn:
        return dn

    name_obj = u.get("name") or {}
    if isinstance(name_obj, dict):
        formatted = str(name_obj.get("formatted") or "").strip()
        if formatted:
            return formatted
        given = str(name_obj.get("givenName") or "").strip()
        family = str(name_obj.get("familyName") or "").strip()
        full = " ".join([p for p in [given, family] if p]).strip()
        if full:
            return full

    return ""

def derive_suffix_token() -> str:
    if LOGIN_SUFFIX:
        return LOGIN_SUFFIX
    return (ENTERPRISE_SLUG.split("-", 1)[0] or "").strip().lower()

def generate_login_candidates_from_email(email: str) -> Set[str]:
    out: Set[str] = set()
    email = (email or "").strip().lower()
    if "@" not in email:
        return out

    local = email.split("@", 1)[0].strip()
    if not local:
        return out

    suffix = derive_suffix_token()

    variants = set()
    variants.add(local)
    variants.add(local.replace(".", ""))
    variants.add(local.replace(".", "-"))
    variants.add(local.replace("_", "-"))
    variants.add(re.sub(r"[^a-z0-9\-]", "", local))
    variants.add(re.sub(r"[^a-z0-9]", "", local))

    for v in list(variants):
        v = v.strip("-").strip()
        if not v:
            continue
        out.add(v)
        if suffix:
            out.add(f"{v}_{suffix}")

    return out

def build_scim_index(scim_users):
    """Build a lookup index from SCIM users.
    
    The index maps multiple lookup keys (email local parts, userName variations, etc.)
    to SCIM user records. To handle cases where different users generate the same derived
    key (e.g., "atishayjain@..." and "atishay-jain@..." both produce "atishayjain" after
    removing hyphens), each key maps to a LIST of candidate records. The `scim_lookup`
    function will then select the best match based on exact-match priority.
    """
    idx: Dict[str, List[Dict[str, str]]] = {}
    for u in scim_users:
        if not isinstance(u, dict):
            continue

        name = pick_scim_name(u)
        email = pick_scim_email(u)
        scim_user_name = str(u.get("userName") or "").strip()

        user_record = {"name": name, "email": email, "scim_userName": scim_user_name}

        keys: Set[str] = set()

        if email:
            keys.add(email.lower())
            keys.add(email.split("@", 1)[0].lower())
            keys |= generate_login_candidates_from_email(email)

        if scim_user_name:
            keys.add(scim_user_name.lower())
            if "@" in scim_user_name:
                keys.add(scim_user_name.split("@", 1)[0].lower())
                keys |= generate_login_candidates_from_email(scim_user_name)

        for k in keys:
            if not k:
                continue
            if k not in idx:
                idx[k] = []
            # Avoid duplicate entries for the same user record under the same key.
            # We check if an identical record already exists before appending.
            if user_record not in idx[k]:
                idx[k].append(user_record)

    return idx

def _select_best_scim_match(candidates: List[Dict[str, str]], login: str) -> Dict[str, str]:
    """Select the best SCIM record from a list of candidates for a given GitHub login.
    
    Priority order:
    1. Exact match on email local part (before @)
    2. Exact match on scim_userName local part (before @)
    3. First candidate (original first-come-first-served fallback)
    """
    if not candidates:
        return {}
    if len(candidates) == 1:
        return candidates[0]
    
    login_lower = login.lower().strip()
    # Remove enterprise suffix if present (e.g., "atishayjain_newgen" -> "atishayjain")
    login_base = login_lower.split("_", 1)[0] if "_" in login_lower else login_lower
    
    # Priority 1: Exact match on email local part
    for c in candidates:
        email = (c.get("email") or "").lower()
        if "@" in email:
            email_local = email.split("@", 1)[0]
            # Check both the full login and the base (without suffix)
            if email_local == login_lower or email_local == login_base:
                return c
            # Also check variations (hyphen -> underscore, underscore -> hyphen)
            email_local_normalized = email_local.replace("-", "_")
            login_normalized = login_base.replace("-", "_")
            if email_local_normalized == login_normalized:
                return c
    
    # Priority 2: Exact match on scim_userName local part
    for c in candidates:
        scim_user_name = (c.get("scim_userName") or "").lower()
        if "@" in scim_user_name:
            scim_local = scim_user_name.split("@", 1)[0]
            if scim_local == login_lower or scim_local == login_base:
                return c
            scim_local_normalized = scim_local.replace("-", "_")
            login_normalized = login_base.replace("-", "_")
            if scim_local_normalized == login_normalized:
                return c
        elif scim_user_name == login_lower or scim_user_name == login_base:
            return c
    
    # Fallback: return first candidate
    return candidates[0]


def scim_lookup(scim_index: Dict[str, List[Dict[str, str]]], login: str) -> Dict[str, str]:
    """Look up SCIM user data for a GitHub login.
    
    When multiple SCIM users map to the same lookup key (e.g., both "atishayjain@..."
    and "atishay-jain@..." normalize to "atishayjain"), this function selects the
    best match based on exact-match priority.
    """
    if not login:
        return {}
    key = login.lower().strip()

    candidates = scim_index.get(key)
    if candidates:
        return _select_best_scim_match(candidates, login)

    base = key
    if "_" in key:
        base = key.split("_", 1)[0]
        candidates = scim_index.get(base)
        if candidates:
            return _select_best_scim_match(candidates, login)

    suffix = derive_suffix_token()
    if suffix:
        candidates = scim_index.get(f"{base}_{suffix}")
        if candidates:
            return _select_best_scim_match(candidates, login)

    return {}

# -------------------------
# Fallback: GitHub user lookup (non-EMU enterprises)
# -------------------------
_gh_user_cache: Dict[str, Dict[str, str]] = {}

def fetch_github_user_info(login: str) -> Dict[str, str]:
    """Fetch display name and public email for a GitHub login via the users API.

    For EMU enterprises this is only called when the SCIM lookup fails to match
    a user (e.g. incomplete SCIM sync).  For non-EMU enterprises it is the
    primary source of name/email data.  The ``email`` field is populated when
    the user has set a publicly-visible email on their GitHub profile; it is
    left as an empty string when the profile email is private or unset.
    Falls back gracefully on any error.
    """
    if not login:
        return {}
    key = login.lower()
    if key in _gh_user_cache:
        return _gh_user_cache[key]

    url = f"{API_BASE}/users/{login}"
    try:
        resp = gh_get(url, headers=HEADERS_JSON)
        if resp.status_code == 404:
            _gh_user_cache[key] = {}
            return {}
        resp.raise_for_status()
        data = resp.json() or {}
        name = str(data.get("name") or "").strip()
        email = str(data.get("email") or "").strip()
        result = {"name": name, "email": email}
        _gh_user_cache[key] = result
        return result
    except requests.exceptions.RequestException as exc:
        print(f"[WARN] Could not fetch GitHub user info for '{login}': {exc}")
        _gh_user_cache[key] = {}
        return {}

# -------------------------
# Copilot seats (billing)
# -------------------------
def fetch_copilot_billing_seats_by_login():
    url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/copilot/billing/seats"

    all_seats = []
    page = 1
    per_page = 100
    while True:
        resp = gh_get(url, headers=HEADERS_JSON, params={"per_page": per_page, "page": page})
        resp.raise_for_status()
        payload = resp.json() or {}
        seats = payload.get("seats", []) or []
        all_seats.extend(seats)
        if len(seats) < per_page:
            break
        page += 1

    by_login = {}
    for s in all_seats:
        login = ((s.get("assignee") or {}).get("login") or "").strip()
        if login:
            by_login[login] = s
    return by_login

def fetch_monthly_premium_requests_by_login(
    logins: List[str],
    year: int,
    month: int,
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, Dict[str, float]]]:
    """Fetch premium request usage and billed amount for a specific calendar month per user.

    Calls GET /enterprises/{enterprise}/settings/billing/premium_request/usage
    with ``year``, ``month``, and ``user`` query parameters once per login.

    *year* and *month* must refer to a fully completed billing period.  Pass
    ``REPORT_YEAR`` and ``REPORT_MONTH`` (which default to the current calendar
    month) so the returned counts cover the whole month from the 1st to the last day.

    Returns a 3-tuple:
      - ``premium_requests``: login → total ``grossQuantity`` consumed in *month*/*year*.
        This is the *gross* (pre-deduction) total, i.e. every premium request the user
        made regardless of whether it fell within their included-request quota.  For a
        user who consumed 450 requests against a 300-request quota this value is 450.
      - ``billed_amounts``:   login → total ``netAmount`` actually charged after the
        included-request quota is deducted.  Matches the "Billed amount" column in the
        GitHub billing UI.  For the same user the value is 150 × $0.04 = $6.00.
        0.0 when ``netAmount`` is absent in the response.
      - ``model_requests``:   login → {model_name → total grossQuantity} breakdown of
        requests per AI model.  Combines included and billed requests (grossQuantity).
        The model name is taken from the ``model`` field of each usage item, falling back
        to ``sku`` or ``skuName`` when ``model`` is absent.

    All dicts are empty when the endpoint is unavailable (e.g. the token does not have
    billing-manager scope, or the enterprise does not use the enhanced billing platform).

    Error-handling policy:
    - HTTP 403 or 501 → the whole endpoint is unavailable; abort and return ({}, {}, {})
      so callers can fall back gracefully.
    - HTTP 400 or 404 for a specific user → that user has no billing record this
      month; record 0 for them and continue with the remaining users.
    - Other non-2xx responses → log a warning, skip that user, continue.
    """
    if not logins:
        return {}, {}, {}

    url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/settings/billing/premium_request/usage"

    result: Dict[str, float] = {}
    billed: Dict[str, float] = {}
    model_requests: Dict[str, Dict[str, float]] = {}
    endpoint_available = True
    period_logged = False

    print(f"  Querying billing API for {len(logins)} user(s) ({year}-{month:02d}) …")
    for idx, login in enumerate(logins, start=1):
        params: Dict[str, Any] = {"year": year, "month": month, "user": login}
        try:
            resp = gh_get(url, headers=HEADERS_JSON, params=params, timeout=90)
        except requests.exceptions.RequestException as exc:
            print(f"  [WARN] Request error fetching billing data for {login}: {exc}")
            continue

        # 403 Forbidden  → token lacks billing-manager scope; no point retrying
        # 501 Not Implemented → endpoint not available for this enterprise
        # Both mean the endpoint is entirely unavailable for all users.
        if resp.status_code in (403, 501):
            print(
                f"  [INFO] Billing premium-request API returned HTTP {resp.status_code} "
                f"(user={login}); endpoint unavailable – billing columns will be empty."
            )
            endpoint_available = False
            break

        # 400 Bad Request or 404 Not Found for a specific user means GitHub has
        # no billing record for that user this month (e.g. they made no premium
        # requests, or they are not enrolled in the enhanced billing platform).
        # Treat as 0 and move on so the rest of the batch is not aborted.
        if resp.status_code in (400, 404):
            print(
                f"  [INFO] Billing API returned HTTP {resp.status_code} for user={login}; "
                f"treating as 0 premium requests for this month."
            )
            result[login] = 0.0
            billed[login] = 0.0
            model_requests[login] = {}
            continue

        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  [WARN] Could not parse billing response for {login}: {exc}")
            continue

        # Log the time period and currency from the first successful response to
        # confirm the API is returning data for the expected month (aids debugging).
        if not period_logged:
            time_period = data.get("timePeriod") or {}
            # The billing API timePeriod response only includes 'year'; use the requested
            # month directly.  The API does not return a currency field.
            print(
                f"  [INFO] Billing API confirmed period: "
                f"year={time_period.get('year', year)}, month={month:02d} "
                f"(requested {year}-{month:02d})"
            )
            period_logged = True

        usage_items = data.get("usageItems") or []
        total_qty = 0.0
        total_billed = 0.0
        user_model_counts: Dict[str, float] = {}
        for item in usage_items:
            if not isinstance(item, dict):
                continue
            qty = to_num(item.get("grossQuantity"))
            total_qty += qty
            # netAmount = actual amount charged after included-request quota → "Billed amount" in GitHub UI.
            total_billed += to_num(item.get("netAmount"))
            # Extract per-model breakdown.  Try "model" first (current API field name),
            # then fall back to "sku" / "skuName" / "modelName" for older or alternate
            # API shapes that may encode the model inside a SKU identifier string.
            model_name = (
                str(item.get("model") or item.get("sku") or item.get("skuName") or item.get("modelName") or "").strip()
            )
            if model_name:
                user_model_counts[model_name] = user_model_counts.get(model_name, 0.0) + qty

        result[login] = total_qty
        billed[login] = total_billed
        model_requests[login] = user_model_counts

        if idx % 20 == 0:
            print(f"  … {idx}/{len(logins)} users processed")

    if not endpoint_available:
        return {}, {}, {}

    print(f"  Billing API: premium request data fetched for {len(result)} user(s) ({year}-{month:02d}).")
    return result, billed, model_requests


def is_active(last_activity_at):
    if not last_activity_at:
        return "inactive"
    try:
        last_activity = datetime.fromisoformat(last_activity_at.replace("Z", "+00:00"))
        now = datetime.now(last_activity.tzinfo)
        return "active" if (now - last_activity) <= timedelta(days=30) else "inactive"
    except Exception:
        return "inactive"

# -------------------------
# Enterprise teams & memberships
# -------------------------
def fetch_enterprise_teams():
    url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/teams"
    return fetch_rest_list_paged(url, headers=HEADERS_JSON, keys=("teams", "items", "data"), per_page=100)

def fetch_enterprise_team_memberships(team_slug):
    url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/teams/{team_slug}/memberships"
    return fetch_rest_list_paged(url, headers=HEADERS_JSON, keys=("memberships", "items", "data"), per_page=100)

def parse_membership_login(m):
    if not isinstance(m, dict):
        return ""
    for path in (("user", "login"), ("member", "login"), ("login",), ("user",), ("member",)):
        cur = m
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""

# -------------------------
# Copilot metrics report (users-28-day/latest)
# Used for interactions, completions, LOC and other rolling-window metrics.
# Premium requests are overridden below by the monthly billing API.
# -------------------------
def dump_json(obj: Any, name: str) -> None:
    if not DEBUG:
        return
    path = f"{DEBUG_PREFIX}_{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    print(f"[DEBUG] wrote {path}")

def dump_text(text: str, name: str) -> None:
    if not DEBUG:
        return
    path = f"{DEBUG_PREFIX}_{name}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[DEBUG] wrote {path}")

def get_json_from_api(url: str) -> Any:
    r = gh_get(url, headers=HEADERS_JSON, params=None, timeout=90)
    r.raise_for_status()
    return r.json()

def extract_download_urls_from_manifest(latest_payload: Any) -> List[str]:
    if not isinstance(latest_payload, dict):
        return []
    dl = latest_payload.get("download_links")
    if not dl:
        return []

    urls: List[str] = []

    def add(v: Any):
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)

    if isinstance(dl, list):
        for item in dl:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                for k in ("url", "download_url", "location", "href"):
                    add(item.get(k))
    elif isinstance(dl, dict):
        for v in dl.values():
            if isinstance(v, str):
                add(v)
            elif isinstance(v, dict):
                for k in ("url", "download_url", "location", "href"):
                    add(v.get(k))
            elif isinstance(v, list):
                for x in v:
                    add(x)

    out: List[str] = []
    seen: Set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def choose_report_url(urls: List[str]) -> str:
    if not urls:
        raise RuntimeError("No download URLs found in manifest download_links.")
    for u in urls:
        lu = u.lower()
        if lu.endswith(".json") or "json" in lu:
            return u
    return urls[0]

def download_all_report_urls(urls: List[str]) -> List[Dict[str, Any]]:
    """Download and parse all report URLs from the manifest, combining rows from every file.

    The GitHub 28-day report manifest can contain multiple ``download_links`` entries –
    for example, one file for IDE completions and a separate file for chat/agent
    interactions.  The original code called ``choose_report_url()`` which returned only
    the *first* matching URL, silently dropping every other data file.  This caused
    premium-request counts (and other aggregated metrics) to reflect only a subset of
    the user's activity, typically producing values roughly half the true total.

    This helper iterates over *all* URLs, downloads each file, parses it, and returns
    the combined list of rows so that ``aggregate_users()`` sees the complete dataset.
    """
    if not urls:
        raise RuntimeError("No download URLs found in manifest download_links.")
    all_rows: List[Dict[str, Any]] = []
    for report_url in urls:
        print(f"[REPORT] downloading report from: {report_url}")
        text = download_report_as_text(report_url)
        if DEBUG:
            dump_text(text[:20000], "report_head")
        rows = parse_report_payload(text)
        all_rows.extend(rows)
    return all_rows

def download_report_as_text(url: str) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(1, 7):
        try:
            r = requests.get(url, allow_redirects=True, timeout=180)
            if r.status_code in (401, 403):
                r = requests.get(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, allow_redirects=True, timeout=180)
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(30, 2 * attempt)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            wait = min(30, 2 * attempt)
            print(f"[WARN] Report download attempt {attempt} failed: {exc}. Retrying in {wait}s …")
            time.sleep(wait)
    raise RuntimeError(f"Failed to download report from {url} after 6 attempts") from last_exc

def parse_report_payload(text: str) -> List[Dict[str, Any]]:
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return [r for r in obj if isinstance(r, dict)]
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return [r for r in v if isinstance(r, dict)]
            return [obj]
    except json.JSONDecodeError:
        pass

    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"[WARN] Skipping malformed NDJSON line: {line[:120]!r}")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows

def download_latest_users_28_day_report_rows() -> List[Dict[str, Any]]:
    latest_url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/copilot/metrics/reports/users-28-day/latest"
    try:
        latest_payload = get_json_from_api(latest_url)
    except requests.exceptions.RequestException as exc:
        print(f"[WARN] Could not fetch Copilot metrics report: {exc}. Metrics columns will be empty.")
        return []
    dump_json(latest_payload, "latest_payload")

    if isinstance(latest_payload, dict) and "download_links" in latest_payload:
        urls = extract_download_urls_from_manifest(latest_payload)
        rows = download_all_report_urls(urls)
        if DEBUG:
            dump_json(rows[:5], "report_rows_first5")
        return rows

    if isinstance(latest_payload, list):
        return [r for r in latest_payload if isinstance(r, dict)]
    if isinstance(latest_payload, dict):
        for _, v in latest_payload.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [r for r in v if isinstance(r, dict)]
        return [latest_payload]
    return []

def to_num(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return float(int(v))
        return float(v)
    except Exception:
        return 0.0

# -------------------------
# Premium-request model helpers
# -------------------------
# Models included at no premium-request cost for Copilot Business / Enterprise paid plans.
# Any model whose name does NOT start with one of these prefixes is treated as a premium model.
# Source: https://docs.github.com/en/copilot/concepts/billing/copilot-requests
_COPILOT_INCLUDED_MODEL_PREFIXES: tuple = (
    "gpt-4o",       # includes gpt-4o, gpt-4o-mini, gpt-4o-2024-*
    "gpt-4.1",      # includes gpt-4.1 and any date-versioned variants
    "gpt-5-mini",   # gpt-5-mini
    "gpt-5mini",    # alternate hyphen-less spelling
    "default",      # "default" slot maps to the included base model
)

def _is_included_model(model_name: str) -> bool:
    """Return True when the model consumes 0 premium requests on paid Copilot plans."""
    m = (model_name or "").lower().strip()
    return any(m.startswith(p) for p in _COPILOT_INCLUDED_MODEL_PREFIXES)

def top_key(counter: Dict[str, float]) -> str:
    if not counter:
        return ""
    return max(counter.items(), key=lambda kv: kv[1])[0]

def format_feature_name(raw: str) -> str:
    if not raw:
        return "Unknown"
    name = raw
    if name.startswith("chat_panel_"):
        name = name[len("chat_panel_"):]
    if name.endswith("_mode"):
        name = name[:-len("_mode")]
    name = " ".join([p.capitalize() for p in name.split("_") if p])
    overrides = {
        "Chat Inline": "Inline Chat",
        "Agent": "Agent",
        "Plan": "Plan",
        "Ask": "Ask",
        "Edit": "Edit",
        "Custom": "Custom",
    }
    return overrides.get(name, name)

def format_language_loc(lang_dict: Dict[str, float]) -> str:
    """Format per-language LOC counts as a human-readable string, e.g. 'java 260, python 56'."""
    if not lang_dict:
        return ""
    sorted_items = sorted(lang_dict.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{lang} {int(v)}" for lang, v in sorted_items if v > 0)

def format_model_premium_requests(model_dict: Dict[str, float]) -> str:
    """Format per-model premium request counts as a human-readable string.

    Combines included and billed requests (both come from grossQuantity).
    Returns a string like 'claude-sonnet-4 - 10, gpt-5.1 - 3' sorted by count descending.
    Returns an empty string when the dict is empty or all counts are zero.
    """
    if not model_dict:
        return ""
    sorted_items = sorted(model_dict.items(), key=lambda kv: kv[1], reverse=True)
    parts = []
    for model, count in sorted_items:
        if count > 0:
            # The billing API occasionally returns fractional grossQuantity values
            # (e.g. 685.40) when the included-request credit is non-integer.
            # Display as an integer when the value is whole, otherwise round to 2 dp.
            count_str = str(int(count)) if count == int(count) else str(round(count, 2))
            parts.append(f"{model} - {count_str}")
    return ", ".join(parts)

def get_loc_field_value(row: Dict[str, Any], new_field: str, old_field: str) -> float:
    """
    Helper to extract LoC field value from API response.
    Tries new field name first (e.g., loc_suggested_to_add_sum, loc_added_sum, loc_deleted_sum),
    falls back to old name (e.g., loc_suggested, loc_added, loc_deleted).
    Returns the numeric value using to_num(). Correctly handles zero values.
    """
    if new_field in row:
        return to_num(row[new_field])
    return to_num(row.get(old_field))

def normalize_feature_name(feature_value: Optional[str]) -> str:
    """
    Normalize feature name to lowercase for consistent lookups.
    Returns DEFAULT_FEATURE_NAME if feature_value is None or empty.
    Note: format_feature_name() handles display formatting (capitalization).
    """
    return (feature_value or DEFAULT_FEATURE_NAME).lower()

_INTERACTION_TOP_FIELDS: tuple[str, ...] = (
    "user_initiated_interaction_count",
    "interaction_count",
    "interactions_count",
    "total_interactions",
    "total_chats",
    "copilot_total_requests",
)

_CHAT_INTERACTION_FIELDS: tuple[str, ...] = (
    "total_chats",
    "total_chat_turns",
    "total_chat_messages",
    "total_chat_interactions",
)

_COPILOT_CHAT_SECTIONS: tuple[str, ...] = (
    "copilot_ide_chat",
    "copilot_dotcom_chat",
    "copilot_cli",
    "copilot_mobile_chat",
)

def get_first_numeric_field_with_presence(row: Dict[str, Any], fields: tuple[str, ...]) -> Tuple[bool, float]:
    """Return whether any field is present and the first present numeric value.

    The boolean distinguishes missing fields from explicit zero values:
    explicit zero returns (True, 0.0), while a missing field returns (False, 0.0).
    The interaction aggregation can still intentionally treat explicit zero
    top-level interactions as incomplete data and fall back to detailed activity
    breakdowns because the report otherwise shows users with real Copilot activity
    as having no interactions.
    """
    for field_name in fields:
        if row.get(field_name) is not None:
            return True, to_num(row.get(field_name))
    return False, 0.0

def sum_nested_numeric_fields(obj: Any, fields: tuple[str, ...]) -> float:
    """Sum numeric fields in a nested API object without double-counting parent totals.

    None or unsupported values return 0.0.  When both parent and child totals are
    present, child totals are used and parent totals are ignored to avoid counting
    the same activity twice.
    """
    if isinstance(obj, list):
        return sum(sum_nested_numeric_fields(item, fields) for item in obj)
    if not isinstance(obj, dict):
        return 0.0

    child_total = sum(
        sum_nested_numeric_fields(value, fields)
        for key, value in obj.items()
        if key not in fields
    )
    if child_total > 0:
        return child_total
    return sum(to_num(obj.get(field_name)) for field_name in fields)

def sum_copilot_chat_interactions(row: Dict[str, Any]) -> float:
    """Sum chat-style Copilot interactions from current nested metrics API sections."""
    return sum(sum_nested_numeric_fields(row.get(section), _CHAT_INTERACTION_FIELDS) for section in _COPILOT_CHAT_SECTIONS)

@dataclass
class UserAgg:
    user: str
    interactions: float = 0.0
    completions: float = 0.0
    acceptances: float = 0.0
    days: Set[str] = field(default_factory=set)

    loc_suggested: float = 0.0
    loc_added: float = 0.0
    loc_deleted: float = 0.0

    premium_requests: float = 0.0
    billed_amount: float = 0.0  # Amount charged this calendar month (from billing API netAmount/grossAmount; currency as returned by the API)

    model_counts: Dict[str, float] = field(default_factory=dict)
    language_counts: Dict[str, float] = field(default_factory=dict)
    feature_counts: Dict[str, float] = field(default_factory=dict)

    language_loc_suggested: Dict[str, float] = field(default_factory=dict)
    language_loc_added: Dict[str, float] = field(default_factory=dict)

    # Per-feature per-language LOC (inline and agent breakdowns)
    language_loc_suggested_inline: Dict[str, float] = field(default_factory=dict)
    language_loc_added_inline: Dict[str, float] = field(default_factory=dict)
    language_loc_suggested_agent: Dict[str, float] = field(default_factory=dict)
    language_loc_added_agent: Dict[str, float] = field(default_factory=dict)

    # Per-feature LoC tracking for refined acceptance percentage calculation
    feature_loc_suggested: Dict[str, float] = field(default_factory=dict)
    feature_loc_added: Dict[str, float] = field(default_factory=dict)
    feature_loc_deleted: Dict[str, float] = field(default_factory=dict)

def get_user_login_from_row(row: Dict[str, Any]) -> str:
    v = row.get("user_login")
    if isinstance(v, str) and v:
        return v
    for k in ("login", "username", "user"):
        v = row.get(k)
        if isinstance(v, str) and v:
            return v
    u = row.get("user")
    if isinstance(u, dict):
        v = u.get("login") or u.get("username")
        if isinstance(v, str) and v:
            return v
    return ""

def aggregate_users(rows: List[Dict[str, Any]]) -> Dict[str, UserAgg]:
    users: Dict[str, UserAgg] = {}

    for r in rows:
        login = get_user_login_from_row(r)
        if not login:
            continue

        agg = users.get(login)
        if not agg:
            agg = UserAgg(user=login)
            users[login] = agg

        # Track whether a top-level interaction count was provided for this row.
        # If missing or zero, fall back to the detailed feature/model/chat breakdowns below.
        has_top_level_interactions, top_level_interactions = get_first_numeric_field_with_presence(r, _INTERACTION_TOP_FIELDS)
        if has_top_level_interactions and top_level_interactions > 0:
            agg.interactions += top_level_interactions
        fallback_interactions_from_models = 0.0
        fallback_interactions_from_features = 0.0
        fallback_interactions_from_nested_chat = sum_copilot_chat_interactions(r)
        agg.completions += to_num(r.get("code_generation_activity_count"))
        agg.acceptances += to_num(r.get("code_acceptance_activity_count"))

        # Premium requests: try explicit field names first (forward-compat with future API shapes),
        # then fall back to counting interactions with non-included (premium) models derived from
        # totals_by_model_feature.  The current users-28-day NDJSON schema does not emit a
        # dedicated top-level premium-request field; the model-based estimation below is the
        # primary path.  None of these approaches apply a per-model multiplier, so the count
        # represents the number of premium-model interactions rather than billed request units.
        _EXPLICIT_PREMIUM_TOP_FIELDS = (
            "copilot_premium_requests",
            "total_premium_requests_count",
            "premium_requests_count",
            "premium_interaction_count",
        )
        has_explicit_top_premium = any(r.get(k) is not None for k in _EXPLICIT_PREMIUM_TOP_FIELDS)
        premium_row = (
            to_num(r.get("copilot_premium_requests"))
            or to_num(r.get("total_premium_requests_count"))
            or to_num(r.get("premium_requests_count"))
            or to_num(r.get("premium_interaction_count"))
        )
        agg.premium_requests += premium_row

        # Day tracking: support both 'day' (nested format) and 'date' (flat NDJSON format).
        day = r.get("day") or r.get("date")
        if isinstance(day, str) and day:
            agg.days.add(day[:10])

        tmm = r.get("totals_by_model_feature")
        if isinstance(tmm, list):
            for mf in tmm:
                if not isinstance(mf, dict):
                    continue
                model = mf.get("model") or "unknown"
                _, interaction_count = get_first_numeric_field_with_presence(mf, _INTERACTION_TOP_FIELDS)
                fallback_interactions_from_models += interaction_count
                agg.model_counts[model] = agg.model_counts.get(model, 0.0) + interaction_count

                # Accumulate premium requests only when no top-level explicit field is present.
                # We check key *presence* (not just truthiness) so that an explicit value of 0
                # (meaning the API actively reported zero) is respected without triggering the
                # model-based fallback below.
                if not has_explicit_top_premium:
                    # 1. Try explicit per-model premium count fields (may appear in future API versions).
                    _EXPLICIT_PREMIUM_MF_FIELDS = (
                        "copilot_premium_requests",
                        "premium_request_count",
                        "premium_requests_count",
                    )
                    has_explicit_mf_premium = any(mf.get(k) is not None for k in _EXPLICIT_PREMIUM_MF_FIELDS)
                    mf_premium = (
                        to_num(mf.get("copilot_premium_requests"))
                        or to_num(mf.get("premium_request_count"))
                        or to_num(mf.get("premium_requests_count"))
                    )
                    if mf_premium:
                        agg.premium_requests += mf_premium
                    elif not has_explicit_mf_premium and model != "unknown" and not _is_included_model(model):
                        # 2. Model-based fallback: count every interaction with a non-included
                        #    (premium) model as one premium request.  This undercounts for models
                        #    with a multiplier greater than 1× but gives correct non-zero values
                        #    for all users who actively use premium models.
                        agg.premium_requests += interaction_count


        else:
            # Flat NDJSON format: model is a top-level field per row.
            model = r.get("model")
            if isinstance(model, str) and model:
                count = to_num(r.get("user_initiated_interaction_count")) or to_num(r.get("copilot_total_requests"))
                agg.model_counts[model] = agg.model_counts.get(model, 0.0) + count
                # If the flat format has no top-level premium field, use model-based estimation.
                if not has_explicit_top_premium and not _is_included_model(model):
                    agg.premium_requests += count

        tlf = r.get("totals_by_language_feature")
        if isinstance(tlf, list):
            for lf in tlf:
                if not isinstance(lf, dict):
                    continue
                lang = lf.get("language") or "unknown"
                val = to_num(lf.get("user_initiated_interaction_count"))
                if val == 0:
                    val = to_num(lf.get("code_generation_activity_count"))
                agg.language_counts[lang] = agg.language_counts.get(lang, 0.0) + val
                loc_sug = to_num(lf.get("loc_suggested_to_add_sum") if "loc_suggested_to_add_sum" in lf else lf.get("loc_suggested"))
                loc_add = to_num(lf.get("loc_added_sum") if "loc_added_sum" in lf else lf.get("loc_added"))
                if loc_sug:
                    agg.language_loc_suggested[lang] = agg.language_loc_suggested.get(lang, 0.0) + loc_sug
                if loc_add:
                    agg.language_loc_added[lang] = agg.language_loc_added.get(lang, 0.0) + loc_add
                # Route to per-feature-language dicts using the feature field on each entry.
                feat_lf = normalize_feature_name(lf.get("feature"))
                _route_language_loc(agg, feat_lf, lang, loc_sug, loc_add)
        else:
            # Flat NDJSON format: language is a top-level field per row.
            lang = r.get("language")
            if isinstance(lang, str) and lang:
                val = to_num(r.get("user_initiated_interaction_count")) or to_num(r.get("copilot_total_requests"))
                agg.language_counts[lang] = agg.language_counts.get(lang, 0.0) + val
                loc_sug = to_num(r.get("loc_suggested_to_add_sum") if "loc_suggested_to_add_sum" in r else r.get("loc_suggested"))
                loc_add = to_num(r.get("loc_added_sum") if "loc_added_sum" in r else r.get("loc_added"))
                if loc_sug:
                    agg.language_loc_suggested[lang] = agg.language_loc_suggested.get(lang, 0.0) + loc_sug
                if loc_add:
                    agg.language_loc_added[lang] = agg.language_loc_added.get(lang, 0.0) + loc_add
                # Route to per-feature-language dicts using the top-level feature field.
                feat_flat = normalize_feature_name(r.get("feature"))
                _route_language_loc(agg, feat_flat, lang, loc_sug, loc_add)

        tbf = r.get("totals_by_feature")
        if isinstance(tbf, list):
            for f in tbf:
                if not isinstance(f, dict):
                    continue
                feat = normalize_feature_name(f.get("feature"))
                _, feat_interaction_count = get_first_numeric_field_with_presence(f, _INTERACTION_TOP_FIELDS)
                fallback_interactions_from_features += feat_interaction_count
                agg.feature_counts[feat] = agg.feature_counts.get(feat, 0.0) + feat_interaction_count

                # Store LoC per feature for refined acceptance percentage calculation.
                # Use get_loc_field_value so that both new field names
                # (loc_suggested_to_add_sum, loc_added_sum, loc_deleted_sum) and the
                # older aliases (loc_suggested, loc_added, loc_deleted) are handled.
                loc_suggested_val = get_loc_field_value(f, "loc_suggested_to_add_sum", "loc_suggested")
                loc_added_val = get_loc_field_value(f, "loc_added_sum", "loc_added")
                loc_deleted_val = get_loc_field_value(f, "loc_deleted_sum", "loc_deleted")

                agg.feature_loc_suggested[feat] = agg.feature_loc_suggested.get(feat, 0.0) + loc_suggested_val
                agg.feature_loc_added[feat] = agg.feature_loc_added.get(feat, 0.0) + loc_added_val
                agg.feature_loc_deleted[feat] = agg.feature_loc_deleted.get(feat, 0.0) + loc_deleted_val
                
                agg.loc_suggested += loc_suggested_val
                agg.loc_added += loc_added_val
                agg.loc_deleted += loc_deleted_val
        else:
            # Flat NDJSON format: feature and LOC fields are top-level per row.
            # Note: 'unknown' is an intentional catch-all for rows without feature data
            feat = normalize_feature_name(r.get("feature"))

            _, val = get_first_numeric_field_with_presence(r, _INTERACTION_TOP_FIELDS)
            fallback_interactions_from_features += val
            agg.feature_counts[feat] = agg.feature_counts.get(feat, 0.0) + val
            
            # Store LoC per feature for refined acceptance percentage calculation
            # Flat NDJSON format supports both old and new field names (fallback logic via helper)
            loc_suggested_val = get_loc_field_value(r, "loc_suggested_to_add_sum", "loc_suggested")
            loc_added_val = get_loc_field_value(r, "loc_added_sum", "loc_added")
            loc_deleted_val = get_loc_field_value(r, "loc_deleted_sum", "loc_deleted")

            agg.feature_loc_suggested[feat] = agg.feature_loc_suggested.get(feat, 0.0) + loc_suggested_val
            agg.feature_loc_added[feat] = agg.feature_loc_added.get(feat, 0.0) + loc_added_val
            agg.feature_loc_deleted[feat] = agg.feature_loc_deleted.get(feat, 0.0) + loc_deleted_val
            
            agg.loc_suggested += loc_suggested_val
            agg.loc_added += loc_added_val
            agg.loc_deleted += loc_deleted_val

        if not has_top_level_interactions or top_level_interactions == 0:
            fallback_interactions = 0.0
            # Choose the first non-zero detailed count so an empty feature breakdown
            # does not hide useful model or nested chat activity.
            for fallback_source in (
                fallback_interactions_from_features,
                fallback_interactions_from_models,
                fallback_interactions_from_nested_chat,
            ):
                if fallback_source:
                    fallback_interactions = fallback_source
                    break
            agg.interactions += fallback_interactions

    return users

def _sum_feature_loc(loc_dict: Dict[str, float], feature_set: frozenset[str]) -> int:
    """Sum LOC values from *loc_dict* for all features in *feature_set*."""
    return int(sum(loc_dict.get(feat, 0.0) for feat in feature_set))


def _route_language_loc(agg: "UserAgg", feat: str, lang: str, loc_sug: float, loc_add: float) -> None:
    """Accumulate *loc_sug* / *loc_add* into the appropriate per-feature-language dict on *agg*."""
    if feat in _INLINE_FEATURES:
        if loc_sug:
            agg.language_loc_suggested_inline[lang] = agg.language_loc_suggested_inline.get(lang, 0.0) + loc_sug
        if loc_add:
            agg.language_loc_added_inline[lang] = agg.language_loc_added_inline.get(lang, 0.0) + loc_add
    elif feat in _AGENT_FEATURES:
        if loc_sug:
            agg.language_loc_suggested_agent[lang] = agg.language_loc_suggested_agent.get(lang, 0.0) + loc_sug
        if loc_add:
            agg.language_loc_added_agent[lang] = agg.language_loc_added_agent.get(lang, 0.0) + loc_add


def metrics_row_for_user(agg: Optional["UserAgg"]) -> dict:
    # When no metrics data exists for a user, return 0 for numeric columns and empty
    # string for text columns.  This ensures that users with active_status="active"
    # (derived from the seat API's last_activity_at) but no metrics API data still
    # show explicit zeros rather than blank cells.
    #
    # Note: The metrics API and seat API are independent data sources.  A user can
    # have recent activity recorded by the seat API (making them "active") but no
    # corresponding metrics data due to:
    # - Metrics API lag or data retention differences
    # - Activity in features not captured by the metrics API
    # - API permission/visibility differences
    if not agg:
        return {
            "metrics_interactions_28d": 0,
            "metrics_completions_28d": 0,
            "metrics_acceptances_28d": 0,
            "metrics_acceptance_pct_28d": 0,
            "metrics_days_active_28d": 0,
            "metrics_loc_suggested_28d": 0,
            "metrics_loc_added_28d": 0,
            "metrics_loc_deleted_28d": 0,
            "metrics_loc_suggested_inline_28d": 0,
            "metrics_loc_added_inline_28d": 0,
            "metrics_loc_acceptance_pct_inline_28d": 0,
            "metrics_loc_suggested_chat_28d": 0,
            "metrics_loc_added_chat_28d": 0,
            "metrics_loc_suggested_edit_28d": 0,
            "metrics_loc_added_edit_28d": 0,
            "metrics_loc_suggested_agent_28d": 0,
            "metrics_loc_added_agent_28d": 0,
            "metrics_top_model_28d": "",
            "metrics_top_language_28d": "",
            "metrics_top_feature_28d": "",
            "metrics_loc_suggested_by_language_total_28d": "",
            "metrics_loc_added_by_language_total_28d": "",
            "metrics_loc_suggested_by_language_inline_28d": "",
            "metrics_loc_added_by_language_inline_28d": "",
            "metrics_loc_suggested_by_language_agent_28d": "",
            "metrics_loc_added_by_language_agent_28d": "",
        }

    acceptance_pct = (agg.acceptances / agg.completions * 100.0) if agg.completions > 0 else 0.0
    
    # Calculate inline-only LoC metrics (code_completion ghost-text suggestions only).
    # Only _INLINE_FEATURES (code_completion) is included here.  Chat, edit, and agent
    # features are tracked separately in their own breakdown columns; including them here
    # would double-count them when a caller sums the four breakdown columns.
    # Formula: (added / suggested) × 100
    # - Example: Copilot suggested 100 lines, developer accepted 80 lines → 80%
    # - Example: Copilot suggested 100 lines, developer accepted and expanded to 150 lines → 150%
    # Note: Values >100% indicate the developer accepted the suggestion and added more code on top.
    inline_loc_suggested = int(sum(agg.feature_loc_suggested.get(feat, 0.0) for feat in _INLINE_FEATURES))
    inline_loc_added = int(sum(agg.feature_loc_added.get(feat, 0.0) for feat in _INLINE_FEATURES))

    # Calculate traditional acceptance rate: what % of suggested code was accepted/added
    loc_acceptance_pct_inline = (inline_loc_added / inline_loc_suggested * 100.0) if inline_loc_suggested > 0 else 0.0

    # Pre-compute per-category values used in both the total and breakdown columns so the
    # total is always identical to the sum of its parts.
    chat_loc_suggested = _sum_feature_loc(agg.feature_loc_suggested, _CHAT_FEATURES)
    chat_loc_added = _sum_feature_loc(agg.feature_loc_added, _CHAT_FEATURES)
    edit_loc_suggested = _sum_feature_loc(agg.feature_loc_suggested, _EDIT_FEATURES)
    edit_loc_added = _sum_feature_loc(agg.feature_loc_added, _EDIT_FEATURES)
    # Agent suggested LOC: use only the real loc_suggested_to_add_sum value for all agent
    # features (including agent_edit).  agent_edit returns 0 from the API because it writes
    # directly to files and has no suggestion UI, but we report that 0 faithfully rather than
    # substituting a proxy so that this column reflects only genuine suggestion data.
    agent_loc_suggested = _sum_feature_loc(agg.feature_loc_suggested, _AGENT_FEATURES)
    agent_loc_added = _sum_feature_loc(agg.feature_loc_added, _AGENT_FEATURES)

    # NOTE: metrics_loc_suggested_28d is the sum of its four breakdown columns
    # (inline + chat + edit + agent).  All four use only the raw loc_suggested_to_add_sum
    # value from the API — no proxy is applied.  agent_edit will therefore contribute 0
    # because the GitHub API does not populate loc_suggested_to_add_sum for direct file
    # writes, but that accurately reflects what the API reports as "suggested".
    # For accurate acceptance percentage, use: metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d
    return {
        "metrics_interactions_28d": int(agg.interactions),
        "metrics_completions_28d": int(agg.completions),
        "metrics_acceptances_28d": int(agg.acceptances),
        "metrics_acceptance_pct_28d": round(acceptance_pct, 2),
        "metrics_days_active_28d": len(agg.days),
        "metrics_loc_suggested_28d": inline_loc_suggested + chat_loc_suggested + edit_loc_suggested + agent_loc_suggested,
        "metrics_loc_added_28d": inline_loc_added + chat_loc_added + edit_loc_added + agent_loc_added,
        "metrics_loc_deleted_28d": int(agg.loc_deleted),
        "metrics_loc_suggested_inline_28d": inline_loc_suggested,
        "metrics_loc_added_inline_28d": inline_loc_added,
        "metrics_loc_acceptance_pct_inline_28d": round(loc_acceptance_pct_inline, 2),
        "metrics_loc_suggested_chat_28d": chat_loc_suggested,
        "metrics_loc_added_chat_28d": chat_loc_added,
        # chat_panel_edit_mode (and legacy "edit"/"edit_mode") populate
        # loc_suggested_to_add_sum with the code-block lines that Copilot showed
        # in the edit-mode chat panel before the user applied them.
        "metrics_loc_suggested_edit_28d": edit_loc_suggested,
        "metrics_loc_added_edit_28d": edit_loc_added,
        # Agent suggested LOC: raw loc_suggested_to_add_sum from the API for all agent features.
        # agent_edit contributes 0 because it writes directly to files (no suggestion UI).
        "metrics_loc_suggested_agent_28d": agent_loc_suggested,
        # loc_added_sum for agent features captures code applied from the agent/plan-mode
        # chat panel and all lines written directly to files (via agent_edit).
        "metrics_loc_added_agent_28d": agent_loc_added,
        "metrics_top_model_28d": top_key(agg.model_counts),
        "metrics_top_language_28d": top_key(agg.language_counts),
        "metrics_top_feature_28d": format_feature_name(top_key(agg.feature_counts)),
        "metrics_loc_suggested_by_language_total_28d": format_language_loc(agg.language_loc_suggested),
        "metrics_loc_added_by_language_total_28d": format_language_loc(agg.language_loc_added),
        "metrics_loc_suggested_by_language_inline_28d": format_language_loc(agg.language_loc_suggested_inline),
        "metrics_loc_added_by_language_inline_28d": format_language_loc(agg.language_loc_added_inline),
        "metrics_loc_suggested_by_language_agent_28d": format_language_loc(agg.language_loc_suggested_agent),
        "metrics_loc_added_by_language_agent_28d": format_language_loc(agg.language_loc_added_agent),
    }

# -------------------------
# Email helpers
# -------------------------
def slug_to_env_name(team_slug: str) -> str:
    """Convert a team slug to the corresponding env-var name for the team email.

    The enterprise namespace prefix (the part before the first ``:``) is stripped
    because GitHub enterprise team slugs are sometimes prefixed with the enterprise
    name (e.g. ``ent:accelerator-copilot``).  Only the local part after the colon
    is used so that the secret name remains stable regardless of the enterprise slug.

    Examples::

        "accelerator-copilot"      →  "ACCELERATOR_COPILOT_TEAM_EMAIL"
        "nt-copilot"               →  "NT_COPILOT_TEAM_EMAIL"
        "ent:genesis-copilot"      →  "GENESIS_COPILOT_TEAM_EMAIL"
    """
    local_slug = team_slug.split(":", 1)[-1] if ":" in team_slug else team_slug
    return re.sub(r"[^A-Z0-9]+", "_", local_slug.upper()).strip("_") + "_TEAM_EMAIL"


def get_team_head_email(team_index: int, team_slug: str = "") -> str:
    """Return the head email(s) for the given team.

    Lookup strategy (first non-empty value wins):

    1. ``{SLUG_UPPER}_TEAM_EMAIL`` – slug-derived name, e.g.
       ``ACCELERATOR_COPILOT_TEAM_EMAIL`` for slug ``accelerator-copilot``.
       See :func:`slug_to_env_name` for the conversion rules.
    2. ``TEAM{team_index}_HEAD_EMAIL`` – legacy positional name
       (e.g. ``TEAM1_HEAD_EMAIL``, ``TEAM2_HEAD_EMAIL``, …).

    The value may be a single address or a comma-separated list of addresses
    (e.g. ``"alice@example.com, bob@example.com"``); the raw string is returned
    as-is and ``send_report_email`` handles splitting and validation.
    Returns an empty string when neither variable is set.
    """
    if team_slug:
        value = os.getenv(slug_to_env_name(team_slug), "").strip()
        if value:
            return value
    # Fall back to positional env var for backward compatibility.
    return os.getenv(f"TEAM{team_index}_HEAD_EMAIL", "").strip()


def send_report_email(to_addr: str, csv_path: str, team_name: str, date_str: str) -> None:
    """Send the team CSV report as an email attachment.

    *to_addr* may contain a single address or multiple comma-separated
    addresses (e.g. ``"alice@example.com, bob@example.com"``).  The report is
    delivered to every address in the list.

    Uses SMTP STARTTLS with username/password authentication (``SMTP_SERVER``,
    ``SMTP_USERNAME``, ``SMTP_PASSWORD``, ``SENDER_EMAIL``).

    Silently skips when the required SMTP settings are absent or *to_addr* is
    empty.  Errors during sending are logged as warnings so they never abort
    the overall report generation.
    """
    # Support multiple comma-separated recipients; skip any malformed addresses.
    recipients = [addr.strip() for addr in to_addr.split(",") if addr.strip()]
    valid_recipients = [addr for addr in recipients if "@" in addr]
    invalid = [addr for addr in recipients if "@" not in addr]
    if invalid:
        print(
            f"  [WARN] Skipping malformed recipient address(es) for team '{team_name}': "
            f"{', '.join(invalid)}"
        )
    recipients = valid_recipients
    if not recipients:
        print(f"  [INFO] No recipient email configured for team '{team_name}' – skipping email.")
        return

    subject = f"Copilot Metrics Report – {team_name} ({date_str})"
    body = (
        f"Hi,\n\n"
        f"Please find attached the Copilot metrics report for team '{team_name}' "
        f"generated on {date_str}.\n\n"
        f"This report is auto-generated and sent daily.\n\n"
        f"─────────────────────────────────────────\n"
        f"COLUMN GLOSSARY\n"
        f"─────────────────────────────────────────\n"
        f"Identity & Team\n"
        f"  enterprise              GitHub enterprise slug\n"
        f"  team_name               Copilot team the user belongs to\n"
        f"  login                   GitHub username\n"
        f"  name                    User display name (from SCIM / GitHub profile)\n"
        f"  email                   User email (from SCIM / GitHub profile)\n\n"
        f"Seat & Billing\n"
        f"  copilot_assigned        Whether the user has a Copilot seat (yes/no)\n"
        f"  plan_type               Copilot plan (e.g. copilot_enterprise)\n"
        f"  last_activity_at        Timestamp of the user's last Copilot activity\n"
        f"  active_status           active = last activity within 30 days; otherwise inactive\n\n"
        f"Billing (calendar month – full month from day 1 to last day)\n"
        f"  billing_period                  The billing month queried, e.g. '2026-03' for March 2026.\n"
        f"                                  Defaults to the current calendar month.\n"
        f"                                  Override with REPORT_YEAR + REPORT_MONTH env vars.\n"
        f"  premium_requests_complete_month Total premium (non-base-model) requests used in the full\n"
        f"                                  calendar month (grossQuantity from billing API).\n"
        f"                                  Source: GET /enterprises/{{ent}}/settings/billing/premium_request/usage.\n"
        f"                                  Empty when the billing API is unavailable.\n"
        f"  billed_amount_month             Amount actually charged for premium requests this month\n"
        f"                                  (netAmount from billing API, after included-request quota deducted).\n"
        f"                                  Matches 'Billed amount' in the GitHub billing UI.\n"
        f"                                  0.00 in the billing currency when usage is within the included-request quota.\n"
        f"                                  Empty when the billing API is unavailable.\n"
        f"  premium_requests_by_model_month Per-model breakdown of total premium requests consumed this month.\n"
        f"                                  Combines included and billed requests (grossQuantity per model).\n"
        f"                                  Format: 'claude-sonnet-4 - 10, gpt-5.1 - 3' (sorted by count).\n"
        f"                                  Empty when the billing API is unavailable or the response lacks model info.\n\n"
        f"Metrics (rolling 28-day window)\n"
        f"  metrics_interactions_28d        Number of prompts the user sent in Chat or Agent mode (e.g. Copilot Chat Ask/Edit/Agent/Plan panel). Does NOT include ghost-text inline completions.\n"
        f"  metrics_completions_28d         Number of inline ghost-text code suggestions that Copilot showed to the user in the IDE editor (code_completion feature). Does NOT include Chat/Agent prompts.\n"
        f"  metrics_acceptances_28d         Number of inline ghost-text suggestions the user accepted (e.g. pressed Tab). Does NOT include Chat/Agent interactions.\n"
        f"  metrics_acceptance_pct_28d      Acceptance rate: (acceptances / completions) × 100 %\n"
        f"  metrics_days_active_28d         Distinct calendar days with at least one Copilot interaction\n"
        f"  metrics_loc_suggested_28d       LOC Copilot proposed (= inline + chat + edit + agent; agent_edit contributes 0 — direct file writes have no suggestion UI)\n"
        f"  metrics_loc_added_28d           LOC applied from Copilot (= inline + chat + edit + agent + agent_edit; agent_edit = direct file writes)\n"
        f"  metrics_loc_deleted_28d         LOC deleted in Copilot-assisted edits\n"
        f"  metrics_loc_suggested_inline_28d      LOC proposed for inline completions (ghost-text; code_completion feature only)\n"
        f"  metrics_loc_added_inline_28d          LOC applied from inline completions\n"
        f"  metrics_loc_acceptance_pct_inline_28d Inline acceptance rate: (added/suggested)×100 (ghost-text only)\n"
        f"  metrics_loc_suggested_chat_28d        LOC proposed for Chat (Ask mode, inline chat)\n"
        f"  metrics_loc_added_chat_28d            LOC applied from Chat suggestions\n"
        f"  metrics_loc_suggested_edit_28d        LOC that Copilot proposed in Edit mode (loc_suggested_to_add_sum\n"
        f"                                        from chat_panel_edit_mode); represents code blocks shown in the\n"
        f"                                        edit-mode chat panel before the user applied them\n"
        f"  metrics_loc_added_edit_28d            LOC the user applied from Edit mode (code blocks inserted/copied\n"
        f"                                        from the edit-mode chat panel; loc_added_sum)\n"
        f"  metrics_loc_suggested_agent_28d       LOC proposed by Agent/Plan mode. All agent features use\n"
        f"                                        loc_suggested_to_add_sum directly from the API.\n"
        f"                                        agent_edit (direct file writes, which bypass the suggestion\n"
        f"                                        UI) contributes 0 because the API returns 0 for\n"
        f"                                        loc_suggested_to_add_sum for direct file writes.\n"
        f"  metrics_loc_added_agent_28d           LOC applied in Agent/Plan mode (chat panel code blocks applied +\n"
        f"                                        all file writes via agent_edit; loc_added_sum)\n"
        f"  metrics_top_model_28d           Most frequently used AI model by interaction count (e.g. gpt-4o).\n"
        f"                                  Note: This differs from premium_requests_by_model_month which\n"
        f"                                  reflects billing-weighted premium request consumption.\n"
        f"  metrics_top_language_28d        Programming language with highest Copilot activity\n"
        f"  metrics_top_feature_28d         Copilot feature used most often (e.g. Inline Chat, Agent, Plan, Ask, Edit)\n"
        f"  metrics_loc_suggested_by_language_total_28d  LOC proposed per language: inline+chat+edit+agent, sorted by volume descending\n"
        f"                                               (agent_edit contributes 0 to loc_suggested — direct file writes have no suggestion UI)\n"
        f"  metrics_loc_added_by_language_total_28d      LOC applied per language: inline+chat+edit+agent+agent_edit, sorted by volume descending\n"
        f"  metrics_loc_suggested_by_language_inline_28d LOC proposed per language: inline (code_completion) only\n"
        f"  metrics_loc_added_by_language_inline_28d     LOC applied per language: inline (code_completion) only\n"
        f"  metrics_loc_suggested_by_language_agent_28d  LOC proposed per language: agent (chat_panel_agent_mode+chat_panel_plan_mode) only\n"
        f"                                               (agent_edit is excluded — direct file writes always return 0 for loc_suggested_to_add_sum)\n"
        f"  metrics_loc_added_by_language_agent_28d      LOC applied per language: agent (chat_panel_agent_mode+chat_panel_plan_mode+agent_edit) only\n"
        f"\n"
        f"─────────────────────────────────────────\n"
        f"WHY loc_suggested CAN BE LESS THAN loc_added\n"
        f"─────────────────────────────────────────\n"
        f"For agent_edit (direct file writes) the GitHub API returns 0 for\n"
        f"loc_suggested_to_add_sum because Copilot writes changes straight into files,\n"
        f"bypassing the suggestion UI.  The report faithfully records 0 for that\n"
        f"component — no proxy is applied.  Because agent_edit lines still count\n"
        f"toward loc_added but contribute 0 to loc_suggested, heavy agent_edit use\n"
        f"will make loc_added exceed loc_suggested.\n"
        f"This is expected and not a data error.\n"
        f"\n"
        f"─────────────────────────────────────────\n"
        f"WHY metrics_loc_*_agent_28d MAY DIFFER FROM metrics_loc_*_by_language_agent_28d\n"
        f"─────────────────────────────────────────\n"
        f"The total agent LOC (metrics_loc_suggested_agent_28d, metrics_loc_added_agent_28d)\n"
        f"and the per-language agent LOC breakdown (metrics_loc_*_by_language_agent_28d) come\n"
        f"from different data sources in the GitHub API:\n"
        f"\n"
        f"  • metrics_loc_suggested_agent_28d / metrics_loc_added_agent_28d:\n"
        f"      Calculated from 'totals_by_feature' — aggregated LOC per feature.\n"
        f"\n"
        f"  • metrics_loc_suggested_by_language_agent_28d / metrics_loc_added_by_language_agent_28d:\n"
        f"      Calculated from 'totals_by_language_feature' — LOC broken down by language AND feature.\n"
        f"\n"
        f"These two API arrays can contain different totals because:\n"
        f"  1. Some API rows may not have language data — the language breakdown may not include\n"
        f"     all LOC if certain interactions were not tagged with a language.\n"
        f"  2. Data granularity differences — feature totals are aggregated differently than\n"
        f"     language-feature breakdowns.\n"
        f"  3. agent_edit (direct file writes) may have incomplete language tracking.\n"
        f"\n"
        f"Example: metrics_loc_suggested_agent_28d = 724 while the sum of\n"
        f"metrics_loc_suggested_by_language_agent_28d values = 634.\n"
        f"This discrepancy is expected and not a data error.\n"
    )

    missing = [
        name
        for name, val in [
            ("SMTP_SERVER", SMTP_SERVER),
            ("SMTP_USERNAME", SMTP_USERNAME),
            ("SMTP_PASSWORD", SMTP_PASSWORD),
            ("SENDER_EMAIL", SENDER_EMAIL),
        ]
        if not val
    ]
    if missing:
        print(
            f"  [WARN] Email skipped for team '{team_name}': "
            f"missing SMTP setting(s): {', '.join(missing)}"
        )
        return

    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(csv_path, "rb") as fh:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(fh.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{os.path.basename(csv_path)}"',
    )
    msg.attach(part)

    try:
        import ssl
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        print(f"  -> Email sent to {', '.join(recipients)}")
    except (smtplib.SMTPException, OSError) as exc:
        print(f"  [ERROR] Failed to send email to '{', '.join(recipients)}' for team '{team_name}': {exc}")


# -------------------------
# Main
# -------------------------
def main():
    print(f"Enterprise: {ENTERPRISE_SLUG}")
    print(f"API_BASE: {API_BASE}")
    print(f"Derived login suffix token: {derive_suffix_token()} (override with LOGIN_SUFFIX env if needed)")
    if ENTERPRISE_TEAM_SLUG_GROUPS:
        group_strs = ["|".join(g) for g in ENTERPRISE_TEAM_SLUG_GROUPS]
        print(f"Filtering to {len(ENTERPRISE_TEAM_SLUG_GROUPS)} group(s) "
              f"({len(ENTERPRISE_TEAM_SLUGS)} team(s)): {', '.join(group_strs)}")
        print(f"Each group will be written to its own CSV report.")
    else:
        print(f"Output: {OUTPUT_CSV}")

    # 1) SCIM index (name/email) — only available for EMU enterprises
    print("Fetching SCIM users...")
    scim_users = fetch_all_scim_users()
    scim_index = build_scim_index(scim_users)
    scim_available = bool(scim_users)
    print(f"SCIM users fetched: {len(scim_users)}; SCIM index keys: {len(scim_index)}")
    if not scim_available:
        print("[INFO] SCIM not available – will fall back to GitHub users API for display names and public emails.")

    # 2) Copilot seats (billing)
    print("Fetching Copilot billing seats...")
    seats_by_login = fetch_copilot_billing_seats_by_login()
    print(f"Copilot seats indexed by login: {len(seats_by_login)}")

    # 2b) Calendar-month premium request counts from the billing API.
    #     Queries the current calendar month by default.  Override with REPORT_YEAR
    #     and REPORT_MONTH env vars to target a specific billing period.
    billing_period_str = f"{REPORT_YEAR}-{REPORT_MONTH:02d}"
    print(
        f"Fetching calendar-month premium request billing data "
        f"for period {billing_period_str} …"
    )
    billing_premium_by_login, billing_amount_by_login, billing_model_requests_by_login = fetch_monthly_premium_requests_by_login(
        list(seats_by_login.keys()), REPORT_YEAR, REPORT_MONTH
    )
    billing_available = bool(billing_premium_by_login) or bool(billing_amount_by_login)
    if not billing_available:
        print(
            f"  [INFO] Billing API data unavailable for {billing_period_str}; "
            f"premium_requests_complete_month and billed_amount_month columns will be empty."
        )

    # 3) Metrics report and aggregate across all users
    print("Downloading Copilot metrics report (users-28-day/latest) ...")
    report_rows = download_latest_users_28_day_report_rows()
    print(f"Detected {len(report_rows)} rows in metrics report payload")
    if DEBUG and report_rows:
        print("[DEBUG] first row keys:", sorted(report_rows[0].keys()))

    metrics_by_login = aggregate_users(report_rows)
    print(f"Aggregated metrics users: {len(metrics_by_login)}")

    # 4) Teams + memberships
    print("Fetching enterprise teams...")
    all_teams = fetch_enterprise_teams()
    print(f"Enterprise teams fetched: {len(all_teams)}")

    # Filter to only the requested team slugs when ENTERPRISE_TEAM_SLUGS is set.
    if ENTERPRISE_TEAM_SLUGS:
        def _normalize(s: str) -> str:
            """Lowercase and replace non-alphanumeric runs with hyphens (slug normalization)."""
            return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

        def _slug_local(s: str) -> str:
            """Strip the enterprise namespace prefix (e.g. 'ent:test' -> 'test')."""
            return s.split(":", 1)[-1] if ":" in s else s

        # Always list available teams so users can verify / copy the correct slugs.
        print("[INFO] Available enterprise teams (use the slug in ENTERPRISE_TEAM_SLUGS):")
        for t in all_teams:
            t_slug = (t.get("slug") or t.get("team_slug") or "").strip()
            t_name = (t.get("name") or t.get("display_name") or t_slug).strip()
            print(f"  slug={t_slug!r}  local={_slug_local(t_slug)!r}  name={t_name!r}")

        requested_lower = {s.lower() for s in ENTERPRISE_TEAM_SLUGS}
        requested_normalized = {_normalize(s) for s in ENTERPRISE_TEAM_SLUGS}

        def _team_slug_key(t):
            return (t.get("slug") or t.get("team_slug") or "").strip().lower()

        def _team_name_key(t):
            return (t.get("name") or t.get("display_name") or "").strip().lower()

        def _team_matches(t):
            slug = _team_slug_key(t)
            slug_local = _slug_local(slug)
            name = _team_name_key(t)
            return (
                slug in requested_lower
                or slug_local in requested_lower
                or name in requested_lower
                or _normalize(slug) in requested_normalized
                or _normalize(slug_local) in requested_normalized
            )

        teams = [t for t in all_teams if _team_matches(t)]
        found_keys: set[str] = set()
        for t in teams:
            slug = _team_slug_key(t)
            found_keys.add(slug)
            found_keys.add(_slug_local(slug))
            found_keys.add(_team_name_key(t))
            found_keys.add(_normalize(slug))
            found_keys.add(_normalize(_slug_local(slug)))
        missing = [
            s for s in ENTERPRISE_TEAM_SLUGS
            if s.lower() not in found_keys and _normalize(s) not in found_keys
        ]
        if missing:
            available_list = ", ".join(
                f"{(t.get('slug') or t.get('team_slug') or '').strip()!r}" for t in all_teams
            )
            print(
                f"[WARN] The following requested team slugs/names were not found: {', '.join(missing)}. "
                f"Available slugs: {available_list}"
            )
        if not teams:
            raise SystemExit(
                f"[ERROR] None of the {len(ENTERPRISE_TEAM_SLUGS)} requested team(s) were found "
                f"in the enterprise. Requested: {', '.join(ENTERPRISE_TEAM_SLUGS)}. "
                f"See the '[INFO] Available enterprise teams' listing above for correct slug values. "
                f"Update the ENTERPRISE_TEAM_SLUGS secret or leave it empty to process all teams."
            )
        print(f"Teams to process: {len(teams)}")
    else:
        teams = all_teams

    fieldnames = [
        # identity / team
        "enterprise",
        "team_name",
        "login",
        "name",
        "email",
        # seat (billing)
        "copilot_assigned",
        "plan_type",
        "last_activity_at",
        "active_status",
        # metrics (28d rolling window)
        "metrics_interactions_28d",
        "metrics_completions_28d",
        "metrics_acceptances_28d",
        "metrics_acceptance_pct_28d",
        "metrics_days_active_28d",
        "metrics_loc_suggested_28d",
        "metrics_loc_added_28d",
        "metrics_loc_deleted_28d",
        "metrics_loc_suggested_inline_28d",
        "metrics_loc_added_inline_28d",
        "metrics_loc_acceptance_pct_inline_28d",
        "metrics_loc_suggested_chat_28d",
        "metrics_loc_added_chat_28d",
        "metrics_loc_suggested_edit_28d",
        "metrics_loc_added_edit_28d",
        "metrics_loc_suggested_agent_28d",
        "metrics_loc_added_agent_28d",
        # billing (calendar month)
        "billing_period",
        "premium_requests_complete_month",
        "billed_amount_month",
        "premium_requests_by_model_month",
        "metrics_top_model_28d",
        "metrics_top_language_28d",
        "metrics_top_feature_28d",
        "metrics_loc_suggested_by_language_total_28d",
        "metrics_loc_added_by_language_total_28d",
        "metrics_loc_suggested_by_language_inline_28d",
        "metrics_loc_added_by_language_inline_28d",
        "metrics_loc_suggested_by_language_agent_28d",
        "metrics_loc_added_by_language_agent_28d",
    ]

    # 5) Build output rows per team.
    # When ENTERPRISE_TEAM_SLUG_GROUPS is set, teams are grouped and each group gets its own CSV.
    # Otherwise all teams are combined into OUTPUT_CSV (original behaviour).
    date_str = datetime.now().strftime("%Y%m%d")
    total_rows = 0
    total_no_scim = 0
    total_no_email = 0

    # Accumulator used only in combined (non-filtered) mode.
    combined_rows: List[Dict[str, Any]] = []

    # Per-team results collected for group-based output (used when ENTERPRISE_TEAM_SLUG_GROUPS is set).
    per_team_results: Dict[str, Dict[str, Any]] = {}

    for i, t in enumerate(teams, start=1):
        team_name = (t.get("name") or t.get("display_name") or t.get("slug") or "").strip()
        team_slug = (t.get("slug") or t.get("team_slug") or "").strip()
        if not team_slug:
            continue

        print(f"[{i}/{len(teams)}] Fetching users for team: {team_name} ({team_slug})")
        memberships = fetch_enterprise_team_memberships(team_slug)

        team_rows: List[Dict[str, Any]] = []
        no_scim_match = 0
        no_email_count = 0

        for m in memberships:
            login = parse_membership_login(m)
            if not login:
                continue

            scim = scim_lookup(scim_index, login)
            seat = seats_by_login.get(login)

            if not scim:
                no_scim_match += 1
                # Prefer name/email already present in the Copilot billing seat's
                # assignee object (it is fetched in bulk and contains the user's
                # GitHub profile name and public email).  This avoids one extra
                # per-user API call for every seat holder.
                seat_assignee = seat.get("assignee") if seat else None
                seat_assignee = seat_assignee if isinstance(seat_assignee, dict) else {}
                seat_name = str(seat_assignee.get("name") or "").strip()
                seat_email = str(seat_assignee.get("email") or "").strip()

                if seat_name or seat_email:
                    scim = {"name": seat_name, "email": seat_email}
                else:
                    # No inline data available – fall back to GitHub users API.
                    # This covers non-EMU enterprises where the user has no seat,
                    # and EMU enterprises with an incomplete SCIM sync.
                    scim = fetch_github_user_info(login)

                # If the seat assignee had a name but no email, try the GitHub
                # users API to fill the gap.  Results are cached in _gh_user_cache,
                # so this call is free if we already fetched this user earlier.
                if scim and not scim.get("email"):
                    gh_info = fetch_github_user_info(login)
                    if gh_info.get("email"):
                        scim = {
                            "name": scim.get("name") or gh_info.get("name", ""),
                            "email": gh_info["email"],
                        }

            agg = metrics_by_login.get(login) or metrics_by_login.get(login.lower())

            user_name = (scim or {}).get("name", "")
            user_email = (scim or {}).get("email", "")
            if not user_email:
                no_email_count += 1

            base = {
                "enterprise": ENTERPRISE_SLUG,
                "team_name": team_name,
                "login": login,
                "name": user_name,
                "email": user_email,
                "copilot_assigned": "yes" if seat else "no",
                "plan_type": (seat or {}).get("plan_type", "") if seat else "",
                "last_activity_at": (seat or {}).get("last_activity_at", "") if seat else "",
                "active_status": is_active((seat or {}).get("last_activity_at")) if seat else "inactive",
                # Billing period: the calendar month covered by the premium-request columns.
                # Format: YYYY-MM (e.g. "2026-03" for March 2026).
                # Defaults to the current calendar month; override with REPORT_YEAR + REPORT_MONTH.
                "billing_period": billing_period_str if billing_available else "",
                # Complete-month premium requests placed in the metrics section for easy
                # comparison alongside other per-user metrics.  Source: billing API
                # (same value as the former billing_premium_requests_month column).
                # round(..., 2) preserves any fractional counts the API may return (the
                # GitHub billing UI shows values like 685.40 or 136.20 because the
                # included-request credit is sometimes non-integer).  Using int() would
                # truncate those fractions and under-report the true consumption.
                "premium_requests_complete_month": (
                    round(billing_premium_by_login[login], 2)
                    if login in billing_premium_by_login
                    else ("" if not billing_available else 0)
                ),
                # Billed amount (netAmount = actual charge after included-request quota).
                # Matches the "Billed amount" column in the GitHub billing UI.
                # Empty when the billing API is unavailable.
                "billed_amount_month": (
                    round(billing_amount_by_login[login], 4)
                    if login in billing_amount_by_login
                    else ("" if not billing_available else 0)
                ),
                # Per-model premium request breakdown for the billing month.
                # Combines included and billed requests (both from grossQuantity per model).
                # Format: "claude-sonnet-4 - 10, gpt-5.1 - 3" (sorted by count descending).
                # Empty when the billing API is unavailable or the response has no model info.
                "premium_requests_by_model_month": (
                    format_model_premium_requests(billing_model_requests_by_login.get(login, {}))
                    if billing_available
                    else ""
                ),
            }

            base.update(metrics_row_for_user(agg))
            team_rows.append(base)

        total_rows += len(team_rows)
        total_no_scim += no_scim_match
        total_no_email += no_email_count

        if ENTERPRISE_TEAM_SLUG_GROUPS:
            # Collect team results for later group-based output.
            per_team_results[team_slug] = {
                "name": team_name,
                "local_slug": _slug_local(team_slug),
                "rows": team_rows,
                "no_scim_match": no_scim_match,
                "no_email_count": no_email_count,
            }
        else:
            combined_rows.extend(team_rows)

    print(f"Total rows (team-user): {total_rows}")
    print(f"Users with no SCIM match: {total_no_scim}")
    print(f"Users with no email resolved: {total_no_email}")
    if total_no_email and not scim_available:
        print(
            "[INFO] Some emails are blank because this is a non-EMU enterprise and those "
            "users have not set a publicly-visible email on their GitHub profile. "
            "For complete email coverage, use an EMU (Enterprise Managed Users) enterprise "
            "or ask affected users to set a public email in their GitHub profile settings."
        )

    if ENTERPRISE_TEAM_SLUG_GROUPS:
        # Group-based output: one CSV per group.  Teams within a group (pipe-separated in
        # ENTERPRISE_TEAM_SLUGS) are merged into a single report with rows deduplicated by login.

        def _find_team_result(requested_slug: str) -> Optional[Dict[str, Any]]:
            """Find per_team_results entry matching the requested slug."""
            req_lower = requested_slug.lower()
            req_local = _slug_local(requested_slug)
            req_norm = _normalize(requested_slug)
            for actual_slug, data in per_team_results.items():
                actual_local = _slug_local(actual_slug)
                if (
                    actual_slug.lower() == req_lower
                    or actual_local.lower() == req_lower
                    or actual_local.lower() == req_local.lower()
                    or _normalize(actual_local) == req_norm
                ):
                    return data
            return None

        for group_idx, group_slugs in enumerate(ENTERPRISE_TEAM_SLUG_GROUPS, start=1):
            # Gather team data for each slug in this group.
            group_team_data: List[Tuple[str, Dict[str, Any]]] = []
            for req_slug in group_slugs:
                td = _find_team_result(req_slug)
                if td:
                    group_team_data.append((req_slug, td))

            if not group_team_data:
                print(f"[WARN] No team data found for group {group_idx}: {group_slugs} – skipping.")
                continue

            # Combine rows, deduplicating by login (first occurrence wins).
            seen_logins: Set[str] = set()
            group_rows: List[Dict[str, Any]] = []
            for _, td in group_team_data:
                for row in td["rows"]:
                    if row["login"] not in seen_logins:
                        seen_logins.add(row["login"])
                        group_rows.append(row)

            # Build CSV filename and display name.
            local_slugs = [td["local_slug"] for _, td in group_team_data]
            team_names_in_group = [td["name"] for _, td in group_team_data]
            if len(local_slugs) > 1:
                combined_slug_part = "_and_".join(local_slugs)
                group_display_name = " + ".join(team_names_in_group)
            else:
                combined_slug_part = local_slugs[0]
                group_display_name = team_names_in_group[0]

            group_csv = f"enterprise_team_{combined_slug_part}_copilot_{date_str}.csv"
            with open(group_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(group_rows)

            group_no_scim = sum(td["no_scim_match"] for _, td in group_team_data)
            group_no_email = sum(td["no_email_count"] for _, td in group_team_data)
            print(
                f"  -> {len(group_rows)} rows written to {group_csv} "
                f"(SCIM misses: {group_no_scim}, missing email: {group_no_email})"
            )

            # Collect email recipients from all teams in the group.
            # Slug-derived env vars take priority; TEAM{group_idx}_HEAD_EMAIL is the fallback.
            all_recipients: List[str] = []
            for req_slug, _ in group_team_data:
                slug_email = os.getenv(slug_to_env_name(req_slug), "").strip()
                if slug_email:
                    for addr in slug_email.split(","):
                        addr = addr.strip()
                        if addr and addr not in all_recipients:
                            all_recipients.append(addr)
                else:
                    fallback_email = os.getenv(f"TEAM{group_idx}_HEAD_EMAIL", "").strip()
                    for addr in fallback_email.split(","):
                        addr = addr.strip()
                        if addr and addr not in all_recipients:
                            all_recipients.append(addr)
            send_report_email(", ".join(all_recipients), group_csv, group_display_name, date_str)

    elif not ENTERPRISE_TEAM_SLUGS:
        # Original behaviour: single combined CSV.
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(combined_rows)
        print(f"CSV report generated: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
