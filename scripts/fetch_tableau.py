"""
fetch_tableau.py — ONDY Tableau Backlog Sync
=============================================
Authenticates with Tableau Server (bi.learn.co.th), downloads the
OndyOnlyTrackingDashboard / 2_Backlog view as CSV, transforms it
into the JSON format expected by ONDY_HQ_Backlog_Monitor.html, and
saves it to data/backlog.json.

Environment variables (set as GitHub Secrets):
  TABLEAU_USERNAME   e.g. your@email.com
  TABLEAU_PASSWORD   your Tableau Server password

Run locally:
  export TABLEAU_USERNAME=xxx TABLEAU_PASSWORD=yyy
  python scripts/fetch_tableau.py
"""

import os, sys, json, csv, io, requests
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
TABLEAU_URL      = "https://bi.learn.co.th"
SITE_CONTENT_URL = "LearnCorp"          # site contentUrl from dashboard URL
API_VERSION      = "3.21"               # Tableau 2024.x — lower to 2.3 if errors
WORKBOOK_NAME    = "OndyOnlyTrackingDashboard"
VIEW_URL_NAME    = "2_Backlog"          # segment after /views/ in the URL
OUTPUT_PATH      = "data/backlog.json"

# Column-name mapping: Tableau column → our field name
# Keys are lowercased & stripped for matching flexibility.
# Add/edit entries to match the actual columns in your view CSV.
COL_MAP = {
    # Area / branch
    "area": "area", "สาขา": "area", "branch": "area",
    # Salesperson code
    "salecode": "name", "sale code": "name", "name": "name",
    "salesperson": "name", "sale": "name", "พนักงาน": "name",
    # Grand total
    "grandtotal": "grand", "grand total": "grand",
    "ยอดขายรวม": "grand", "total": "grand",
    # Monthly columns (Jan–Dec)
    "jan": "m1", "ม.ค.": "m1", "january": "m1",
    "feb": "m2", "ก.พ.": "m2", "february": "m2",
    "mar": "m3", "มี.ค.": "m3", "march": "m3",
    "apr": "m4", "เม.ย.": "m4", "april": "m4",
    "may": "m5", "พ.ค.": "m5",
    "jun": "m6", "มิ.ย.": "m6", "june": "m6",
    "jul": "m7", "ก.ค.": "m7", "july": "m7",
    "aug": "m8", "ส.ค.": "m8", "august": "m8",
    "sep": "m9", "ก.ย.": "m9", "september": "m9",
    "oct": "m10", "ต.ค.": "m10", "october": "m10",
    "nov": "m11", "พ.ย.": "m11", "november": "m11",
    "dec": "m12", "ธ.ค.": "m12", "december": "m12",
    # Conversion stats
    "plantotal": "planTotal", "plan total": "planTotal",
    "เป้าหมาย": "planTotal", "plan": "planTotal",
    "actualtotal": "actualTotal", "actual total": "actualTotal",
    "ยอดจริง": "actualTotal", "actual": "actualTotal",
    "countall": "countAll", "count all": "countAll",
    "จำนวนลูกค้า": "countAll", "total leads": "countAll",
    "countconverted": "countConverted", "count converted": "countConverted",
    "ปิดการขาย": "countConverted", "converted": "countConverted",
}

# ── AUTH ──────────────────────────────────────────────────────────────────────

def sign_in(username: str, password: str) -> tuple[str, str]:
    url = f"{TABLEAU_URL}/api/{API_VERSION}/auth/signin"
    payload = {
        "credentials": {
            "name": username,
            "password": password,
            "site": {"contentUrl": SITE_CONTENT_URL},
        }
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"[ERROR] Sign-in failed {r.status_code}: {r.text[:400]}", file=sys.stderr)
        r.raise_for_status()
    data = r.json()["credentials"]
    token   = data["token"]
    site_id = data["site"]["id"]
    user_id = data["user"]["id"]
    print(f"[OK] Signed in — site_id={site_id}, user_id={user_id}")
    return token, site_id


def sign_out(token: str) -> None:
    url = f"{TABLEAU_URL}/api/{API_VERSION}/auth/signout"
    requests.post(url, headers={"X-Tableau-Auth": token}, timeout=10)
    print("[OK] Signed out")

# ── VIEW LOOKUP ───────────────────────────────────────────────────────────────

def find_view_id(token: str, site_id: str) -> str:
    url = f"{TABLEAU_URL}/api/{API_VERSION}/sites/{site_id}/views"
    params = {"filter": f"viewUrlName:eq:{VIEW_URL_NAME}"}
    r = requests.get(url, headers={"X-Tableau-Auth": token}, params=params, timeout=30)
    r.raise_for_status()
    views = r.json().get("views", {}).get("view", [])
    if not views:
        # Fallback: list all views in the workbook
        print(f"[WARN] View '{VIEW_URL_NAME}' not found via filter, searching workbook...")
        url2 = f"{TABLEAU_URL}/api/{API_VERSION}/sites/{site_id}/views"
        r2 = requests.get(url2, headers={"X-Tableau-Auth": token}, timeout=30)
        all_views = r2.json().get("views", {}).get("view", [])
        for v in all_views:
            wb = v.get("workbook", {}).get("name", "")
            vname = v.get("name", "")
            print(f"  Available view: {wb} / {vname} (id={v['id']})")
        raise ValueError(f"View '{VIEW_URL_NAME}' not found. See available views above.")
    view_id = views[0]["id"]
    view_name = views[0].get("name", "")
    print(f"[OK] Found view: {view_name} (id={view_id})")
    return view_id

# ── DATA DOWNLOAD ─────────────────────────────────────────────────────────────

def download_view_csv(token: str, site_id: str, view_id: str) -> str:
    """Download the underlying data for a view as CSV."""
    url = f"{TABLEAU_URL}/api/{API_VERSION}/sites/{site_id}/views/{view_id}/data.csv"
    r = requests.get(url, headers={"X-Tableau-Auth": token, "Accept": "text/csv"}, timeout=60)
    if r.status_code != 200:
        # Try alternate endpoint (some Tableau versions)
        url2 = f"{TABLEAU_URL}/api/{API_VERSION}/sites/{site_id}/views/{view_id}/export/crosstab/json"
        r2 = requests.get(url2, headers={"X-Tableau-Auth": token}, timeout=60)
        if r2.status_code == 200:
            return _crosstab_json_to_csv(r2.json())
        r.raise_for_status()
    print(f"[OK] Downloaded CSV ({len(r.content):,} bytes)")
    return r.text


def _crosstab_json_to_csv(data: dict) -> str:
    """Convert Tableau crosstab JSON export to CSV string."""
    cols = [c.get("fieldName", f"col{i}") for i, c in enumerate(data.get("columns", []))]
    rows = []
    for row in data.get("data", []):
        rows.append({cols[i]: v for i, v in enumerate(row)})
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()

# ── CSV → JSON TRANSFORM ──────────────────────────────────────────────────────

def _clean_num(val: str) -> float:
    if not val:
        return 0.0
    return float(str(val).replace(",", "").replace(" ", "").replace("฿", "") or 0)


def _map_columns(header: list[str]) -> dict[str, str]:
    """Return {original_col_name: field_name} for columns we recognise."""
    mapping = {}
    for col in header:
        key = col.strip().lower()
        if key in COL_MAP:
            mapping[col] = COL_MAP[key]
    return mapping


def csv_to_json(csv_text: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty — check view permissions or column visibility in Tableau.")

    header = list(rows[0].keys())
    print(f"[INFO] CSV columns ({len(header)}): {header}")
    col_map = _map_columns(header)
    print(f"[INFO] Mapped columns: {col_map}")

    # Check for required fields
    mapped_fields = set(col_map.values())
    required = {"area", "name"}
    missing = required - mapped_fields
    if missing:
        print(f"[WARN] Missing required fields: {missing}")
        print("[WARN] Add entries to COL_MAP at the top of this script to fix column mapping.")

    raw_list = []
    conv_dict = {}

    for row in rows:
        r = {col_map.get(k, k): v for k, v in row.items()}

        area  = str(r.get("area", "")).strip()
        name  = str(r.get("name", "")).strip()
        if not name:
            continue

        grand = _clean_num(r.get("grand", r.get("grandTotal", 0)))
        months = [_clean_num(r.get(f"m{i}", 0)) for i in range(1, 13)]

        raw_list.append([area, name, grand] + months)

        plan_total       = _clean_num(r.get("planTotal", grand))
        actual_total     = _clean_num(r.get("actualTotal", 0))
        count_all        = int(_clean_num(r.get("countAll", 0)))
        count_converted  = int(_clean_num(r.get("countConverted", 0)))

        conv_dict[name] = {
            "planTotal":      plan_total,
            "actualTotal":    actual_total,
            "countAll":       count_all,
            "countConverted": count_converted,
        }

    print(f"[OK] Parsed {len(raw_list)} salesperson rows")
    return {"raw": raw_list, "conv": conv_dict}

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    username = os.environ.get("TABLEAU_USERNAME")
    password = os.environ.get("TABLEAU_PASSWORD")

    if not username or not password:
        print("[ERROR] Set TABLEAU_USERNAME and TABLEAU_PASSWORD environment variables.", file=sys.stderr)
        sys.exit(1)

    token, site_id = sign_in(username, password)
    try:
        view_id  = find_view_id(token, site_id)
        csv_text = download_view_csv(token, site_id, view_id)
        data     = csv_to_json(csv_text)
    finally:
        sign_out(token)

    data["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    data["source"]      = "Tableau REST API"
    data["view"]        = f"{WORKBOOK_NAME} / {VIEW_URL_NAME}"

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[DONE] Saved {len(data['raw'])} rows → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
