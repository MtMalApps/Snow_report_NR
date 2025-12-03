"""
Daily Snow Report Email Sender

This script reads the latest snow report data from Firebase Firestore
and sends a consolidated email report to subscribers.

Usage:
    python send_daily_report.py

Environment Variables Required:
    - GMAIL_ADDRESS: Your Gmail address for sending reports
    - GMAIL_CREDENTIALS_PATH: Path to OAuth credentials.json (default: credentials.json)
    - GMAIL_TOKEN_PATH: Path to OAuth token.json (default: token.json)
    - FIREBASE_SERVICE_ACCOUNT: Path to Firebase service account JSON

Author: Split from snow_report_scraper_oauth.py
"""

import os
import sys
import json
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
import re
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Import the NWS outlook class (we need it for the banner logic)
from nws_storm_outlook_v3 import NWSStormOutlook

# =============================================================================
# CONFIGURATION
# =============================================================================

# Dashboard URL
DASHBOARD_URL = "https://northern-rockies-snow.streamlit.app/"

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6


# ──────────────────────────────────────────────────────────────
# MASTER RESORT LIST + COORDS (MATCH DASHBOARD)
# ──────────────────────────────────────────────────────────────

MASTER_RESORTS = [
    "Snowbowl",
    "Discovery",
    "LookoutPass",
    "BigMountain",
    "LostTrail",
    "TetonPass",
    "Showdown",
    "Blacktail",
    "BridgerBowl",
    "BigSky",
    "RedLodge",  # or "RedLodge" depending on what Firestore uses
    "Maverick",
    "GreatDivide",
    "BearPaw",
    "SilverMountain",
    "TurnerMountain",
    "Schweitzer",
]


def get_display_name(resort_name: str) -> str:
    name_map = {
        "LookoutPass": "Lookout Pass",
        "BigMountain": "Big Mountain",
        "LostTrail": "Lost Trail",
        "TetonPass": "Teton Pass",
        "Blacktail": "Blacktail",
        "Snowbowl": "Snowbowl",
        "Discovery": "Discovery",
        "Showdown": "Showdown",
        "BridgerBowl": "Bridger Bowl",
        "BigSky": "Big Sky",
        "RedLodge": "Red Lodge Mountain",
        "RedLodgeMountain": "Red Lodge Mountain",
        "GreatDivide": "Great Divide",
        "BearPaw": "Bear Paw",
        "SilverMountain": "Silver Mountain",
        "Schweitzer": "Schweitzer",
        "TurnerMountain": "Turner Mountain",
        "Maverick": "Maverick",
    }
    return name_map.get(resort_name, resort_name)





# Used for SNOTEL distance calculations in the email (similar to RESORTS_DATA in the dashboard)
RESORT_COORDS = {
    "Snowbowl":        {"lat": 47.032417,  "lon": -113.9915282},
    "Discovery":       {"lat": 46.262206,  "lon": -113.246187},
    "LookoutPass":     {"lat": 47.4531005, "lon": -115.706537},
    "BigMountain":     {"lat": 48.502127,  "lon": -114.341252},
    "LostTrail":       {"lat": 45.695247,  "lon": -113.965263},
    "TetonPass":       {"lat": 47.929804,  "lon": -112.816723},
    "Showdown":        {"lat": 46.837747,  "lon": -110.715599},
    "Blacktail":       {"lat": 48.011676,  "lon": -114.365251},
    "BridgerBowl":     {"lat": 45.813919,  "lon": -110.921873},
    "BigSky":          {"lat": 45.280943,  "lon": -111.440644},
    "RedLodge": {"lat": 45.181125, "lon": -109.354325},
    "Maverick":        {"lat": 45.438286,  "lon": -113.142233},
    "GreatDivide":     {"lat": 46.748900,  "lon": -112.328513},
    "BearPaw":         {"lat": 48.162084,  "lon": -109.679937},
    "SilverMountain":  {"lat": 47.499070,  "lon": -116.119163},
    "TurnerMountain":  {"lat": 48.609788,  "lon": -115.648756},
    "Schweitzer":      {"lat": 48.377785,  "lon": -116.633436},
}





# SNOTEL Station Info (needed for distance/elevation display)
SNOTEL_STATION_INFO = {
    # Existing
    "Showdown":     {"triplet": "437:MT:SNTL",  "name": "Deadman Creek",   "lat": 46.79, "lon": -110.68, "elevation": 6440},
    "TetonPass":    {"triplet": "649:MT:SNTL",  "name": "Mount Lockhart",  "lat": 47.92, "lon": -112.82, "elevation": 6430},
    "Discovery":    {"triplet": "850:MT:SNTL",  "name": "Warm Springs",    "lat": 46.27, "lon": -113.16, "elevation": 7840},
    "LostTrail":    {"triplet": "727:MT:SNTL",  "name": "Saddle Mountain", "lat": 45.69, "lon": -113.97, "elevation": 7890},
    "Snowbowl":     {"triplet": "901:MT:SNTL",  "name": "Stuart Mountain", "lat": 47.00, "lon": -113.93, "elevation": 7270},
    "LookoutPass":  {"triplet": "594:ID:SNTL",  "name": "Lookout",         "lat": 47.46, "lon": -115.70, "elevation": 5180},
    "Blacktail":    {"triplet": "1144:MT:SNTL", "name": "Blacktail Mtn",   "lat": 47.98, "lon": -114.35, "elevation": 5660},
    "BigMountain":  {"triplet": "469:MT:SNTL",  "name": "Emery Creek",     "lat": 48.43, "lon": -113.94, "elevation": 4340},
    
    # New Additions
    "SilverMountain": {"triplet": "803:ID:SNTL", "name": "Sunset",            "lat": 47.56, "lon": -115.82, "elevation": 5580},
    "TurnerMountain": {"triplet": "311:MT:SNTL", "name": "Banfield Mountain", "lat": 48.57, "lon": -115.45, "elevation": 5580},
    "BearPaw":        {"triplet": "917:MT:SNTL", "name": "Rocky Boy",         "lat": 48.17, "lon": -109.65, "elevation": 4730},
    "GreatDivide":    {"triplet": "903:MT:SNTL", "name": "Nevada Ridge",      "lat": 46.84, "lon": -112.51, "elevation": 7030},
    "Schweitzer":     {"triplet": "738:ID:SNTL", "name": "Schweitzer Basin",  "lat": 48.37, "lon": -116.64, "elevation": 6090},
    "Maverick":       {"triplet": "656:MT:SNTL", "name": "Mule Creek",        "lat": 45.41, "lon": -112.96, "elevation": 8300},
    "BigSky":         {"triplet": "590:MT:SNTL", "name": "Lone Mountain",     "lat": 45.27, "lon": -111.43, "elevation": 8810},
    "BridgerBowl":    {"triplet": "365:MT:SNTL", "name": "Brackett Creek",    "lat": 45.89, "lon": -110.94, "elevation": 7370},
    "RedLodge":       {"triplet": "407:MT:SNTL", "name": "Cole Creek",        "lat": 45.19, "lon": -109.35, "elevation": 7910},
}


# Data directory (for CSV chart data)
DATA_DIR = os.path.expanduser("~/snow_reports/data")

# Timezone and freshness config (to mirror dashboard logic)
LOCAL_TZ = ZoneInfo("America/Denver")
FRESHNESS_TOLERANCE_HOURS = 30  # used for "fresh" snow definitions

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def normalize_text(s: str) -> str:
    """Replace curly quotes, dashes, and non-breaking spaces with ASCII equivalents."""
    if not s:
        return s

    replacements = {
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' ',  # non-breaking space -> normal space
    }
    for orig, repl in replacements.items():
        s = s.replace(orig, repl)
    
    return s

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in miles."""
    from math import radians, sin, cos, asin, sqrt
    R = 3958.7613  # Earth radius in miles
    phi1, lam1, phi2, lam2 = map(radians, [lat1, lon1, lat2, lon2])
    dphi, dlam = (phi2 - phi1), (lam2 - lam1)
    a = sin(dphi/2)**2 + cos(phi1) * cos(phi2) * sin(dlam/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

# =============================================================================
# FIREBASE CONNECTION
# =============================================================================

def initialize_firebase() -> Optional[firestore.Client]:
    """Initialize Firebase Admin SDK."""
    firebase_cred_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    
    if not firebase_cred_path:
        print("⚠️  FIREBASE_SERVICE_ACCOUNT environment variable not set")
        return None
    
    if not os.path.exists(firebase_cred_path):
        print(f"⚠️  Firebase credentials file not found: {firebase_cred_path}")
        return None
    
    try:
        # Check if already initialized
        try:
            firebase_admin.get_app()
            print("✅ Firebase already initialized")
        except ValueError:
            # Not initialized, initialize now
            cred = credentials.Certificate(firebase_cred_path)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized successfully")
            
        db = firestore.client()
        return db
    except Exception as e:
        print(f"❌ Error initializing Firebase: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_latest_resort_data_from_firebase(db) -> list[dict]:
    """
    Return a list of resort dicts, one for EACH entry in MASTER_RESORTS,
    in that order, with Firestore data merged in when available.

    This guarantees:
      - All resorts appear in the email every day.
      - SNOTEL + NWS dicts are present (possibly empty).
    """
    if db is None:
        # No DB → just return default stubs for every resort
        resort_list = []
        for r in MASTER_RESORTS:
            resort_list.append({
                "resort": r,
                "display_name": get_display_name(r),
                "date": None,
                "last_updated": "No Report",
                "last_updated_dt": None,
                "snow_overnight": 0.0,
                "snow_24h_summit": 0.0,
                "snow_24h_base": 0.0,
                "base_depth": 0.0,
                "summit_depth": 0.0,
                "temp_base": None,
                "temp_summit": None,
                "wind_speed": None,
                "lifts_open": "N/A",
                "runs_open": "N/A",
                "conditions_surface": "N/A",
                "comments": "",
                "nws_forecast": {},
                "snotel_data": {},
            })
        return resort_list

    # 1) Get most recent report date
    latest_q = (
        db.collection("snow_reports")
        .order_by("date", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    latest_docs = [d.to_dict() for d in latest_q]
    if not latest_docs:
        # No reports at all → just stubs for all resorts
        return get_latest_resort_data_from_firebase(None)

    latest_date = latest_docs[0]["date"]

    # 2) Grab all docs for that date
    docs = (
        db.collection("snow_reports")
        .where(filter=firestore.FieldFilter("date", "==", latest_date))
        .stream()
    )
    raw_rows = [d.to_dict() for d in docs]

    # 3) Index by resort key
    by_resort: dict[str, dict] = {}
    for row in raw_rows:
        r_key = row.get("resort")
        if not r_key:
            continue
        by_resort[r_key] = row

    # 4) Build full master list in fixed order
    resort_list: list[dict] = []
    for r in MASTER_RESORTS:
        base = by_resort.get(r, {})

        # Numeric helpers
        def num(field, default=0.0):
            try:
                return float(base.get(field, default) or default)
            except (TypeError, ValueError):
                return default

        # String helpers
        def text(field, default="N/A"):
            val = base.get(field, default)
            if val is None:
                return default
            return str(val)

        # Dict helpers
        nws = base.get("nws_forecast")
        if not isinstance(nws, dict):
            nws = {}
        snotel = base.get("snotel_data")
        if not isinstance(snotel, dict):
            snotel = {}

        # last_updated parsing
        last_updated_str = base.get("last_updated")
        last_updated_dt = None
        if last_updated_str:
            try:
                dt = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M")
                last_updated_dt = dt.replace(tzinfo=LOCAL_TZ)
            except Exception:
                last_updated_dt = None

        resort_list.append({
            "resort": r,
            "display_name": get_display_name(r),
            "date": base.get("date", latest_date),
            "last_updated": last_updated_str or "No Report",
            "last_updated_dt": last_updated_dt,

            "snow_overnight": num("snow_overnight", 0.0),
            "snow_24h_summit": num("snow_24h_summit", 0.0),
            "snow_24h_base": num("snow_24h_base", 0.0),
            "base_depth": num("base_depth", 0.0),
            "summit_depth": num("summit_depth", 0.0),
            "temp_base": base.get("temp_base"),
            "temp_summit": base.get("temp_summit"),
            "wind_speed": base.get("wind_speed"),

            "lifts_open": text("lifts_open"),
            "runs_open": text("runs_open"),
            "conditions_surface": text("conditions_surface"),
            "comments": text("comments", ""),

            "nws_forecast": nws,
            "snotel_data": snotel,
        })

    return resort_list


# =============================================================================
# STALE DATA LOGIC (mirrors dashboard-style behavior)
# =============================================================================

def apply_stale_logic_to_resorts(resort_data_list: List[Dict]) -> List[Dict]:
    """
    Apply seasonal and freshness logic to resort data so that:
    - Data from a previous season (before Oct 1 of current season) has ALL snow and depth set to 0.
    - Data from this season but older than FRESHNESS_TOLERANCE_HOURS has 24h/overnight snow cleared but keeps depths.
    - Also computes snow_24h_display = max(24h_summit, 24h_base) for unified use.
    """
    if not resort_data_list:
        return resort_data_list

    df = pd.DataFrame(resort_data_list)

    if "last_updated" not in df.columns:
        return resort_data_list

    # Ensure numeric snow/depth columns exist and are numeric
    num_cols = [
        "snow_24h_summit",
        "snow_24h_base",
        "base_depth",
        "summit_depth",
        "snow_overnight",
    ]
    for col in num_cols:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Parse last_updated to timezone-aware datetimes in LOCAL_TZ
    dt = pd.to_datetime(df["last_updated"], format="mixed", errors="coerce")
    try:
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize(LOCAL_TZ)
        else:
            dt = dt.dt.tz_convert(LOCAL_TZ)
    except Exception:
        # If timezone operations fail, keep as-is
        df["last_updated_dt"] = dt
    else:
        df["last_updated_dt"] = dt

    # Season start is October 1 of current or previous year
    today_local = datetime.now(LOCAL_TZ).date()
    season_start_year = today_local.year if today_local.month >= 10 else today_local.year - 1
    season_start_dt = datetime(season_start_year, 10, 1, tzinfo=LOCAL_TZ)

    is_from_this_season = df["last_updated_dt"] >= season_start_dt

    # Zero out *all* snow-related fields for prior seasons
    all_snow_cols = [
        "snow_24h_summit",
        "snow_24h_base",
        "base_depth",
        "summit_depth",
        "snow_overnight",
    ]
    for col in all_snow_cols:
        if col in df.columns:
            df.loc[~is_from_this_season, col] = 0

    # For this season, clear 24h/overnight snow that's too old, but keep depths
    now_dt = datetime.now(LOCAL_TZ)
    age_hours = (now_dt - df["last_updated_dt"]).dt.total_seconds() / 3600
    is_fresh_snow = age_hours < FRESHNESS_TOLERANCE_HOURS

    fresh_snow_cols = ["snow_24h_summit", "snow_24h_base", "snow_overnight"]
    for col in fresh_snow_cols:
        if col in df.columns:
            df.loc[is_from_this_season & ~is_fresh_snow, col] = 0

    # Store a date-only version for sorting / dashboard-style logic
    df["last_updated_date"] = df["last_updated_dt"].dt.date

    # Unified 24h snow (summit vs base) for email & charts
    df["snow_24h_display"] = df[["snow_24h_summit", "snow_24h_base"]].max(axis=1)

    return df.to_dict(orient="records")

# =============================================================================
# EMAIL THEME
# =============================================================================

class EmailTheme:
    # === Backgrounds ===
    BG_OUTER = "#0b1220"      # Main page background
    BG_CONTAINER = "#0f172a"  # Main email body container
    BG_CARD = "#0b1220"       # Background for summary rows & detail tables

    # === Text Colors ===
    TEXT_PRIMARY = "#e2e8f0"     # Main text
    TEXT_SECONDARY = "#94a3b8"   # Muted text
    TEXT_MUTED = "#cbd5e1"       # Muter text
    TEXT_WHITE = "#ffffff"

    # === Accent Colors ===
    ACCENT_BLUE = "#93c5fd"      # Main accent (date badge, SNOTEL title)
    ACCENT_GREEN = "#a7f3d0"     # NWS Outlook title
    
    # === Borders ===
    BORDER_SUBTLE = "rgba(148,163,184,.15)"
    BORDER_MEDIUM = "rgba(148,163,184,.25)"
    BORDER_MICRO = "rgba(148,163,184,.12)"
    DATE_BADGE_BORDER = "rgba(255,255,255,0.12)"
    SHADOW = "rgba(0,0,0,0.35)"

    # === Header ===
    GRADIENT_START = "#1e293b"
    GRADIENT_END   = "#0ea5e9"

    # === Powder/Historic Badges (Summary Table) ===
    POWDER_BADGE_BG = "#22c55e"
    POWDER_BADGE_TEXT = "#052e16"
    HISTORIC_BADGE_BG = "#334155"
    HISTORIC_BADGE_TEXT = "#e2e8f0"
    
    # === SNOTEL Badges (Detail View) ===
    SNOTEL_POWDER_BG = "#22c55e"
    SNOTEL_POWDER_TEXT = "#04120a"
    SNOTEL_HISTORIC_BG = "#64748b"
    SNOTEL_HISTORIC_TEXT = "#e2e8f0"

    # === Storm Watch Banner ===
    STORM_BANNER_BG = "#190b0b"
    STORM_BANNER_BORDER = "rgba(248,113,113,.25)"
    STORM_TEXT = "#fecaca"
    STORM_BADGE_BG = "#7f1d1d"
    STORM_BADGE_BORDER = "rgba(248,113,113,.35)"
    ALERT_RED_LIGHT = "#fca5a5"
    TOTAL_BADGE_BG = "#172554"
    TOTAL_BADGE_TEXT = "#bfdbfe"
    TOTAL_BADGE_BORDER = "rgba(148,163,184,.35)"
    
    # === Hero Badges (Detail View) ===
    HERO_BADGE_24H_BG = "#172554"
    HERO_BADGE_24H_TEXT = "#93c5fd"
    HERO_BADGE_24H_SPAN = "#ffffff"
    HERO_BADGE_BASE_BG = "#042f2e"
    HERO_BADGE_BASE_TEXT = "#5eead4"
    HERO_BADGE_BASE_SPAN = "#e2e8f0"
    
    # === Dividers ===
    DIVIDER_GRADIENT = "linear-gradient(90deg, rgba(148,163,184,.2), rgba(148,163,184,.06))"

    # === Mini-Chart Colors (5-Day) ===
    CHART_COLORS = [
        "#cbd5e1", # D-4
        "#38bdf8", # D-3
        "#a78bfa", # D-2
        "#14b8a6", # D-1
        "#1e40af", # Today
    ]

# =============================================================================
# EMAIL MESSENGER CLASS
# =============================================================================

class EmailSnowReportMessenger:
    """
    Manages email distribution of snow reports with dark theme HTML formatting.
    """
    def __init__(self, db, gmail_address: str, credentials_path: str = "credentials.json",
                 token_path: str = "token.json"):
        self.db = db  # Store the database connection
        self.gmail_address = gmail_address
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.subscribers_file = os.path.join(DATA_DIR, "snow_report_subscribers.csv")
        self.nws_outlook = NWSStormOutlook()
    
    # -------------------------------------------------------------------------
    # UTILITY METHODS
    # -------------------------------------------------------------------------
    def _fmt_inches(self, value) -> str:
        """Format inches with quote mark."""
        if value is None:
            return ""
        s = str(value).strip()
        if not s or s.lower() in ('', 'none', 'n/a'):
            return ""
        
        s = s.replace('"', '"').replace('"', '"').replace('″', '"')
        s = re.sub(r'"{2,}', '"', s).strip()
        
        if s.endswith('"') and re.match(r'^\s*-?\d+(\.\d+)?"\s*$', s):
            return s.strip()
        
        digits = ''.join(c for c in s if c.isdigit() or c in ".-")
        return f'{digits}"' if digits else ""
    
    def _sanitize_text(self, value: str) -> str:
        """Normalize text for HTML output."""
        if value is None:
            return ""
        s = normalize_text(str(value))
        s = re.sub(r'"+', '"', s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    
    def get_24hr_snow(self, resort_data: Dict) -> float:
        """
        Extract 24-hour snow amount using the unified snow_24h_display
        (max of summit/base), falling back to raw fields if needed.
        """
        # Prefer normalized, numeric snow_24h_display when present
        if "snow_24h_display" in resort_data:
            try:
                return float(resort_data.get("snow_24h_display") or 0)
            except (ValueError, TypeError):
                pass

        # Fallback: parse from summit/base fields
        try:
            snow = resort_data.get('snow_24h_summit') or resort_data.get('snow_24h_base', '0')
            snow = ''.join(c for c in str(snow) if c.isdigit() or c == '.')
            return float(snow or 0)
        except (ValueError, TypeError):
            return 0.0
    
    def get_master_sort_key(self, resort_data: Dict) -> tuple:
            """
            Sort key similar to dashboard:
            - Resorts with any report come first.
            - Newest last_updated_date (date-only) first.
            - Then by 24h snow (desc).
            - Then by display name.
            """
            resort_name = resort_data.get('resort', '')
            display_name = get_display_name(resort_name)
    
            last_updated_str = resort_data.get('last_updated', '') or ''
            has_report = bool(last_updated_str and last_updated_str != 'N/A')
    
            # Use last_updated_date from stale logic if available
            lud = resort_data.get("last_updated_date")
            
            # Handle various date formats (String, Datetime, or Pandas Timestamp)
            if isinstance(lud, str):
                try:
                    lud = datetime.fromisoformat(lud).date()
                except ValueError:
                    lud = None
            elif isinstance(lud, datetime):
                lud = lud.date()
    
            # --- FIX STARTS HERE ---
            # NaT objects have toordinal() but raise ValueError when called.
            try:
                date_ord = lud.toordinal() if hasattr(lud, "toordinal") else 0
            except (ValueError, AttributeError):
                date_ord = 0
            # --- FIX ENDS HERE ---
    
            snow_amount = self.get_24hr_snow(resort_data)
    
            return (
                0 if has_report else 1,       # has_report first
                -date_ord,                    # newest date first
                -float(snow_amount),          # more snow first
                display_name                  # tie-break on name
            )

    def _get_display_name(self, resort_name: str) -> str:
        return get_display_name(resort_name)



    def _check_powder_alert(self, resort_data_list: List[Dict]) -> bool:
        """Check if any resort has powder alert worthy snow."""
        today = datetime.now(LOCAL_TZ).date()
        for resort_data in resort_data_list:
            last_updated_str = resort_data.get('last_updated', '')
            if not last_updated_str:
                continue
            last_updated_date = None
            if last_updated_str:
                for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                    try:
                        last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                        break
                    except ValueError:
                        continue
            
            is_current = last_updated_date == today if last_updated_date else False

            if is_current:
                if self.get_24hr_snow(resort_data) >= POWDER_ALERT_THRESHOLD:
                    return True
        return False
    
    # -------------------------------------------------------------------------
    # CHART PREPARATION
    # -------------------------------------------------------------------------

    def _prepare_chart_data(self, resort_data_list: List[Dict]) -> List[Dict]:
        """
        Prepare 5-day snow accumulation chart data by querying Firebase history,
        mirroring dashboard logic:
        - For each resort & query date, use max(24h_summit, 24h_base) for that day.
        - Only count snow if the internal last_updated date matches that date.
        """
        chart_data: List[Dict] = []
        today = datetime.now(LOCAL_TZ).date()
        
        # 1. Fetch last 5 days of history from Firebase
        history_map: Dict[str, Dict[datetime.date, float]] = {}  # { resort_name: { date_obj: snow_amount } }
        
        dates_to_check: List[datetime.date] = []
        for i in range(5):
            d = today - timedelta(days=i)
            dates_to_check.append(d)

        print("📊 Fetching 5-day history from Firebase for charts...")
        try:
            for date_obj in dates_to_check:
                date_str = date_obj.strftime("%Y-%m-%d")
                
                # Query Firebase for that specific date
                docs = self.db.collection('snow_reports').where('date', '==', date_str).stream()
                
                for doc in docs:
                    data = doc.to_dict()
                    r_name = data.get('resort')
                    
                    if not r_name: 
                        continue

                    # --- Check for stale data on that query date ---
                    last_updated_str = data.get('last_updated', '')
                    is_fresh_for_date = False
                    
                    if last_updated_str and last_updated_str != 'N/A':
                        try:
                            lu_dt = pd.to_datetime(last_updated_str, errors='coerce')
                            if pd.notna(lu_dt):
                                if lu_dt.tz is None:
                                    lu_dt = lu_dt.replace(tzinfo=LOCAL_TZ)
                                else:
                                    lu_dt = lu_dt.astimezone(LOCAL_TZ)
                                
                                if lu_dt.date() == date_obj:
                                    is_fresh_for_date = True
                        except Exception:
                            pass
                    
                    # If the data isn't actually from this date, skip it
                    if not is_fresh_for_date:
                        continue

                    if r_name not in history_map:
                        history_map[r_name] = {}
                    
                    snow_val = 0.0
                    try:
                        s_sum = data.get('snow_24h_summit', 0)
                        s_base = data.get('snow_24h_base', 0)
                        
                        def clean_snow(val):
                            if isinstance(val, (int, float)):
                                return float(val)
                            if isinstance(val, str):
                                val = val.replace('"', '').strip()
                                return float(val) if val else 0.0
                            return 0.0

                        v_sum = clean_snow(s_sum)
                        v_base = clean_snow(s_base)
                        
                        # Use max(summit, base) for that day (dashboard-style)
                        snow_val = max(v_sum, v_base)
                    except Exception:
                        snow_val = 0.0

                    history_map[r_name][date_obj] = snow_val

        except Exception as e:
            print(f"⚠️ Error fetching history from Firebase: {e}")

        # 2. Build the chart data structure
        for resort_data in resort_data_list:
            resort_name = resort_data['resort']
            
            values = [0.0] * 5
            date_labels = dates_to_check
            
            r_history = history_map.get(resort_name, {})
            
            for i, date_obj in enumerate(date_labels):
                if date_obj in r_history:
                    values[i] = r_history[date_obj]
            
            total = float(sum(values))
            most_recent_update = today  # not used for sorting in email, but kept for completeness
            
            chart_data.append({
                'resort': resort_name,
                'values': values,
                'date_labels': date_labels,
                'total': total,
                'has_current_data': True,
                'most_recent_update': most_recent_update
            })

        return chart_data
    
    # -------------------------------------------------------------------------
    # HTML GENERATION - HEADER
    # -------------------------------------------------------------------------

    def _generate_email_header(self, is_powder_alert: bool = False) -> str:
        """Generate email header with title and date."""
        today = datetime.now()
        title = "POWDER ALERT - Daily Snow Report" if is_powder_alert else "Daily Snow Report"
        
        date_string = today.strftime('%A, %B %d, %Y')
        preheader_text = "&zwnj;&nbsp;"
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Snow Report</title>
  <style>
    /* --- START MOBILE FIX --- */
    @media only screen and (max-width: 620px) {{
      .container {{ width: 100% !important; }}
      .stack {{ 
        display: block !important; 
        width: 100% !important; 
        padding: 0 !important;
        text-align: left !important;
      }}
      .p-sm {{ padding: 14px !important; }}
      .center-sm {{ text-align: center !important; }}
      .hide-sm {{ display: none !important; }}
      .stack-item {{ padding-left: 0 !important; padding-top: 12px; }}

      .stack-row {{
        display: block !important;
        width: 100% !important;
      }}
      .stack-row-col {{
        display: block !important;
        width: 100% !important;
        box-sizing: border-box !important;
        border-radius: 0 !important;
        border: 0 !important;
      }}
      .stack-row-col-first {{
        border-radius: 10px 10px 0 0 !important;
        border-top: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
        border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
        border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
      }}
      .stack-row-col-last {{
        border-radius: 0 0 10px 10px !important;
        border-bottom: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
        border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
        border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
      }}
      .stack-row-col-mid {{
        border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
        border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
      }}
    }}
    /* --- END MOBILE FIX --- */
  </style>
</head>
<body style="margin:0; padding:0; background:{EmailTheme.BG_OUTER}; color:{EmailTheme.BG_OUTER};">
  <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent; mso-hide:all;">
    {preheader_text}
  </div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{EmailTheme.BG_OUTER};">
    <tr>
      <!-- reduced padding so the card comes closer to the edges of the reading pane -->
      <td align="center" style="padding:8px;">
        <!-- changed width from 700 to 100%, and style width:700px to 100% -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" class="container" style="width:100%; max-width:100%; background:{EmailTheme.BG_CONTAINER}; border-radius:16px; overflow:hidden; box-shadow:0 8px 24px {EmailTheme.SHADOW};">
          <tr>
            <td style="padding:24px 24px 16px 24px; background:linear-gradient(135deg,{EmailTheme.GRADIENT_START},{EmailTheme.GRADIENT_END});">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td class="stack" style="vertical-align:middle; color:{EmailTheme.TEXT_PRIMARY}; font-family:Arial, Helvetica, sans-serif; font-size:13px;">
                    <div style="letter-spacing:.08em; text-transform:uppercase; opacity:.9;">{title}</div>
                    <div style="font-size:22px; font-weight:700; color:{EmailTheme.TEXT_WHITE}; margin-top:4px;">Northern Rockies</div>
                  </td>
                  <td class="stack center-sm" style="vertical-align:middle; text-align:right;">
                    <span style="display:inline-block; background:{EmailTheme.BG_OUTER}; color:{EmailTheme.ACCENT_BLUE}; font-family:Arial, Helvetica, sans-serif; font-size:12px; padding:8px 12px; border-radius:999px; border:1px solid {EmailTheme.DATE_BADGE_BORDER};">
                      {date_string}
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 24px 16px 24px; background:linear-gradient(135deg,{EmailTheme.GRADIENT_START},{EmailTheme.GRADIENT_END}); text-align:center;">
              <a href="{DASHBOARD_URL}" style="font-family:Arial, Helvetica, sans-serif; color:#bfdbfe; font-size:12px; text-decoration:none;">
                → View live dashboard <span style="opacity:0.7;">(updates 5-11am MST)</span>
              </a>
            </td>
          </tr>
"""


    # -------------------------------------------------------------------------
    # HTML GENERATION - Powder Alert
    # -------------------------------------------------------------------------

    def _generate_powder_alert_banner(self, sorted_resorts: List[Dict]) -> str:
        """Generate powder alert banner with resort badges."""
        powder_resorts = []
        today = datetime.now(LOCAL_TZ).date()
        
        for resort_data in sorted_resorts:
            last_updated_str = resort_data.get('last_updated', '')
            if not last_updated_str:
                continue
            
            last_updated_date = None
            for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                try:
                    last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                    break
                except ValueError:
                    continue
            
            is_current = last_updated_date == today if last_updated_date else False
            
            if is_current:
                snow_amount = self.get_24hr_snow(resort_data)
                if snow_amount >= POWDER_ALERT_THRESHOLD:
                    powder_resorts.append({
                        'name': get_display_name(resort_data['resort']),
                        'snow': int(round(snow_amount))
                    })

        if not powder_resorts:
            return ""

        powder_resorts.sort(key=lambda x: x['snow'], reverse=True)
        
        badges_html = []
        for resort in powder_resorts:
            badges_html.append(
                f'<span style="display:inline-block; background:#113b63; border:1px solid rgba(148,163,184,.25); color:#cfe7ff; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:700; margin: 4px;">'
                f"{resort['name']} &middot; {resort['snow']}\""
                '</span>'
            )
        
        # --- START MODIFICATION FOR UNIVERSAL UNICODE ICON ---
        CONTAINER_BG = "#0d1b31"
        HIGHLIGHT_STYLE = "display:inline-flex; align-items:center; background:rgba(255, 255, 255, 0.1); padding:4px 8px; border-radius:6px;"
        
        # Use a simple Unicode snowflake (❄️) that email clients won't block
        UNICODE_ICON = f'<span style="font-size:16px; color:{EmailTheme.TEXT_WHITE}; margin-right:6px;">❄️</span>'

        highlighted_title = f"""
            <span style="{HIGHLIGHT_STYLE}">
                {UNICODE_ICON}
                <div>
                    <div style="font-weight:800; color:{EmailTheme.TEXT_WHITE}; font-size:16px;">
                        POWDER ALERT
                    </div>
                    <div style="font-size:12px; font-weight:400; color:{EmailTheme.TEXT_PRIMARY}; margin-top:2px;">
                        (&ge; {POWDER_ALERT_THRESHOLD}" in last 24 hrs)
                    </div>
                </div>
            </span>
        """
        # --- END MODIFICATION FOR UNIVERSAL UNICODE ICON ---

        return f"""
          <tr>
            <td style="padding:16px 24px; background:{CONTAINER_BG}; border-bottom:1px solid {EmailTheme.BORDER_SUBTLE};">
              <div style="font-family:Arial, Helvetica, sans-serif; line-height:1.5;">
                
                <div style="margin-bottom:12px;">
                    {highlighted_title}
                </div>
                
                <div style="font-weight:800; color:{EmailTheme.TEXT_PRIMARY}; font-size:14px; margin-bottom:6px;">
                    Resorts with Fresh Snow:
                </div>
                
                <div style="display:flex; flex-wrap:wrap; margin: -4px;">
                  {''.join(badges_html)}
                </div>
              </div>
            </td>
          </tr>
        """
    # -------------------------------------------------------------------------
    # HTML GENERATION - SUMMARY TABLE WITH CHARTS
    # -------------------------------------------------------------------------
    
    def _generate_summary_table(self, sorted_resorts: List[Dict], chart_data: List[Dict]) -> str:
        """Generate summary table with 5-day charts."""
        html = [f"""
          <tr>
            <td style="padding:20px 24px 6px 24px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate; border-spacing:0 8px;">
                
                <tr class="hide-sm">
                  <td style="width:38%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">Resort</td>
                  <td style="width:22%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">24-hr</td>
                  <td style="width:40%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">5-Day Trend</td>
                </tr>
                """]
        
        today = datetime.now(LOCAL_TZ).date()
        
        for resort_data in sorted_resorts:
            resort_name = resort_data['resort']
            display_name = get_display_name(resort_name)
            snow_24hr = int(round(self.get_24hr_snow(resort_data)))
            
            # find this resort's chart entry
            resort_chart = next((c for c in chart_data if c['resort'] == resort_name), None)
            
            last_updated_str = resort_data.get('last_updated', '')
            last_updated_date = None
            if last_updated_str:
                for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                    try:
                        last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                        break
                    except ValueError:
                        continue
            
            is_current = last_updated_date == today if last_updated_date else False
            is_powder = is_current and snow_24hr >= POWDER_ALERT_THRESHOLD
            
            if not is_current:
                snow_24hr = 0

            badge_html = ""
            if is_powder:
                badge_html = f'<span style="display:inline-block; background:{EmailTheme.POWDER_BADGE_BG}; color:{EmailTheme.POWDER_BADGE_TEXT}; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; margin-left:8px;">Powder</span>'
            elif not is_current:
                badge_html = f'<span style="display:inline-block; background:{EmailTheme.HISTORIC_BADGE_BG}; color:{EmailTheme.HISTORIC_BADGE_TEXT}; font-size:11px; padding:2px 8px; border-radius:999px; margin-left:8px;">Historic</span>'
            
            last_updated_display = last_updated_str.replace(' ', ' ') if last_updated_str else "N/A"
            
            # mini-chart HTML (unchanged)
            chart_html = self._generate_mini_chart(resort_chart['values'] if resort_chart else [0,0,0,0,0])

            # NEW: 5-day total, from chart_data['total']
            total_5day = 0
            if resort_chart is not None:
                try:
                    total_5day = float(resort_chart.get('total', 0.0))
                except (TypeError, ValueError):
                    total_5day = 0.0
            total_5day_int = int(round(total_5day))

            # build row
            html.append(f"""
                  <tr class="stack-row">
                    <td class="stack-row-col stack-row-col-first" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-radius:10px 0 0 10px; border:1px solid {EmailTheme.BORDER_SUBTLE};">
                      <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:14px; font-weight:700;">
                        {display_name}{badge_html}
                      </div>
                      <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:11px; margin-top:2px;">Last updated: {last_updated_display}</div>
                    </td>
                    <td class="stack-row-col stack-row-col-mid" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-top:1px solid {EmailTheme.BORDER_SUBTLE}; border-bottom:1px solid {EmailTheme.BORDER_SUBTLE}; border-right:1px solid {EmailTheme.BORDER_SUBTLE};">
                      <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:14px; font-weight:700; text-align:right;">{snow_24hr}"</div>
                      <div class="hide-lg" style="display:none; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; text-align:left;">24-hr Snow</div>
                    </td>
                    <td class="stack-row-col stack-row-col-last" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-radius:0 10px 10px 0; border:1px solid {EmailTheme.BORDER_SUBTLE};">
                      {chart_html}
                      <!-- NEW: 5-day total below/right of the chart -->
                      <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:11px; text-align:right; margin-top:4px;">
                        5-day total: <span style="color:{EmailTheme.TEXT_PRIMARY}; font-weight:700;">{total_5day_int}"</span>
                      </div>
                      <div class="hide-lg" style="display:none; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; text-align:left; margin-top:4px;">5-Day Trend</div>
                    </td>
                  </tr>
            """)
        
        legend_items = []
        date_objects = [today - timedelta(days=i) for i in range(5)]
        date_objects.reverse()
        labels = [d.strftime('%a') for d in date_objects] 

        for i, color in enumerate(EmailTheme.CHART_COLORS):
            legend_items.append(
                f'<span style="padding:6px 8px;">'
                f'<span style="display:inline-block; width:14px; height:14px; background:{color}; border-radius:3px; vertical-align:middle;"></span>'
                f'<span style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:12px; margin-left:6px; vertical-align:middle;">{labels[i]}</span>'
                '</span>'
            )

        html.append(f"""
                      </table>
                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 8px;">
                        <tr>
                          <td style="padding:8px 0 2px 0;">
                            {''.join(legend_items)}
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:10px 24px 2px 24px;">
                      <div style="height:1px; background:{EmailTheme.DIVIDER_GRADIENT};"></div>
                    </td>
                  </tr>
            """)
        
        return ''.join(html)


    def _generate_mini_chart(self, values: List[float]) -> str:
        """Generate mini 5-day bar chart with true per-row proportional widths.

        - Each resort uses its own max value for scaling.
        - All 5 days are always rendered as 5 <td> cells.
        - Non-zero segments get proportional widths; zero-days get a small fixed width.
        """
        # Oldest on the left, newest on the right in the final chart
        values_reversed = list(reversed(values))

        # If everything is zero, render a simple empty bar
        if all((v or 0) == 0 for v in values_reversed):
            return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;">
      <tr><td style="height:14px; background:{EmailTheme.BG_OUTER};"></td></tr>
    </table>
"""

        # Clean and clamp to non-negative
        vals = []
        for v in values_reversed:
            try:
                vv = float(v or 0)
            except (TypeError, ValueError):
                vv = 0.0
            vals.append(max(vv, 0.0))

        max_val = max(vals)
        if max_val <= 0:
            # Defensive fallback
            return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;">
      <tr><td style="height:14px; background:{EmailTheme.BG_OUTER};"></td></tr>
    </table>
"""

        # Fractions relative to this resort's max
        fractions = [v / max_val if v > 0 else 0.0 for v in vals]
        sum_frac = sum(fractions)

        # How much width do we reserve for zeros?
        zero_count = sum(1 for v in vals if v == 0)
        zero_width_each = 2.0 if zero_count > 0 else 0.0
        zeros_total = zero_width_each * zero_count

        # Remaining width available for non-zero bars
        pos_total_pct = max(0.0, 100.0 - zeros_total)

        # Compute final widths for each day
        widths: List[float] = []
        for v, frac in zip(vals, fractions):
            if v == 0:
                widths.append(zero_width_each)
            else:
                # Normalize among non-zero days
                if sum_frac > 0:
                    widths.append((frac / sum_frac) * pos_total_pct)
                else:
                    widths.append(0.0)

        # Build HTML – *every* cell gets an explicit width
        html = [
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'width="100%" style="border-radius:6px; overflow:hidden;"><tr>'
        ]

        for v, w, color in zip(vals, widths, EmailTheme.CHART_COLORS):
            if v > 0:
                label = f'{int(round(v))}"'
                text_color = (
                    EmailTheme.BG_CONTAINER
                    if color == EmailTheme.CHART_COLORS[0]
                    else EmailTheme.TEXT_WHITE
                )
                html.append(
                    f'<td style="width:{w:.1f}%; height:14px; background:{color}; '
                    f'color:{text_color}; font-size:11px; text-align:center; '
                    f'white-space:nowrap;">{label}</td>'
                )
            else:
                # Zero day: small spacer segment with no label
                html.append(
                    f'<td style="width:{w:.1f}%; height:14px; background:{EmailTheme.BG_CARD};"></td>'
                )

        html.append('</tr></table>')
        return ''.join(html)

    
    # -------------------------------------------------------------------------
    # HTML GENERATION - NWS STORM BANNER
    # -------------------------------------------------------------------------

    def _generate_nws_storm_banner(self, nws_outlooks: List[Dict]) -> str:
        """Generate storm watch banner for significant weather."""
        if not nws_outlooks:
            return ""
        
        STORM_SNOW_THRESHOLD = 4.0
        STORM_RAIN_THRESHOLD = 0.5
        STORM_WIND_GUST_THRESHOLD = 40.0
        STORM_WIND_SPEED_THRESHOLD = 30.0
        
        heavy_snow = []
        heavy_rain = []
        heavy_wind = []
        
        for outlook in nws_outlooks:
            if not outlook.get('outlook'):
                continue
            
            ol = outlook['outlook']
            resort_name = get_display_name(outlook['resort_name'])
            
            snow_amount = ol.get('total_snow_inches', 0) or 0
            precip_amount = ol.get('total_precip_inches', 0) or 0
            
            # Snow
            if snow_amount >= STORM_SNOW_THRESHOLD:
                heavy_snow.append(f"{resort_name}: {snow_amount:.1f}\" snow")
            
            # Heavy rain (only when not really a snow event)
            if precip_amount >= STORM_RAIN_THRESHOLD and snow_amount < 1.0:
                prob = ol.get('precip_probability_max', 0) or 0
                heavy_rain.append(f"{resort_name}: {precip_amount:.2f}\" ({prob}% chance)")

            # ---------- NEW: Strong wind logic ----------
            wind = ol.get("wind") or {}
            ambient_max = wind.get("ambient_max_mph")
            gust_max = wind.get("gust_max_mph")
            ambient_min = wind.get("ambient_min_mph")

            # Decide if winds are notable
            max_speed = 0.0
            if isinstance(ambient_max, (int, float)):
                max_speed = max(max_speed, float(ambient_max))
            if isinstance(gust_max, (int, float)):
                max_speed = max(max_speed, float(gust_max))

            is_strong_wind = (
                (isinstance(gust_max, (int, float)) and gust_max >= STORM_WIND_GUST_THRESHOLD) or
                (isinstance(ambient_max, (int, float)) and ambient_max >= STORM_WIND_SPEED_THRESHOLD)
            )

            if is_strong_wind and max_speed > 0:
                parts = []

                # Range if both min and max exist
                if isinstance(ambient_min, (int, float)) and isinstance(ambient_max, (int, float)) and (ambient_min or ambient_max):
                    s_min = int(round(ambient_min))
                    s_max = int(round(ambient_max))
                    if s_min == s_max:
                        parts.append(f"{s_max} mph")
                    else:
                        parts.append(f"{s_min}–{s_max} mph")
                elif isinstance(ambient_max, (int, float)) and ambient_max:
                    parts.append(f"up to {int(round(ambient_max))} mph")

                # Gusts
                if isinstance(gust_max, (int, float)) and gust_max:
                    parts.append(f"gusts to {int(round(gust_max))} mph")

                if parts:
                    heavy_wind.append(f"{resort_name}: " + ", ".join(parts))

        # If nothing noteworthy at all, no banner
        if not heavy_snow and not heavy_rain and not heavy_wind:
            return ""
        
        snow_text = ""
        if heavy_snow:
            snow_text = f"""
            <b style="color: {EmailTheme.ACCENT_BLUE};">❄️ Snow Forecast (48hr):</b><br>
            {' · '.join(heavy_snow[:4])}
        """
        
        rain_text = ""
        if heavy_rain:
            rain_text = f"""
            <b style="color: {EmailTheme.ACCENT_BLUE};">🌧️ Heavy Rain Forecast (48hr):</b><br>
            {' · '.join(heavy_rain[:2])}
        """

        wind_text = ""
        if heavy_wind:
            wind_text = f"""
            <b style="color: {EmailTheme.ACCENT_BLUE};">Strong Wind (48hr):</b><br>
            {' · '.join(heavy_wind[:4])}
        """
            
        return f"""
      <tr>
        <td style="padding: 20px 24px 10px 24px;">
          <div style="border-top: 1px solid {EmailTheme.BORDER_SUBTLE}; padding-top: 20px; border-bottom: 1px solid {EmailTheme.BORDER_SUBTLE}; padding-bottom: 20px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="width: 40px; vertical-align: top; padding-top: 4px;">
                  <span style="font-size: 28px; line-height: 1;">⚠️</span>
                </td>
                <td style="font-family: Arial, Helvetica, sans-serif; color: {EmailTheme.TEXT_PRIMARY}; padding-left: 12px;">
                  <div style="font-size: 16px; font-weight: 700; color: {EmailTheme.STORM_TEXT};">STORM WATCH</div>
                  <div style="font-size: 13px; color: {EmailTheme.TEXT_SECONDARY}; line-height: 1.5; margin-top: 8px;">
                    Heavy precipitation and/or strong winds forecast - see detailed reports below.
                    <div style="margin-top: 8px;">{snow_text}</div>
                    <div style="margin-top: 8px;">{rain_text}</div>
                    <div style="margin-top: 8px;">{wind_text}</div>
                  </div>
                </td>
              </tr>
            </table>
          </div>
        </td>
      </tr>
    """

    
    # -------------------------------------------------------------------------
    # HTML GENERATION - DETAILED RESORT SECTIONS
    # -------------------------------------------------------------------------

    def _generate_detailed_section(self, resort_data: Dict, snotel_data: Optional[Dict], nws_outlook: Optional[Dict], station_info: Optional[Dict]) -> str:
        """Generate detailed resort section with hero stats, data table, NWS, and SNOTEL."""
        resort_name = resort_data['resort']
        display_name = get_display_name(resort_name)
        last_updated = resort_data.get('last_updated', 'N/A')
        
        snow_24hr = int(round(self.get_24hr_snow(resort_data)))
        
        today = datetime.now().date()
        last_updated_date = None
        if last_updated:
            for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                try:
                    last_updated_date = datetime.strptime(last_updated, fmt).date()
                    break
                except ValueError:
                    continue
        
        is_current = last_updated_date == today if last_updated_date else False

        if not is_current:
            snow_24hr = 0
            
        base_depth_val = resort_data.get('base_depth') or resort_data.get('summit_depth')
        base_depth = self._fmt_inches(base_depth_val)
        
        data_rows = self._generate_resort_data_rows(resort_data, is_current)
        
        nws_box = self._generate_nws_outlook_box(nws_outlook, display_name) if nws_outlook else ""
        
        snotel_box = self._generate_snotel_box(snotel_data, station_info, resort_name) if snotel_data else ""
        
        return f"""
      <tr>
        <td style="padding:18px 24px 6px 24px;">
          <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:18px; font-weight:800;">{display_name}</div>
          <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:4px;">Last updated {last_updated}</div>
          <div style="margin-top:8px;">
            <div style="display:inline-block; background:{EmailTheme.HERO_BADGE_24H_BG}; color:{EmailTheme.HERO_BADGE_24H_TEXT}; padding:9px 14px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:14px; font-weight:700; border:1px solid {EmailTheme.BORDER_MEDIUM}; margin-right:8px;">
              24h: <span style="color:{EmailTheme.HERO_BADGE_24H_SPAN};">{snow_24hr}"</span>
            </div>
            <div style="display:inline-block; background:{EmailTheme.HERO_BADGE_BASE_BG}; color:{EmailTheme.HERO_BADGE_BASE_TEXT}; padding:9px 14px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:14px; font-weight:700; border:1px solid {EmailTheme.BORDER_MEDIUM};">
              Base: <span style="color:{EmailTheme.HERO_BADGE_BASE_SPAN};">{base_depth if base_depth else 'N/A'}</span>
            </div>
          </div>
        </td>
      </tr>
      <tr>
        <td style="padding:8px 24px 20px 24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td class="stack" style="width:56%; vertical-align:top; padding-right:12px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{EmailTheme.BG_CARD}; border-radius:12px;">
                  {data_rows}
                </table>
              </td>
              <td class="stack stack-item" style="width:44%; vertical-align:top; padding-left:12px;">
                {snotel_box}
                <div style="height:10px;"></div>
                {nws_box}
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:0 24px 8px 24px;">
          <div style="height:1px; background:{EmailTheme.DIVIDER_GRADIENT};"></div>
        </td>
      </tr>
"""

    def _generate_resort_data_rows(self, resort_data: Dict, is_current: bool) -> str:
        """Generate data rows for resort table."""
        rows = []
        
        field_map = [
            ('snow_24h_base', '24-hr Snow – Base', True),
            ('snow_24h_summit', '24-hr Snow – Summit', True),
            ('snow_overnight', 'Overnight Snow', False),
            ('base_depth', 'Base Depth', False),
            ('summit_depth', 'Summit Depth', False),
            ('temp_base', 'Base Temp', False),
            ('temp_summit', 'Summit Temp', False),
            ('wind_speed', 'Wind Speed', False),
            ('lifts_open', 'Lifts Open', False),
            ('runs_open', 'Runs Open', False),
            ('conditions_surface', 'Surface', False),
            ('weather_current', 'Weather', False),
        ]
        
        historic_safe_fields = ['base_depth', 'summit_depth']

        for field_key, label, is_highlight in field_map:
            if not is_current and field_key not in historic_safe_fields:
                continue

            value = resort_data.get(field_key)
            if value is None or str(value).strip().lower() in ('', 'none', 'n/a', '0', '0.0'):
                if is_current and field_key in ['snow_24h_base', 'snow_24h_summit', 'snow_overnight'] and str(value) == '0':
                    pass
                else:
                    continue
            
            if field_key in ['snow_24h_base', 'snow_24h_summit', 'snow_overnight', 'base_depth', 'summit_depth']:
                formatted_value = self._fmt_inches(value)
            elif field_key in ['temp_base', 'temp_summit']:
                formatted_value = f"{self._sanitize_text(value)}°F" if value else ""
            elif field_key == 'wind_speed':
                formatted_value = f"{self._sanitize_text(value)} mph" if value else ""
            else:
                formatted_value = self._sanitize_text(value)
            
            if not formatted_value:
                continue
            
            font_weight = '700' if is_highlight else '400'
            
            rows.append(f"""
    <tr>
      <td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">{label}</td>
      <td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:{font_weight}; text-align:right;">{formatted_value}</td>
    </tr>""")
        
        if rows:
            rows[-1] = rows[-1].replace(f'border-bottom:1px solid {EmailTheme.BORDER_MICRO}', 'border-bottom:0')

        return ''.join(rows)

    def _generate_nws_outlook_box(self, nws_outlook: Optional[Dict], display_name: str) -> str:
        """Generate NWS 48-hour outlook box."""
        if not nws_outlook or not nws_outlook.get('outlook'):
            return ""
        
        ol = nws_outlook['outlook']
        
        prob = ol.get('precip_probability_max', 0)
        precip = f"{ol.get('total_precip_inches', 0):.2f}\" liquid ({prob}% chance)"
        
        snow_val = ol.get('total_snow_inches', 0)
        snow = f"{snow_val:.1f}\" expected"
        
        temp_high = ol.get('temp_high_f', 'N/A')
        temp_low = ol.get('temp_low_f', 'N/A')
        temps = f"High {temp_high}°F, Low {temp_low}°F"
        
        snow_level = ol.get('snow_level_feet', 'N/A')
        snow_level_str = f"{snow_level} ft" if snow_level not in ('N/A', None) else "N/A"
        
        conditions_list = ol.get('conditions')
        if isinstance(conditions_list, list):
            conditions = ", ".join(conditions_list)
        else:
            conditions = ol.get('conditions', 'N/A')

        # ---------- NEW: Wind summary ----------
        wind = ol.get("wind") or {}
        ambient_min = wind.get("ambient_min_mph")
        ambient_max = wind.get("ambient_max_mph")
        gust_max = wind.get("gust_max_mph")
        category = wind.get("category")

        wind_parts: List[str] = []

        # Category (e.g., "moderate")
        if isinstance(category, str) and category.strip():
            wind_parts.append(category.strip().capitalize())

        # Speed range
        range_str = ""
        if isinstance(ambient_min, (int, float)) and isinstance(ambient_max, (int, float)):
            if ambient_min or ambient_max:
                s_min = int(round(ambient_min))
                s_max = int(round(ambient_max))
                if s_min == s_max:
                    range_str = f"{s_max} mph"
                else:
                    range_str = f"{s_min}–{s_max} mph"
        elif isinstance(ambient_max, (int, float)) and ambient_max:
            s_max = int(round(ambient_max))
            range_str = f"up to {s_max} mph"
        elif isinstance(ambient_min, (int, float)) and ambient_min:
            s_min = int(round(ambient_min))
            range_str = f"{s_min} mph"

        if range_str:
            wind_parts.append(range_str)

        # Gusts
        if isinstance(gust_max, (int, float)) and gust_max:
            g = int(round(gust_max))
            wind_parts.append(f"gusts to {g} mph")

        wind_summary = "; ".join(wind_parts)
        # Only render a row if we have something meaningful
        if wind_summary:
            wind_row = f"""<tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Wind (48 hr)</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{wind_summary}</td></tr>"""
        else:
            wind_row = ""

        return f"""
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                    <tr>
                      <td style="padding:12px;">
                        <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_GREEN}; font-weight:800; font-size:12px;">48-Hour Weather Outlook - {display_name}</div>
                        <div style="height:8px;"></div>
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                          <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Precipitation</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{precip}</td></tr>
                          <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Snow</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{snow}</td></tr>
                          <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Temps</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{temps}</td></tr>
                          {wind_row}
                          <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Snow Level</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{snow_level_str}</td></tr>
                          <tr><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%; vertical-align: top;">Conditions</td><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{conditions}</td></tr>
                        </table>
                      </td>
                    </tr>
                  </table>
    """


    def _generate_snotel_box(self, snotel_data: Dict, station_info: Optional[Dict], resort_name: str) -> str:
        """Generate SNOTEL backcountry data box."""
        if not snotel_data:
            return ""

        station_name = snotel_data.get('station_name') or (station_info.get('name') if station_info else 'SNOTEL')

        distance_str = "nearby"
        elevation_str = "—"
        if station_info and {'lat','lon'} <= station_info.keys():
            try:
                rc = RESORT_COORDS.get(resort_name)
                if rc:
                    distance_str = f"{haversine_miles(rc['lat'], rc['lon'], station_info['lat'], station_info['lon']):.1f} mi"
            except Exception:
                pass
        elev = station_info.get('elevation') if station_info else None
        elevation_str = f"{int(elev)} ft" if isinstance(elev,(int,float)) else "—"

        unavailable = snotel_data.get('unavailable', False)
        if unavailable:
            error_reason = snotel_data.get('error_reason')
            msg = "SNOTEL data unavailable"
            if error_reason:
                msg += f" ({error_reason})"
            status_info = f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; color:{EmailTheme.TEXT_SECONDARY}; margin-top:4px;">{msg}.</div>'
            
            return f"""
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                    <tr>
                        <td style="padding:12px 12px 6px 12px;">
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_BLUE}; font-weight:700; font-size:12px;">SNOTEL</div>
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; margin-top:2px;">{station_name}</div>
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:2px;">{distance_str} • {elevation_str}</div>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding:6px 12px 12px 12px;">
                            {status_info}
                        </td>
                    </tr>
                </table>
            """

        def inches(value, fallback='0"'):
            if value in (None, '', 'N/A'): return fallback
            try:
                if isinstance(value, (int, float)): return f'{value:.2f}"'.rstrip('0').rstrip('.')
                if isinstance(value, str) and value.endswith('"'): return value
                digits = ''.join(c for c in str(value) if c.isdigit() or c in ".-")
                return f'{digits}"' if digits else fallback
            except Exception: return fallback

        snow_depth = snotel_data.get('snow_depth', 0)
        snow_depth_display = inches(snow_depth, fallback='0"') if snow_depth != 'melting/settling' else '0"'
        swe_display = inches(snotel_data.get('swe', 0), fallback='0"')
        density = snotel_data.get('density', 'N/A')
        density_display = f"{density}%" if density not in ('N/A', None) else "—"
        snow_quality = snotel_data.get('snow_category', 'N/A')
        
        percent_of_median = snotel_data.get('percent_of_median')
        median_display = f"{int(percent_of_median)}%" if isinstance(percent_of_median, (int, float)) else "—"

        snow_depth_numeric = 0.0
        if isinstance(snow_depth, (int, float)):
            snow_depth_numeric = snow_depth
        
        if 'Powder' in str(snow_quality):
            badge_html = f'<div style="margin-top:8px; display:inline-block; background:{EmailTheme.SNOTEL_POWDER_BG}; color:{EmailTheme.SNOTEL_POWDER_TEXT}; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">{snow_quality}</div>'
        elif snow_depth != 'melting/settling' and snow_depth_numeric > 0:
            badge_html = f'<div style="margin-top:8px; display:inline-block; background:#eab308; color:#0b1220; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">{snow_quality}</div>'
        else:
            badge_html = f'<div style="margin-top:8px; display:inline-block; background:{EmailTheme.SNOTEL_HISTORIC_BG}; color:{EmailTheme.SNOTEL_HISTORIC_TEXT}; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">—</div>'

        return f"""
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                <tr>
                    <td style="padding:12px 12px 6px 12px;">
                        <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_BLUE}; font-weight:700; font-size:12px;">SNOTEL</div>
                        <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; margin-top:2px;">{station_name}</div>
                        <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:2px;">{distance_str} • {elevation_str}</div>
                        {badge_html}
                    </td>
                </tr>
                <tr>
                    <td style="padding:6px 12px 12px 12px;">
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0"
                           width="100%" class="container"
                           style="width:100%; max-width:100%; background:{EmailTheme.BG_CONTAINER};
                                  border-radius:16px; overflow:hidden; box-shadow:0 8px 24px {EmailTheme.SHADOW};">

                            <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">24-hr Snow Δ</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{snow_depth_display}</td></tr>
                            <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">24-hr SWE Δ</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{swe_display}</td></tr>
                            <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Density</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{density_display}</td></tr>
                            <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">% of Median</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{median_display}</td></tr>
                            <tr><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Latest Obs</td><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{snotel_data.get('latest_observation', 'N/A')}</td></tr>
                        </table>
                    </td>
                </tr>
            </table>
        """

    # -------------------------------------------------------------------------
    # HTML GENERATION - FOOTER
    # -------------------------------------------------------------------------
    
    def _generate_footer(self) -> str:
        """Generate email footer."""
        return f"""
          <tr>
            <td style="padding:22px 24px 28px 24px; background:{EmailTheme.BG_CONTAINER}; border-top:1px solid {EmailTheme.BORDER_SUBTLE};">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td class="stack center-sm" style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px;">
                    Data sources: resort public snow reports, NWS API, &amp; nearby SNOTEL stations. Values are best-effort scrapes and may lag resort updates.
                    <div style="font-family:Arial, Helvetica, sans-serif; color:#64748b; font-size:11px; margin-top:12px; text-align:center;">
                      If you no longer want to receive these reports,
                      <a href="mailto:mtmalamuteapps+unsubscribe@gmail.com?subject=Unsubscribe" style="color:#93c5fd; text-decoration:none;">
                        click here to unsubscribe
                      </a>.
                      <br>
                      Tip: Add <span style="color:#e2e8f0;">mtmalamuteapps@gmail.com</span> to your Contacts to ensure delivery.
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    
    # -------------------------------------------------------------------------
    # MAIN EMAIL ASSEMBLY
    # -------------------------------------------------------------------------

    def _build_email_html(self, sorted_resorts: List[Dict], is_powder_alert: bool = False, nws_outlooks: List[Dict] = None) -> str:
        """Build complete HTML email with dark theme."""
        html_parts = []
        
        # Header
        html_parts.append(self._generate_email_header(is_powder_alert))
        
        # Powder alert banner
        if is_powder_alert:
            html_parts.append(self._generate_powder_alert_banner(sorted_resorts))
        
        # Summary table with charts
        chart_data = self._prepare_chart_data(sorted_resorts)
        html_parts.append(self._generate_summary_table(sorted_resorts, chart_data))
        
        # NWS storm banner (if applicable)
        if nws_outlooks:
            html_parts.append(self._generate_nws_storm_banner(nws_outlooks))
        
        # Detailed resort sections
        for resort_data in sorted_resorts:
            resort_name = resort_data['resort']
            
            # Get SNOTEL data from the object
            snotel_data = resort_data.get('snotel_data')
            
            station_info = SNOTEL_STATION_INFO.get(resort_name)
            nws_outlook = next((item for item in nws_outlooks if item['resort_name'] == resort_name), None) if nws_outlooks else None
            
            html_parts.append(self._generate_detailed_section(resort_data, snotel_data, nws_outlook, station_info))
        
        # Footer
        html_parts.append(self._generate_footer())
        
        return ''.join(html_parts)
    
    # -------------------------------------------------------------------------
    # EMAIL SENDING
    # -------------------------------------------------------------------------

    def send_consolidated_report(self, resort_data_list: List[Dict]):
        """
        Send consolidated snow report email via Gmail API OAuth2.
        Data is pre-compiled with NWS and SNOTEL info from Firebase.
        """
        if not os.path.exists(self.subscribers_file):
            print(f"No subscribers file found at {self.subscribers_file}")
            return
        try:
            subscribers_df = pd.read_csv(self.subscribers_file)
            recipients = subscribers_df['email'].tolist()
        except Exception as e:
            print(f"Error reading subscribers: {e}")
            return
        if not recipients:
            print("No subscribers found")
            return
        
        # Apply stale logic so email matches dashboard-style behavior
        resort_data_list = apply_stale_logic_to_resorts(resort_data_list)
        
        # Sort resorts using unified sort key
        sorted_resorts = sorted(resort_data_list, key=self.get_master_sort_key)
        
        # Check powder alert
        is_powder_alert = self._check_powder_alert(sorted_resorts)
        
        # Build NWS outlooks list
        nws_outlooks_for_email = []
        for resort_data in sorted_resorts:
            nws_outlooks_for_email.append({
                "resort_name": resort_data["resort"],
                "outlook": resort_data.get("nws_forecast")
            })

        # Build email HTML
        email_html = self._build_email_html(sorted_resorts, is_powder_alert, nws_outlooks_for_email)
        
        # Debug: write out the exact html
        debug_path = os.path.expanduser("~/email_last_sent.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(email_html)
        print("EMAIL DEBUG | Wrote exact outgoing HTML to:", debug_path)
        
        date_string = datetime.now().strftime('%A, %B %d, %Y')

        # Subject
        if is_powder_alert:
            max_snow = max(self.get_24hr_snow(r) for r in sorted_resorts)
            best_resort = max(sorted_resorts, key=lambda x: self.get_24hr_snow(x))['resort']
            subject = f"POWDER ALERT: {int(max_snow)}\" at {self._get_display_name(best_resort)} - {date_string}"
        else:
            subject = f"Daily Snow Report - {date_string}"
        
        # Gmail API send
        try:
            creds = None
            SCOPES = ['https://www.googleapis.com/auth/gmail.send']
            
            if os.path.exists(self.token_path):
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    print("Refreshing expired token...")
                    creds.refresh(Request())
                    with open(self.token_path, 'w') as token:
                        token.write(creds.to_json())
                else:
                    print(f"FATAL: No valid token.json found at {self.token_path}")
                    print("This script must be run with a valid token.json file.")
                    return 

            service = build('gmail', 'v1', credentials=creds)
            
            message = MIMEMultipart('alternative')
            message['Subject'] = subject
            message['From'] = self.gmail_address
            message['To'] = ', '.join(recipients)

            # List-Unsubscribe headers (improves deliverability)
            local, _, domain = self.gmail_address.partition("@")
            unsubscribe_email = f"{local}+unsubscribe@{domain}" if domain else self.gmail_address
            message['List-Unsubscribe'] = f"<mailto:{unsubscribe_email}?subject=Unsubscribe>"
            message['List-Unsubscribe-Post'] = "List-Unsubscribe=One-Click"
            
            # Plain-text fallback part
            text_part = MIMEText(
                "Northern Rockies Daily Snow Report\n\n"
                "Full snow report available at:\n"
                "https://northern-rockies-snow.streamlit.app/\n\n"
                "Tip: Add mtmalamuteapps@gmail.com to your Contacts to ensure delivery.",
                "plain"
            )
            message.attach(text_part)
            
            # HTML part
            html_part = MIMEText(email_html, 'html')
            message.attach(html_part)

            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
            
            service.users().messages().send(userId='me', body={'raw': raw_message}).execute()
            
            print(f"✅ Report sent successfully to {len(recipients)} recipients")
            
        except HttpError as error:
            print(f"Gmail API error: {error}")
            if 'invalid_grant' in str(error):
                print("FATAL: invalid_grant. The token in GitHub Secrets is expired or revoked.")
        except Exception as e:
            print(f"Error sending email: {e}")

# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """
    Main execution:
    1. Initialize Firebase
    2. Read latest resort data from Firestore
    3. Send email report
    """
    print("=" * 60)
    print("DAILY SNOW REPORT EMAIL SENDER")
    print("=" * 60)
    
    # Get Gmail credentials from environment
    gmail_address = os.environ.get('GMAIL_ADDRESS')
    credentials_path = os.environ.get('GMAIL_CREDENTIALS_PATH', 'credentials.json')
    token_path = os.environ.get('GMAIL_TOKEN_PATH', 'token.json')
    
    if not gmail_address:
        print("❌ GMAIL_ADDRESS environment variable must be set")
        sys.exit(1)
    
    # Initialize Firebase
    print("\n📡 Connecting to Firebase...")
    db = initialize_firebase()
    
    if not db:
        print("❌ Failed to initialize Firebase. Cannot send report.")
        sys.exit(1)
    
    # Get latest resort data from Firebase
    print("\n📊 Retrieving resort data from Firebase...")
    resort_data_list = get_latest_resort_data_from_firebase(db)
    
    if not resort_data_list:
        print("⚠️  No resort data found in Firebase. Cannot send report.")
        sys.exit(0)
    
    print(f"✅ Found data for {len(resort_data_list)} resorts")
    
    # Initialize messenger and send report
    print("\n📧 Preparing email report...")
    messenger = EmailSnowReportMessenger(
        db=db,
        gmail_address=gmail_address,
        credentials_path=credentials_path,
        token_path=token_path
    )
    
    messenger.send_consolidated_report(resort_data_list)
    
    print("\n" + "=" * 60)
    print("EMAIL REPORT COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
