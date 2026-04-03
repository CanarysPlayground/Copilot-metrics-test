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

# Features to exclude from inline completion acceptance rate calculation
# Edit and Agent features add code directly without traditional suggestions,
# so they shouldn't be included when calculating inline completion acceptance rate.
# Using a set for O(1) lookup performance.
EXCLUDED_FEATURES_FOR_INLINE_PCT = {"edit", "edit_mode", "agent"}

# -------------------------
# HTTP helpers
# -------------------------
def gh_get(url: str, headers: Dict[str, str], params=None, timeout=60) -> requests.Response:
    last = None
    for attempt in range(1, 7):
        resp = SESSION.get(url, headers=headers, params=params, timeout=timeout)
        last = resp

        if resp.status_code in (403, 429, 500, 502, 503, 504):
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
    idx = {}
    for u in scim_users:
        if not isinstance(u, dict):
            continue

        name = pick_scim_name(u)
        email = pick_scim_email(u)
        scim_user_name = str(u.get("userName") or "").strip()

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
            idx.setdefault(
                k,
                {"name": name, "email": email, "scim_userName": scim_user_name},
            )

    return idx

def scim_lookup(scim_index: Dict[str, Dict[str, str]], login: str) -> Dict[str, str]:
    if not login:
        return {}
    key = login.lower().strip()

    hit = scim_index.get(key)
    if hit:
        return hit

    base = key
    if "_" in key:
        base = key.split("_", 1)[0]
        hit = scim_index.get(base)
        if hit:
            return hit

    suffix = derive_suffix_token()
    if suffix:
        hit = scim_index.get(f"{base}_{suffix}")
        if hit:
            return hit

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
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Fetch premium request usage and billed amount for a specific calendar month per user.

    Calls GET /enterprises/{enterprise}/settings/billing/premium_request/usage
    with ``year``, ``month``, and ``user`` query parameters once per login.

    *year* and *month* must refer to a fully completed billing period.  Pass
    ``REPORT_YEAR`` and ``REPORT_MONTH`` (which default to the previous calendar
    month) so the returned counts cover the whole month from the 1st to the last day.

    Returns a 2-tuple:
      - ``premium_requests``: login → total ``grossQuantity`` consumed in *month*/*year*.
      - ``billed_amounts``:   login → total USD amount charged (``netAmount`` when present,
        falling back to ``grossAmount`` per usage item).

    Both dicts are empty when the endpoint is unavailable (e.g. the token does not have
    billing-manager scope, or the enterprise does not use the enhanced billing platform).

    Error-handling policy:
    - HTTP 403 or 501 → the whole endpoint is unavailable; abort and return ({}, {})
      so callers can fall back gracefully.
    - HTTP 400 or 404 for a specific user → that user has no billing record this
      month; record 0 for them and continue with the remaining users.
    - Other non-2xx responses → log a warning, skip that user, continue.
    """
    if not logins:
        return {}, {}

    url = f"{API_BASE}/enterprises/{ENTERPRISE_SLUG}/settings/billing/premium_request/usage"

    result: Dict[str, float] = {}
    billed: Dict[str, float] = {}
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
            # Log currency information so callers can verify the unit of the amounts.
            currency = data.get("currency") or data.get("currencyCode") or "N/A"
            print(
                f"  [INFO] Billing API time period: year={time_period.get('year', 'N/A')}, "
                f"month={time_period.get('month', 'N/A')} "
                f"(requested {year}-{month:02d}); currency={currency}"
            )
            period_logged = True

        usage_items = data.get("usageItems") or []
        total_qty = 0.0
        total_billed = 0.0
        for item in usage_items:
            if not isinstance(item, dict):
                continue
            total_qty += to_num(item.get("grossQuantity"))
            # Prefer netAmount (post-discount), fall back to grossAmount.
            amount = item.get("netAmount")
            if amount is None:
                amount = item.get("grossAmount")
            total_billed += to_num(amount)

        result[login] = total_qty
        billed[login] = total_billed

        if idx % 20 == 0:
            print(f"  … {idx}/{len(logins)} users processed")

    if not endpoint_available:
        return {}, {}

    print(f"  Billing API: premium request data fetched for {len(result)} user(s) ({year}-{month:02d}).")
    return result, billed


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
    r = requests.get(url, allow_redirects=True, timeout=180)
    if r.status_code in (401, 403):
        r = requests.get(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, allow_redirects=True, timeout=180)
    r.raise_for_status()
    return r.text

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
        row = json.loads(line)
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

        agg.interactions += to_num(r.get("user_initiated_interaction_count"))
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
                interaction_count = to_num(mf.get("user_initiated_interaction_count"))
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

        tbf = r.get("totals_by_feature")
        if isinstance(tbf, list):
            for f in tbf:
                if not isinstance(f, dict):
                    continue
                feat = normalize_feature_name(f.get("feature"))
                agg.feature_counts[feat] = agg.feature_counts.get(feat, 0.0) + to_num(
                    f.get("user_initiated_interaction_count")
                )

                # Store LoC per feature for refined acceptance percentage calculation
                # Nested format uses fixed field names (loc_suggested_to_add_sum, loc_added_sum, loc_deleted_sum)
                loc_suggested_val = to_num(f.get("loc_suggested_to_add_sum"))
                loc_added_val = to_num(f.get("loc_added_sum"))
                loc_deleted_val = to_num(f.get("loc_deleted_sum"))
                
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
            
            val = to_num(r.get("user_initiated_interaction_count")) or to_num(r.get("copilot_total_requests"))
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

    return users

def metrics_row_for_user(agg: Optional[UserAgg]) -> Dict[str, Any]:
    if not agg:
        return {
            "metrics_interactions_28d": "",
            "metrics_completions_28d": "",
            "metrics_acceptances_28d": "",
            "metrics_acceptance_pct_28d": "",
            "metrics_days_active_28d": "",
            "metrics_loc_suggested_28d": "",
            "metrics_loc_added_28d": "",
            "metrics_loc_deleted_28d": "",
            "metrics_loc_suggested_inline_28d": "",
            "metrics_loc_added_inline_28d": "",
            "metrics_loc_acceptance_pct_inline_28d": "",
            "metrics_top_model_28d": "",
            "metrics_top_language_28d": "",
            "metrics_top_feature_28d": "",
            "metrics_loc_suggested_by_language_28d": "",
            "metrics_loc_added_by_language_28d": "",
        }

    acceptance_pct = (agg.acceptances / agg.completions * 100.0) if agg.completions > 0 else 0.0
    
    # Calculate inline-only LoC acceptance percentage (excluding edit and agent features).
    # This measures the traditional acceptance rate: what percentage of suggested code was accepted.
    # Formula: (added / suggested) × 100
    # - Example: Copilot suggested 100 lines, developer accepted 80 lines → 80%
    # - Example: Copilot suggested 100 lines, developer accepted and expanded to 150 lines → 150%
    # Note: Values >100% indicate the developer accepted the suggestion and added more code on top.
    # 
    # Two filters are applied:
    # 1. Exclude edit/agent features (EXCLUDED_FEATURES_FOR_INLINE_PCT) - these features don't use
    #    ghost-text suggestions and should not be included in inline completion metrics
    # 2. Only include features where suggested > 0 - avoids division by zero and ensures we only
    #    measure features that actually showed suggestions
    inline_loc_suggested = 0.0
    inline_loc_added = 0.0
    
    for feat, suggested in agg.feature_loc_suggested.items():
        if feat not in EXCLUDED_FEATURES_FOR_INLINE_PCT and suggested > 0:
            inline_loc_suggested += suggested
            inline_loc_added += agg.feature_loc_added.get(feat, 0.0)
    
    # Calculate traditional acceptance rate: what % of suggested code was accepted/added
    loc_acceptance_pct_inline = (inline_loc_added / inline_loc_suggested * 100.0) if inline_loc_suggested > 0 else 0.0

    # NOTE: The output includes both total LOC metrics and inline-only LOC metrics:
    # - metrics_loc_*_28d: Total across ALL features (includes edit, agent, inline)
    # - metrics_loc_*_inline_28d: Only inline completions (excludes edit, agent)
    # - metrics_loc_acceptance_pct_inline_28d: Calculated using inline-only values
    #
    # For accurate acceptance percentage, use: metrics_loc_added_inline_28d / metrics_loc_suggested_inline_28d
    # Do NOT calculate as: metrics_loc_added_28d / metrics_loc_suggested_28d (will be incorrect)
    return {
        "metrics_interactions_28d": int(agg.interactions),
        "metrics_completions_28d": int(agg.completions),
        "metrics_acceptances_28d": int(agg.acceptances),
        "metrics_acceptance_pct_28d": round(acceptance_pct, 2),
        "metrics_days_active_28d": len(agg.days),
        "metrics_loc_suggested_28d": int(agg.loc_suggested),
        "metrics_loc_added_28d": int(agg.loc_added),
        "metrics_loc_deleted_28d": int(agg.loc_deleted),
        "metrics_loc_suggested_inline_28d": int(inline_loc_suggested),
        "metrics_loc_added_inline_28d": int(inline_loc_added),
        "metrics_loc_acceptance_pct_inline_28d": round(loc_acceptance_pct_inline, 2),
        "metrics_top_model_28d": top_key(agg.model_counts),
        "metrics_top_language_28d": top_key(agg.language_counts),
        "metrics_top_feature_28d": format_feature_name(top_key(agg.feature_counts)),
        "metrics_loc_suggested_by_language_28d": format_language_loc(agg.language_loc_suggested),
        "metrics_loc_added_by_language_28d": format_language_loc(agg.language_loc_added),
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
        f"  billing_premium_requests_month  Total premium (non-base-model) requests billed for the\n"
        f"                                  full calendar month.  Source: GitHub billing API\n"
        f"                                  (GET /enterprises/{{ent}}/settings/billing/premium_request/usage).\n"
        f"                                  Empty when the billing API is unavailable.\n"
        f"  billing_billed_amount_month     USD amount charged for premium requests this month\n"
        f"                                  (netAmount when available, otherwise grossAmount).\n"
        f"                                  Empty when the billing API is unavailable.\n\n"
        f"Metrics (rolling 28-day window)\n"
        f"  interactions_28d        Total user-initiated prompts across all Copilot features\n"
        f"  completions_28d         Number of times Copilot generated code for the user\n"
        f"  acceptances_28d         Number of times the user accepted a Copilot suggestion\n"
        f"  acceptance_pct_28d      Acceptance rate: (acceptances / completions) × 100 %\n"
        f"  days_active_28d         Distinct calendar days with at least one Copilot interaction\n"
        f"  loc_suggested_28d       Lines of Code (LOC) that Copilot proposed (mainly inline completions)\n"
        f"  loc_added_28d           LOC actually applied from Copilot (all features: completions + Chat/Edit/Agent)\n"
        f"  loc_deleted_28d         LOC deleted in Copilot-assisted edits\n"
        f"  loc_acceptance_pct_inline_28d  Inline acceptance rate: (added/suggested)×100, excludes edit/agent\n"
        f"  premium_requests_complete_month  Total premium (non-base-model) requests for the complete\n"
        f"                                   calendar month. Source: GitHub billing API\n"
        f"                                   (same value as billing_premium_requests_month).\n"
        f"                                   Empty when the billing API is unavailable.\n"
        f"  top_model_28d           AI model used most often (e.g. gpt-4o)\n"
        f"  top_language_28d        Programming language with highest Copilot activity\n"
        f"  top_feature_28d         Copilot feature used most often (e.g. Inline Chat, Agent, Ask, Edit)\n\n"
        f"─────────────────────────────────────────\n"
        f"WHY loc_suggested CAN BE LESS THAN loc_added\n"
        f"─────────────────────────────────────────\n"
        f"loc_suggested counts lines proposed in inline completion ghost-text suggestions.\n"
        f"loc_added counts ALL lines applied from Copilot across every feature, including\n"
        f"Copilot Chat (Ask), Edit, and Agent — where code is applied directly without a\n"
        f"traditional ghost-text suggestion. Heavy use of Chat/Edit/Agent therefore causes\n"
        f"loc_added to exceed loc_suggested, which is expected and not a data error.\n"
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
    billing_premium_by_login, billing_amount_by_login = fetch_monthly_premium_requests_by_login(
        list(seats_by_login.keys()), REPORT_YEAR, REPORT_MONTH
    )
    billing_available = bool(billing_premium_by_login) or bool(billing_amount_by_login)
    if not billing_available:
        print(
            f"  [INFO] Billing API data unavailable for {billing_period_str}; "
            f"billing_premium_requests_month and billing_billed_amount_month columns will be empty."
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
        "premium_requests_complete_month",
        "billed_amount_month",
        "metrics_top_model_28d",
        "metrics_top_language_28d",
        "metrics_top_feature_28d",
        "metrics_loc_suggested_by_language_28d",
        "metrics_loc_added_by_language_28d",
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
                # Complete-month premium requests placed in the metrics section for easy
                # comparison alongside other per-user metrics.  Source: billing API
                # (same value as the former billing_premium_requests_month column).
                "premium_requests_complete_month": (
                    int(billing_premium_by_login[login])
                    if login in billing_premium_by_login
                    else ("" if not billing_available else 0)
                ),
                # Billed amount (netAmount/grossAmount) for the calendar month from
                # the premium-request billing API.  Empty when the API is unavailable.
                "billed_amount_month": (
                    round(billing_amount_by_login[login], 4)
                    if login in billing_amount_by_login
                    else ("" if not billing_available else 0)
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
