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
from typing import Any, Dict, List, Optional, Set

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
_raw_team_slugs = os.getenv("ENTERPRISE_TEAM_SLUGS", "").strip()
ENTERPRISE_TEAM_SLUGS: List[str] = [s.strip() for s in _raw_team_slugs.split(",") if s.strip()] if _raw_team_slugs else []

# Optional override if your suffix is not derived correctly from enterprise slug
LOGIN_SUFFIX = (os.getenv("LOGIN_SUFFIX") or "").strip().lower()

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
        report_url = choose_report_url(urls)
        print(f"[REPORT] downloading report from: {report_url}")

        text = download_report_as_text(report_url)
        if DEBUG:
            dump_text(text[:20000], "report_head")

        rows = parse_report_payload(text)
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

    model_counts: Dict[str, float] = field(default_factory=dict)
    language_counts: Dict[str, float] = field(default_factory=dict)
    feature_counts: Dict[str, float] = field(default_factory=dict)

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

        day = r.get("day")
        if isinstance(day, str) and day:
            agg.days.add(day)

        tmm = r.get("totals_by_model_feature")
        if isinstance(tmm, list):
            for mf in tmm:
                if not isinstance(mf, dict):
                    continue
                model = mf.get("model") or "unknown"
                agg.model_counts[model] = agg.model_counts.get(model, 0.0) + to_num(
                    mf.get("user_initiated_interaction_count")
                )

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

        tbf = r.get("totals_by_feature")
        if isinstance(tbf, list):
            for f in tbf:
                if not isinstance(f, dict):
                    continue
                feat = f.get("feature") or "unknown"
                agg.feature_counts[feat] = agg.feature_counts.get(feat, 0.0) + to_num(
                    f.get("user_initiated_interaction_count")
                )

                agg.loc_suggested += to_num(f.get("loc_suggested_to_add_sum"))
                agg.loc_added += to_num(f.get("loc_added_sum"))
                agg.loc_deleted += to_num(f.get("loc_deleted_sum"))

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
            "metrics_top_model_28d": "",
            "metrics_top_language_28d": "",
            "metrics_top_feature_28d": "",
        }

    acceptance_pct = (agg.acceptances / agg.completions * 100.0) if agg.completions > 0 else 0.0

    return {
        "metrics_interactions_28d": int(agg.interactions),
        "metrics_completions_28d": int(agg.completions),
        "metrics_acceptances_28d": int(agg.acceptances),
        "metrics_acceptance_pct_28d": round(acceptance_pct, 2),
        "metrics_days_active_28d": len(agg.days),
        "metrics_loc_suggested_28d": int(agg.loc_suggested),
        "metrics_loc_added_28d": int(agg.loc_added),
        "metrics_loc_deleted_28d": int(agg.loc_deleted),
        "metrics_top_model_28d": top_key(agg.model_counts),
        "metrics_top_language_28d": top_key(agg.language_counts),
        "metrics_top_feature_28d": format_feature_name(top_key(agg.feature_counts)),
    }

# -------------------------
# Email helpers
# -------------------------
def get_team_head_email(team_index: int) -> str:
    """Return the head email for the 1-based team index.

    Reads ``TEAM{team_index}_HEAD_EMAIL`` from the environment
    (e.g. ``TEAM1_HEAD_EMAIL``, ``TEAM2_HEAD_EMAIL``, …).
    The index is not capped – any positive integer is valid as long as the
    corresponding secret is configured.
    Returns an empty string when the variable is not set.
    """
    return os.getenv(f"TEAM{team_index}_HEAD_EMAIL", "").strip()


def send_report_email(to_addr: str, csv_path: str, team_name: str, date_str: str) -> None:
    """Send the team CSV report as an email attachment.

    *to_addr* may contain a single address or multiple comma-separated
    addresses (e.g. ``"alice@example.com, bob@example.com"``).  The report is
    delivered to every address in the list.

    Silently skips when any required SMTP setting is missing or *to_addr* is
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

    subject = f"Copilot Metrics Report – {team_name} ({date_str})"
    body = (
        f"Hi,\n\n"
        f"Please find attached the Copilot metrics report for team '{team_name}' "
        f"generated on {date_str}.\n\n"
        f"This report is auto-generated and sent daily.\n"
    )

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
    if ENTERPRISE_TEAM_SLUGS:
        print(f"Filtering to {len(ENTERPRISE_TEAM_SLUGS)} team(s): {', '.join(ENTERPRISE_TEAM_SLUGS)}")
        print(f"Each team will be written to its own CSV report.")
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
        # metrics (28d)
        "metrics_interactions_28d",
        "metrics_completions_28d",
        "metrics_acceptances_28d",
        "metrics_acceptance_pct_28d",
        "metrics_days_active_28d",
        "metrics_loc_suggested_28d",
        "metrics_loc_added_28d",
        "metrics_loc_deleted_28d",
        "metrics_top_model_28d",
        "metrics_top_language_28d",
        "metrics_top_feature_28d",
    ]

    # 5) Build output rows per team.
    # When ENTERPRISE_TEAM_SLUGS is set, each team gets its own CSV file.
    # Otherwise all teams are combined into OUTPUT_CSV (original behaviour).
    date_str = datetime.now().strftime("%Y%m%d")
    total_rows = 0
    total_no_scim = 0
    total_no_email = 0

    # Accumulator used only in combined (non-filtered) mode.
    combined_rows: List[Dict[str, Any]] = []

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
            }

            base.update(metrics_row_for_user(agg))
            team_rows.append(base)

        total_rows += len(team_rows)
        total_no_scim += no_scim_match
        total_no_email += no_email_count

        if ENTERPRISE_TEAM_SLUGS:
            # Write a separate CSV report for this team.
            # Strip the enterprise namespace prefix (e.g. "ent:admin" -> "admin") so the
            # filename is "enterprise_team_admin_copilot_<date>.csv" rather than
            # "enterprise_team_ent-admin_copilot_<date>.csv".
            team_csv = f"enterprise_team_{_slug_local(team_slug)}_copilot_{date_str}.csv"
            with open(team_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(team_rows)
            print(f"  -> {len(team_rows)} rows written to {team_csv} "
                  f"(SCIM misses: {no_scim_match}, missing email: {no_email_count})")
            # Email the report to the team head (TEAM{i}_HEAD_EMAIL).
            recipient = get_team_head_email(i)
            send_report_email(recipient, team_csv, team_name, date_str)
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

    if not ENTERPRISE_TEAM_SLUGS:
        # Original behaviour: single combined CSV.
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(combined_rows)
        print(f"CSV report generated: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
