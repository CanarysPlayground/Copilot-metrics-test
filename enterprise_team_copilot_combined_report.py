import os
import csv
import io
import smtplib
import ssl
import time
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
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

# Optional override if your suffix is not derived correctly from enterprise slug
LOGIN_SUFFIX = (os.getenv("LOGIN_SUFFIX") or "").strip().lower()

# Debug for metrics report parsing
DEBUG = os.getenv("DEBUG_JSON", "0") == "1"
DEBUG_PREFIX = os.getenv("DEBUG_FILE_PREFIX", "copilot_metrics_debug")

# -------------------------
# Email / SMTP config
# -------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")

# Enterprise teams and their head email environment variable names.
# Each team head receives only their own team's daily Copilot report.
ENTERPRISE_TEAM_EMAIL_VARS: Dict[str, str] = {
    "accelerator-copilot": "ACCELERATOR_COPILOT_HEAD_EMAIL",
    "delivery-copilot":    "DELIVERY_COPILOT_HEAD_EMAIL",
    "genesis-copilot":     "GENESIS_COPILOT_HEAD_EMAIL",
    "nt-copilot":          "NT_COPILOT_HEAD_EMAIL",
    "pdg-copilot":         "PDG_COPILOT_HEAD_EMAIL",
}

# TEAM_EMAILS: JSON mapping of team slug (or team name) to recipient email.
# Example: {"accelerator-copilot": "head@example.com", "delivery-copilot": "head2@example.com"}
# If not provided, individual per-team env vars above are used instead.
TEAM_EMAILS_RAW = os.getenv("TEAM_EMAILS", "")

def _parse_team_emails(raw: str) -> Dict[str, str]:
    """Parse the TEAM_EMAILS JSON string into a dict mapping team key -> email."""
    if not raw.strip():
        return {}
    try:
        mapping = json.loads(raw)
        if isinstance(mapping, dict):
            return {str(k).strip().lower(): str(v).strip() for k, v in mapping.items() if v}
    except json.JSONDecodeError as exc:
        print(f"[WARN] Could not parse TEAM_EMAILS JSON: {exc}")
    return {}

def _build_team_emails() -> Dict[str, str]:
    """Build team -> email mapping from JSON env var or individual per-team env vars."""
    mapping = _parse_team_emails(TEAM_EMAILS_RAW)
    # Merge individual per-team env vars (they take precedence over TEAM_EMAILS JSON)
    for team_slug, env_var in ENTERPRISE_TEAM_EMAIL_VARS.items():
        email = os.getenv(env_var, "").strip()
        if email:
            mapping[team_slug] = email
    return mapping

TEAM_EMAILS: Dict[str, str] = _build_team_emails()
EMAIL_ENABLED = bool(TEAM_EMAILS and SMTP_HOST and SENDER_EMAIL)

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
    url = f"{API_BASE}/scim/v2/enterprises/{ENTERPRISE_SLUG}/Users"

    start_index = 1
    count = 100
    users = []

    while True:
        resp = gh_get(url, headers=HEADERS_SCIM, params={"startIndex": start_index, "count": count})
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
    latest_payload = get_json_from_api(latest_url)
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
def build_team_csv_content(rows: List[Dict[str, Any]], fieldnames: List[str]) -> str:
    """Write *rows* into an in-memory CSV string."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def resolve_team_email(team_name: str, team_slug: str) -> Optional[str]:
    """Look up the recipient email for a team using slug or name (case-insensitive)."""
    slug_lower = team_slug.lower()
    name_lower = team_name.lower()
    return TEAM_EMAILS.get(slug_lower) or TEAM_EMAILS.get(name_lower)


def send_team_report_email(
    recipient: str,
    team_name: str,
    csv_content: str,
    date_str: str,
) -> None:
    """Send an email with the team CSV report as an attachment."""
    msg = EmailMessage()
    msg["Subject"] = f"Copilot Metrics Report – {team_name} – {date_str}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = recipient

    msg.set_content(
        f"Hello,\n\n"
        f"Please find attached the daily GitHub Copilot usage report "
        f"for team '{team_name}' (enterprise: {ENTERPRISE_SLUG}).\n\n"
        f"Report date: {date_str}\n\n"
        f"Regards,\n"
        f"Copilot Metrics Automation"
    )

    filename = f"copilot_report_{team_name}_{date_str}.csv"
    msg.add_attachment(
        csv_content.encode("utf-8"),
        maintype="text",
        subtype="csv",
        filename=filename,
    )

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)

    print(f"[EMAIL] Report sent to {recipient} for team '{team_name}'")


# -------------------------
# Main
# -------------------------
def main():
    print(f"Enterprise: {ENTERPRISE_SLUG}")
    print(f"API_BASE: {API_BASE}")
    print(f"Derived login suffix token: {derive_suffix_token()} (override with LOGIN_SUFFIX env if needed)")
    print(f"Output: {OUTPUT_CSV}")

    # 1) SCIM index (name/email)
    print("Fetching SCIM users...")
    scim_users = fetch_all_scim_users()
    scim_index = build_scim_index(scim_users)
    print(f"SCIM users fetched: {len(scim_users)}; SCIM index keys: {len(scim_index)}")

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
    teams = fetch_enterprise_teams()
    print(f"Enterprise teams fetched: {len(teams)}")

    # 5) Build output rows
    rows_out: List[Dict[str, Any]] = []
    no_scim_match = 0

    for i, t in enumerate(teams, start=1):
        team_name = (t.get("name") or t.get("display_name") or t.get("slug") or "").strip()
        team_slug = (t.get("slug") or t.get("team_slug") or "").strip()
        if not team_slug:
            continue

        print(f"[{i}/{len(teams)}] Fetching users for team: {team_name} ({team_slug})")
        memberships = fetch_enterprise_team_memberships(team_slug)

        for m in memberships:
            login = parse_membership_login(m)
            if not login:
                continue

            scim = scim_lookup(scim_index, login)
            if not scim:
                no_scim_match += 1

            seat = seats_by_login.get(login)
            agg = metrics_by_login.get(login) or metrics_by_login.get(login.lower())

            base = {
                "enterprise": ENTERPRISE_SLUG,
                "team_name": team_name,
                "team_slug": team_slug,
                "login": login,
                "name": (scim or {}).get("name", ""),
                "email": (scim or {}).get("email", ""),
                "copilot_assigned": "yes" if seat else "no",
                "plan_type": (seat or {}).get("plan_type", "") if seat else "",
                "last_activity_at": (seat or {}).get("last_activity_at", "") if seat else "",
                "active_status": is_active((seat or {}).get("last_activity_at")) if seat else "inactive",
            }

            base.update(metrics_row_for_user(agg))
            rows_out.append(base)

    print(f"Total rows (team-user): {len(rows_out)}")
    print(f"Users with no SCIM match (email/name blank): {no_scim_match}")

    fieldnames = [
        # identity / team
        "enterprise",
        "team_name",
        "team_slug",
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

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)

    print(f"CSV report generated: {OUTPUT_CSV}")

    # 6) Email per-team reports to team heads
    if EMAIL_ENABLED:
        date_str = datetime.now().strftime("%Y-%m-%d")
        # Group rows by team
        rows_by_team: Dict[str, List[Dict[str, Any]]] = {}
        team_name_for_slug: Dict[str, str] = {}
        for row in rows_out:
            t_name = row.get("team_name", "")
            # Use the authoritative team slug from the API; fall back to deriving from name
            t_slug = row.get("team_slug", "").lower() or re.sub(r"[^a-z0-9]+", "-", t_name.lower()).strip("-")
            rows_by_team.setdefault(t_slug, []).append(row)
            team_name_for_slug[t_slug] = t_name

        sent = 0
        for t_slug, t_rows in rows_by_team.items():
            t_name = team_name_for_slug.get(t_slug, t_slug)
            recipient = resolve_team_email(t_name, t_slug)
            if not recipient:
                print(f"[EMAIL] No recipient configured for team '{t_name}' (slug: {t_slug}), skipping.")
                continue
            csv_content = build_team_csv_content(t_rows, fieldnames)
            try:
                send_team_report_email(recipient, t_name, csv_content, date_str)
                sent += 1
            except Exception as exc:
                print(f"[EMAIL] Failed to send report for team '{t_name}' to {recipient}: {exc}")

        print(f"[EMAIL] {sent} team report(s) emailed successfully.")
    else:
        if TEAM_EMAILS_RAW:
            print("[EMAIL] Email sending is disabled. Ensure SMTP_HOST and SENDER_EMAIL are set.")
        else:
            print("[EMAIL] No TEAM_EMAILS configured; skipping email delivery.")

if __name__ == "__main__":
    main()