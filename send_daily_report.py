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
DASHBOARD_URL = "https://northern-rockries-snow.streamlit.app/"
DASHBOARD_URL = "https://northern-rockies-snow.streamlit.app/"

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6

# Resort coordinates for distance calculations
RESORT_COORDS = {
    "Snowbowl": {"lat": 46.9137, "lon": -113.9525},
    "Discovery": {"lat": 46.3861, "lon": -113.2144},
    "LookoutPass": {"lat": 47.4536, "lon": -115.7414},
    "BigMountain": {"lat": 48.4853, "lon": -114.3572},
    "LostTrail": {"lat": 45.7019, "lon": -113.9219},
    "TetonPass": {"lat": 47.9299, "lon": -112.8109},
    "Showdown": {"lat": 46.9939, "lon": -110.6647},
    "Blacktail": {"lat": 48.0067, "lon": -114.3589}
}

# SNOTEL Station Info (needed for distance/elevation display)
SNOTEL_STATION_INFO = {
    "Showdown":    {"triplet": "437:MT:SNTL",  "name": "Deadman Creek",   "lat": 46.79, "lon": -110.68, "elevation": 6440},
    "TetonPass":   {"triplet": "649:MT:SNTL",  "name": "Mount Lockhart",  "lat": 47.92, "lon": -112.82, "elevation": 6430},
    "Discovery":   {"triplet": "850:MT:SNTL",  "name": "Warm Springs",    "lat": 46.27, "lon": -113.16, "elevation": 7840},
    "LostTrail":   {"triplet": "727:MT:SNTL",  "name": "Saddle Mountain", "lat": 45.69, "lon": -113.97, "elevation": 7890},
    "Snowbowl":    {"triplet": "901:MT:SNTL",  "name": "Stuart Mountain", "lat": 47.00, "lon": -113.93, "elevation": 7270},
    "LookoutPass": {"triplet": "594:ID:SNTL",  "name": "Lookout",         "lat": 47.46, "lon": -115.70, "elevation": 5180},
    "Blacktail":   {"triplet": "1144:MT:SNTL", "name": "Blacktail Mtn",   "lat": 47.98, "lon": -114.35, "elevation": 5660},
    "BigMountain": {"triplet": "469:MT:SNTL",  "name": "Emery Creek",     "lat": 48.43, "lon": -113.94, "elevation": 4340},
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

def get_latest_resort_data_from_firebase(db: firestore.Client) -> List[Dict]:
    """
    Retrieve the latest data for all resorts from Firebase.
    Returns a list of resort data dictionaries.
    """
    try:
        collection_ref = db.collection('snow_reports')
        
        # Get all documents
        all_docs = collection_ref.stream()
        
        # Group by resort and get the latest for each
        resort_map = {}
        
        for doc in all_docs:
            data = doc.to_dict()
            resort_name = data.get('resort')
            timestamp_str = data.get('timestamp')
            
            if not resort_name or not timestamp_str:
                continue
            
            # Parse timestamp
            try:
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            
            # Keep only the latest entry for each resort
            if resort_name not in resort_map:
                resort_map[resort_name] = (timestamp, data)
            else:
                existing_timestamp = resort_map[resort_name][0]
                if timestamp > existing_timestamp:
                    resort_map[resort_name] = (timestamp, data)
        
        # Extract just the data (not timestamps)
        resort_data_list = [data for timestamp, data in resort_map.values()]
        
        print(f"📊 Retrieved data for {len(resort_data_list)} resorts from Firebase")
        return resort_data_list
        
    except Exception as e:
        print(f"❌ Error retrieving data from Firebase: {e}")
        import traceback
        traceback.print_exc()
        return []

# =============================================================================
# STALE DATA LOGIC (mirrors dashboard behavior)
# =============================================================================

def apply_stale_logic_to_resorts(resort_data_list: List[Dict]) -> List[Dict]:
    """
    Apply seasonal and freshness logic to resort data so that:
    - Data from a previous season (before Oct 1 of current season) has ALL snow and depth set to 0.
    - Data from this season but older than FRESHNESS_TOLERANCE_HOURS has 24h/overnight snow cleared but keeps depths.
    """
    if not resort_data_list:
        return resort_data_list

    df = pd.DataFrame(resort_data_list)

    if "last_updated" not in df.columns:
        return resort_data_list

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

    fresh_snow_cols = ["snow_24h_summit", "snow_overnight"]
    for col in fresh_snow_cols:
        if col in df.columns:
            df.loc[is_from_this_season & ~is_fresh_snow, col] = 0

    # Optionally store a date-only version for other logic
    df["last_updated_date"] = df["last_updated_dt"].dt.date

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
    def __init__(self, gmail_address: str, credentials_path: str = "credentials.json",
                 token_path: str = "token.json"):
        self.gmail_address = gmail_address
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.subscribers_file = os.path.join(DATA_DIR, "snow_report_subscribers.csv")
        self.nws_outlook = NWSStormOutlook()
    
    # -------------------------------------------------------------------------
    # UTILITY METHODS
    # -------------------------------------------------------------------------
    def _fmt_inches(self, value) -> str:
        """Format inches with single quote mark."""
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
        """Extract 24-hour snow amount (prefer summit, fallback base)."""
        try:
            snow = resort_data.get('snow_24h_summit') or resort_data.get('snow_24h_base', '0')
            snow = ''.join(c for c in str(snow) if c.isdigit() or c == '.')
            return float(snow or 0)
        except (ValueError, TypeError):
            return 0.0
    
    def get_master_sort_key(self, resort_data: Dict) -> tuple:
        """Sort key: fresh first, then by snow amount (desc), then recency, then name."""
        resort_name = resort_data.get('resort', '')
        today = datetime.now().date()
        
        last_updated_str = resort_data.get('last_updated', '')
        last_updated_date = None
        if last_updated_str and last_updated_str != 'N/A':
            for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                try:
                    last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                    break
                except ValueError:
                    continue
        
        snow_amount = self.get_24hr_snow(resort_data)
        is_fresh = 0 if (last_updated_date == today) else 1
        snow_sort = -float(snow_amount)
        date_sort = -(last_updated_date.toordinal() if last_updated_date else 0)
        return (is_fresh, snow_sort, date_sort, resort_name)

    def _get_display_name(self, resort_name: str) -> str:
        """Get display-friendly resort name."""
        name_map = {
            "LookoutPass": "Lookout Pass",
            "BigMountain": "Big Mountain",
            "LostTrail": "Lost Trail",
            "TetonPass": "Teton Pass",
            "Blacktail": "Blacktail Mountain",
            "Snowbowl": "Snowbowl",
            "Discovery": "Discovery",
            "Showdown": "Showdown"
        }
        return name_map.get(resort_name, resort_name)

    def _check_powder_alert(self, resort_data_list: List[Dict]) -> bool:
        """Check if any resort has powder alert worthy snow."""
        today = datetime.now().date()
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
        """Prepare 5-day snow accumulation chart data from CSV files."""
        chart_data = []
        today = datetime.now().date()

        for resort_data in resort_data_list:
            resort_name = resort_data['resort']
            csv_file = os.path.join(DATA_DIR, f"{resort_name}_snow_report.csv")

            try:
                if os.path.exists(csv_file):
                    df = pd.read_csv(csv_file)
                    
                    df['report_date'] = pd.to_datetime(df['date'], format='%Y-%m-%d', errors='coerce')
                    # Fix: Add utc=True to handle mixed timezones and silence the warning
                    # We convert to UTC first, which ensures we get a valid datetime object
                    temp_dates = pd.to_datetime(df['last_updated'], format='mixed', errors='coerce', utc=True)
                    
                    # Convert to the local timezone (optional, but good for accuracy) 
                    # or just extract the date. Since we just need the date:
                    df['last_updated_dt'] = temp_dates.dt.date
                    
                    if df['report_date'].isna().all():
                        raise ValueError("All report_dates are NaT")
                        
                    df = df.dropna(subset=['report_date']).sort_values('report_date', ascending=False)
                    most_recent_update = df['report_date'].iloc[0].date()
                    days_since_update = (today - most_recent_update).days

                    values = [0] * 5
                    date_labels = [(today - timedelta(days=i)) for i in range(5)]

                    for i, date in enumerate(date_labels):
                        day_data = df[df['report_date'].dt.date == date]
                        
                        if not day_data.empty:
                            row = day_data.iloc[0]
                            
                            is_stale = (row['last_updated_dt'] is pd.NaT) or (row['last_updated_dt'] != date)
                            if is_stale:
                                values[i] = 0
                                continue

                            snow = row['snow_24h_summit']
                            if pd.isna(snow):
                                snow = row['snow_24h_base']
                            if isinstance(snow, str):
                                snow = snow.replace('"', '').strip()
                            try:
                                values[i] = float(snow) if snow else 0
                            except Exception:
                                values[i] = 0

                    total = sum(values)
                    chart_data.append({
                        'resort': resort_name,
                        'values': values,
                        'date_labels': date_labels,
                        'total': total,
                        'has_current_data': days_since_update < 2,
                        'most_recent_update': most_recent_update
                    })
                else:
                    raise FileNotFoundError("No CSV file")
            except Exception as e:
                print(f"Chart prep error for {resort_name}: {e}")
                chart_data.append({
                    'resort': resort_name,
                    'values': [0, 0, 0, 0, 0],
                    'date_labels': [today - timedelta(days=i) for i in range(5)],
                    'total': 0,
                    'has_current_data': False,
                    'most_recent_update': None
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
      <td align="center" style="padding:24px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="700" class="container" style="width:700px; max-width:100%; background:{EmailTheme.BG_CONTAINER}; border-radius:16px; overflow:hidden; box-shadow:0 8px 24px {EmailTheme.SHADOW};">
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
        today = datetime.now().date()
        
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
                        'name': self._get_display_name(resort_data['resort']),
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

        return f"""
          <tr>
            <td style="padding:16px 24px; background:#051d33; border-bottom:1px solid {EmailTheme.BORDER_SUBTLE};">
              <div style="font-family:Arial, Helvetica, sans-serif; font-size:14px; color:{EmailTheme.TEXT_PRIMARY}; line-height:1.5;">
                <div style="font-weight:800; color:{EmailTheme.TEXT_WHITE}; margin-bottom:6px;">Powder Alert (&ge; {POWDER_ALERT_THRESHOLD}" in last 24 hrs)</div>
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
        
        today = datetime.now().date()
        
        for resort_data in sorted_resorts:
            resort_name = resort_data['resort']
            display_name = self._get_display_name(resort_name)
            snow_24hr = int(round(self.get_24hr_snow(resort_data)))
            
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
            
            chart_html = self._generate_mini_chart(resort_chart['values'] if resort_chart else [0,0,0,0,0])
            
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
        """Generate mini bar chart HTML."""
        values_reversed = list(reversed(values))
        max_val = max(max(values_reversed), 10)
        
        if all(v == 0 for v in values_reversed):
            return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;">
      <tr><td style="height:14px; background:{EmailTheme.BG_OUTER};"></td></tr>
    </table>
"""
        
        html = ['<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;"><tr>']
        
        for i, (val, color) in enumerate(zip(values_reversed, EmailTheme.CHART_COLORS)):
            if val > 0:
                width_pct = (val / max_val) * 100
                text_color = EmailTheme.BG_CONTAINER if color == EmailTheme.CHART_COLORS[0] else EmailTheme.TEXT_WHITE
                html.append(f'<td style="width:{width_pct:.1f}%; height:14px; background:{color}; color:{text_color}; font-size:11px; text-align:center;">{int(round(val))}"</td>')
            else:
                if i == 0 or values_reversed[i-1] == 0:
                    html.append(f'<td style="height:14px; background:{EmailTheme.BG_CARD};"></td>')
        
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
        
        heavy_snow = []
        heavy_rain = []
        
        for outlook in nws_outlooks:
            if not outlook.get('outlook'):
                continue
            
            ol = outlook['outlook']
            resort_name = self._get_display_name(outlook['resort_name'])
            
            snow_amount = ol.get('total_snow_inches', 0)
            precip_amount = ol.get('total_precip_inches', 0)
            
            if snow_amount >= STORM_SNOW_THRESHOLD:
                heavy_snow.append(f"{resort_name}: {snow_amount:.1f}\" snow")
            
            if precip_amount >= STORM_RAIN_THRESHOLD and snow_amount < 1.0:
                prob = ol.get('precip_probability_max', 0)
                heavy_rain.append(f"{resort_name}: {precip_amount:.2f}\" ({prob}% chance)")

        if not heavy_snow and not heavy_rain:
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
                    Heavy precipitation forecast - see detailed reports below.
                    <div style="margin-top: 8px;">{snow_text}</div>
                    <div style="margin-top: 8px;">{rain_text}</div>
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
        display_name = self._get_display_name(resort_name)
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
        snow_level_str = f"{snow_level} ft" if snow_level != 'N/A' and snow_level is not None else "N/A"
        
        conditions_list = ol.get('conditions')
        if isinstance(conditions_list, list):
            conditions = ", ".join(conditions_list)
        else:
            conditions = ol.get('conditions', 'N/A')
        
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
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
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
        
        # Apply stale logic so email matches dashboard behavior
        resort_data_list = apply_stale_logic_to_resorts(resort_data_list)
        
        # Sort resorts
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
