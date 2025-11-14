import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.parse as urlparse
import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import altair as alt

import firebase_admin
from firebase_admin import credentials, firestore
import ast # <-- ADD THIS IMPORT

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Northern Rockies Snow Report",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Debug flags
DEBUG_TABLE = False
DEBUG_CHART = False

LOCAL_TZ = ZoneInfo("America/Denver")
FRESHNESS_TOLERANCE_HOURS = 30 # For "fresh" snow

# SNOTEL iframe settings
SNOTEL_CROP_TOP = 410
SNOTEL_CROP_BOTTOM = 480
SNOTEL_VIEW_HEIGHT = 440


def get_snotel_iframe_html(triplet: str, station_name: str, show_years: str | None) -> str:
    """Return HTML snippet cropping NRCS SNOTEL graph nicely."""
    try:
        state = triplet.split(":")[1].strip().upper()
    except Exception:
        state = ""
    name_enc = urlparse.quote(station_name)
    
    # --- FIX: Updated to the correct SNOTEL URL ---
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/WTEQ/{state}/{name_enc}.html"
    
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years.strip()}")
    url = base + "?" + "&".join(params)
    inner_height = SNOTEL_VIEW_HEIGHT + SNOTEL_CROP_TOP + SNOTEL_CROP_BOTTOM
    return f"""
    <div style="width:100%; height:{SNOTEL_VIEW_HEIGHT}px; overflow:hidden; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.15); margin-top: 16px; background: white;">
      <iframe
        src="{url}"
        style="width:100%; height:{inner_height}px; border:0; transform: translateY(-{SNOTEL_CROP_TOP}px);"
        loading="lazy"
        referrerpolicy="no-referrer-when-downgrade"
      ></iframe>
    </div>
    """

# ──────────────────────────────────────────────────────────────
# FIREBASE INIT
# ──────────────────────────────────────────────────────────────
@st.cache_resource
def initialize_firebase():
    try:
        # --- NEW, SAFER FIX ---
        # Check if the LOCAL environment variable is set first.
        cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        
        if cred_path:
            # We are running LOCALLY.
            if not os.path.exists(cred_path):
                st.error(f"LOCAL RUN: Firebase credentials file not found at: {cred_path}")
                st.info("Please check the file name and path in your `export` command.")
                return None
            cred = credentials.Certificate(cred_path)
        else:
            # We are running in STREAMLIT CLOUD.
            # Now it is safe to access st.secrets.
            creds = st.secrets["firebase_service_account"]
            if isinstance(creds, str):
                # This handles the string-parsing error we saw before
                creds_dict = ast.literal_eval(creds)
            else:
                creds_dict = dict(creds)
            cred = credentials.Certificate(creds_dict)
        
        # Initialize app
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)
        
        return firestore.client()
    
    except Exception as e:
        st.error(f"Firebase init error: {e}")
        return None

# ──────────────────────────────────────────────────────────────
# --- SECTION MOVED UP: HELPERS ---
# ──────────────────────────────────────────────────────────────
def get_display_name(resort_name: str) -> str:
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

def parse_open_status(status_string: str) -> (float, str):
    if not isinstance(status_string, str):
        return (0.0, "N/A")
    parts = re.findall(r'\d+', status_string)
    if len(parts) == 2:
        try:
            open_count = float(parts[0])
            total_count = float(parts[1])
            if total_count == 0:
                return (0.0, status_string)
            percentage = open_count / total_count
            return (percentage, status_string)
        except Exception:
            pass
    return (0.0, status_string)


def is_fresh_for_day(qd, lu):
    if pd.isna(lu) or pd.isna(qd):
        return False
    mid = datetime(qd.year, qd.month, qd.day, 12, tzinfo=LOCAL_TZ)
    return abs((lu - mid).total_seconds()) / 3600 <= FRESHNESS_TOLERANCE_HOURS


# ──────────────────────────────────────────────────────────────
# DATA LOADING
# ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=600)
def load_latest_data(_db):
    if _db is None:
        return pd.DataFrame()
    try:
        latest_q = (
            _db.collection("snow_reports")
            .order_by("date", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        latest = [d.to_dict() for d in latest_q]
        if not latest:
            return pd.DataFrame()
        latest_date = latest[0]["date"]
        docs = (
            _db.collection("snow_reports")
            .where(filter=firestore.FieldFilter("date", "==", latest_date))
            .stream()
        )
        st.session_state.latest_date_str = latest_date # Save for Hero
        
        rows = [d.to_dict() for d in docs]
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        def ensure_series(col, default, dtype="str"):
            if col in df.columns:
                s = df[col]
            else:
                s = pd.Series([default] * len(df), index=df.index)
                df[col] = s
            if dtype == "num":
                return pd.to_numeric(s, errors="coerce").fillna(0)
            else:
                return s.astype(str).replace(["nan", "None", "N/A"], "", regex=False)

        obj_cols = [
            "resort", "state", "last_updated", "lifts_open", "runs_open",
            "conditions_surface", "weather_current", "comments"
        ]
        for c in obj_cols:
            df[c] = ensure_series(c, default="", dtype="str")

        num_cols = [
            "snow_24h_summit", "snow_24h_base", "base_depth", "summit_depth",
            "snow_overnight", "temp_base", "temp_summit", "wind_speed"
        ]
        for c in num_cols:
            df[c] = ensure_series(c, default=0, dtype="num")

        # FIX: Use lambda to handle map objects correctly
        if "nws_forecast" not in df.columns:
            df["nws_forecast"] = [{} for _ in range(len(df))]
        else:
            df["nws_forecast"] = df["nws_forecast"].apply(lambda x: x if isinstance(x, dict) else {})

        if "snotel_data" not in df.columns:
            df["snotel_data"] = [{} for _ in range(len(df))]
        else:
            df["snotel_data"] = df["snotel_data"].apply(lambda x: x if isinstance(x, dict) else {})

        def parse_dt(s):
            dt = pd.to_datetime(s, errors="coerce")
            if pd.isna(dt):
                return pd.NaT
            return dt.replace(tzinfo=LOCAL_TZ) if dt.tzinfo is None else dt.astimezone(LOCAL_TZ)

        df["last_updated_dt"] = df["last_updated"].apply(parse_dt)
        df["is_powder"] = (df["snow_24h_summit"] >= 6)

        if "state" not in df.columns:
            df["state"] = ""
            
        # --- NEW STALE DATA LOGIC ---
        
        # 1. DEFINE THE CURRENT SEASON
        # We'll set the season start to October 1st.
        today = datetime.now(LOCAL_TZ).date()
        season_start_year = today.year if today.month >= 10 else today.year - 1
        SEASON_START_DATE = pd.Timestamp(season_start_year, 10, 1, tz=LOCAL_TZ)
        
        # 2. SEASONAL STALE CHECK (for data from last season)
        # If a report is from before this season, zero out EVERYTHING.
        all_snow_cols = ['snow_24h_summit', 'snow_24h_base', 'base_depth', 'summit_depth', 'snow_overnight']
        is_from_this_season = df['last_updated_dt'] >= SEASON_START_DATE
        
        for col in all_snow_cols:
            if col in df.columns:
                df.loc[~is_from_this_season, col] = 0
                
        # 3. FRESH SNOW STALE CHECK (for data > 30h old)
        # For reports that ARE from this season, check if they are "fresh".
        # If not fresh, zero out 24h/overnight snow, but LEAVE base/summit depth.
        now_dt = datetime.now(LOCAL_TZ)
        is_fresh_snow = (now_dt - df['last_updated_dt']).dt.total_seconds() / 3600 < FRESHNESS_TOLERANCE_HOURS
        
        fresh_snow_cols = ['snow_24h_summit', 'snow_overnight']
        
        for col in fresh_snow_cols:
            if col in df.columns:
                # Apply this check ONLY to reports that are from this season
                # but are no longer "fresh" (i.e., > 30h old)
                df.loc[is_from_this_season & ~is_fresh_snow, col] = 0
                
        # --- END STALE DATA LOGIC ---
        
        df["display_name"] = df["resort"].apply(get_display_name) # <-- This line was failing
        df = df.sort_values("snow_24h_summit", ascending=False).reset_index(drop=True)

        return df
    except Exception as e:
        st.error(f"Error loading data from Firestore: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600)
def load_historical_data(_db, days=5):
    if _db is None:
        return pd.DataFrame()
    try:
        dates = [
            (datetime.now(LOCAL_TZ).date() - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days)
        ]
        all_rows = []
        for d in dates:
            docs = (
                _db.collection("snow_reports")
                .where(filter=firestore.FieldFilter("date", "==", d))
                .stream()
            )
            for doc in docs:
                r = doc.to_dict()
                r["query_date"] = d
                all_rows.append(r)
        df = pd.DataFrame(all_rows)
        if df.empty:
            return df
        df["query_date"] = pd.to_datetime(df["query_date"], errors="coerce").dt.tz_localize(LOCAL_TZ)
        df["last_updated_dt"] = pd.to_datetime(df["last_updated"], errors="coerce")
        df["last_updated_dt"] = df["last_updated_dt"].apply(
            lambda x: x.replace(tzinfo=LOCAL_TZ)
            if x is not None and pd.notna(x) and x.tzinfo is None
            else (x.astimezone(LOCAL_TZ) if pd.notna(x) and x.tzinfo is not None else x)
        )
        return df
    except Exception as e:
        st.error(f"Error loading history: {e}")
        return pd.DataFrame()


def prepare_chart_data(df_hist, df_current):
    if df_hist.empty or df_current.empty:
        return pd.DataFrame()
    resorts = df_current["resort"].unique().tolist()
    rows = []
    today = datetime.now(LOCAL_TZ).date()
    days = [(today - timedelta(days=i)) for i in range(4, -1, -1)]

    for r in resorts:
        disp_row = df_current.loc[df_current["resort"] == r, "display_name"]
        if disp_row.empty:
            continue
        disp = disp_row.iloc[0]
        
        subset = df_hist[df_hist["resort"] == r]
        for d in days:
            qd = pd.Timestamp(d, tz=LOCAL_TZ)
            row = subset[subset["query_date"].dt.date == d]
            snow = 0.0
            if not row.empty:
                raw = float(row.iloc[0].get("snow_24h_summit", 0) or 0)
                lu = row.iloc[0]["last_updated_dt"]
                if raw > 0 and is_fresh_for_day(qd, lu):
                    snow = raw
            rows.append({"display_name": disp, "date": d, "snow": snow})

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
        
    totals = df.groupby("display_name", as_index=False)["snow"].sum().rename(columns={"snow": "total_snow"})
    return df.merge(totals, on="display_name")

# ──────────────────────────────────────────────────────────────
# CSS
# ──────────────────────────────────────────────────────────────
def load_css():
    st.markdown("""
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
            
            * {
                font-family: 'Inter', sans-serif !important;
            }
            
            .main {
                background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%);
                background-attachment: fixed;
            }
            
            .block-container {
                padding: 2rem 3rem !important;
                max-width: 1800px;
            }
            
            /* Hero Section */
            .hero {
                background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 50%, #ec4899 100%);
                padding: 2.5rem;
                border-radius: 24px;
                margin-bottom: 2rem;
                box-shadow: 0 20px 60px rgba(59, 130, 246, 0.4);
                text-align: center;
                position: relative;
                overflow: hidden;
            }
            
            .hero::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.05'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
                opacity: 0.3;
            }
            
            .hero-title {
                font-size: 3.5rem !important;
                font-weight: 900 !important;
                color: white !important;
                margin: 0 !important;
                text-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
                position: relative;
                z-index: 1;
            }
            
            .hero-subtitle {
                color: rgba(255, 255, 255, 0.95);
                font-size: 1.2rem;
                margin: 0.5rem 0 0 0;
                position: relative;
                z-index: 1;
            }
            
            /* Metric Cards - Enhanced */
            [data-testid="stMetric"] {
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.8) 0%, rgba(51, 65, 85, 0.6) 100%) !important;
                padding: 1.75rem !important;
                border-radius: 20px !important;
                border: 1px solid rgba(59, 130, 246, 0.2) !important;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3) !important;
                transition: all 0.3s ease !important;
                backdrop-filter: blur(20px) !important;
            }
            
            /* --- CSS FIX: Removed the ::before gradient line --- */
            
            [data-testid="stMetric"]:hover {
                transform: translateY(-8px);
                border-color: rgba(59, 130, 246, 0.4) !important;
                box-shadow: 0 16px 48px rgba(59, 130, 246, 0.3) !important;
            }
            
            [data-testid="stMetricLabel"] {
                font-size: 0.8rem !important;
                font-weight: 700 !important;
                text-transform: uppercase !important;
                letter-spacing: 0.1em !important;
                color: #94a3b8 !important;
                margin-bottom: 0.75rem !important;
            }
            
            /* --- CSS FIX: Removed gradient from metric value --- */
            [data-testid="stMetricValue"] {
                font-size: 2.75rem !important;
                font-weight: 900 !important;
                color: #ffffff !important; /* Solid white text */
                line-height: 1.2 !important;
            }
            
            [data-testid="stMetricDelta"] {
                font-size: 0.9rem !important;
                color: #cbd5e1 !important;
                font-weight: 600 !important;
                margin-top: 0.5rem !important;
            }
            
            [data-testid="stMetricDelta"] svg {
                display: none !important;
            }
            
            /* Section Headers - More Visual */
            .section-header {
                font-size: 1.75rem;
                font-weight: 800;
                color: white;
                margin: 2.5rem 0 1.5rem 0;
                padding: 1rem 1.5rem;
                background: linear-gradient(135deg, rgba(59, 130, 246, 0.15), rgba(139, 92, 246, 0.1));
                border-left: 4px solid #3b82f6;
                border-radius: 12px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }
            
            /* Powder Alert Banner - More Dramatic */
            .powder-alert {
                background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
                padding: 2rem;
                border-radius: 20px;
                margin-bottom: 2rem;
                box-shadow: 0 12px 40px rgba(220, 38, 38, 0.5);
                border: 2px solid rgba(255, 255, 255, 0.2);
                animation: pulse-glow 2s ease-in-out infinite;
            }
            
            @keyframes pulse-glow {
                0%, 100% { 
                    box-shadow: 0 12px 40px rgba(220, 38, 38, 0.5);
                    transform: scale(1);
                }
                50% { 
                    box-shadow: 0 12px 60px rgba(220, 38, 38, 0.7);
                    transform: scale(1.01);
                }
            }
            
            .powder-alert-title {
                font-size: 2rem;
                font-weight: 900;
                color: white;
                margin: 0 0 0.75rem 0;
                text-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
            }
            
            .powder-alert-text {
                color: rgba(255, 255, 255, 0.95);
                font-size: 1.1rem;
                margin-bottom: 1rem;
                font-weight: 500;
            }
            
            /* Leaderboard Table - Enhanced */
            [data-testid="stDataFrame"] {
                background: linear-gradient(135deg, rgba(15, 23, 42, 0.8), rgba(30, 41, 59, 0.6)) !important;
                border-radius: 20px !important;
                border: 1px solid rgba(59, 130, 246, 0.2) !important;
                box-shadow: 0 12px 48px rgba(0, 0, 0, 0.4) !important;
                overflow: hidden !important;
            }
            
            [data-testid="stDataFrame"] * {
                color: white !important;
            }
            
            [data-testid="stDataFrame"] th {
                background: linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(139, 92, 246, 0.15)) !important;
                font-weight: 800 !important;
                text-transform: uppercase !important;
                letter-spacing: 0.08em !important;
                font-size: 0.8rem !important;
                padding: 1.25rem !important;
                border-bottom: 2px solid rgba(59, 130, 246, 0.3) !important;
            }
            
            [data-testid="stDataFrame"] td {
                padding: 1rem !important;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05) !important;
                font-weight: 500 !important;
            }
            
            [data-testid="stDataFrame"] tbody tr:hover {
                background: linear-gradient(90deg, rgba(59, 130, 246, 0.15), rgba(139, 92, 246, 0.1)) !important;
                transform: translateX(4px);
                transition: all 0.2s ease;
            }
            
            /* Resort Detail Cards - More Depth */
            [data-testid="stVerticalBlockBorderWrapper"] {
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.7), rgba(51, 65, 85, 0.5)) !important;
                border-radius: 20px !important;
                border: 1px solid rgba(59, 130, 246, 0.15) !important;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3) !important;
                backdrop-filter: blur(10px) !important;
            }
            
            /* Readability inside detail cards */
            [data-testid="stVerticalBlockBorderWrapper"] p,
            [data-testid="stVerticalBlockBorderWrapper"] span,
            [data-testid="stVerticalBlockBorderWrapper"] .st-emotion-cache-1j9prn,
            [data-testid="stVerticalBlockBorderWrapper"] .st-emotion-cache-10trblm,
            [data-testid="stVerticalBlockBorderWrapper"] div {
                color: #e2e8f0 !important; /* light gray for text */
            }
            
            [data-testid="stVerticalBlockBorderWrapper"] h2 {
                color: white !important;
            }
            [data-testid="stVerticalBlockBorderWrapper"] h3 {
                color: #60a5fa !important; /* blue for subheaders */
            }
            [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMetricValue"] {
                color: #ffffff !important; /* bright white for numbers */
            }
            [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMetricLabel"] {
                color: #94a3b8 !important; /* muted gray for labels */
            }
            [data-testid="stVerticalBlockBorderWrapper"] .stAlert * {
                color: #e2e8f0 !important;
            }

            /* Progress bars - Gradient */
            [data-testid="stProgressBar"] > div {
                background: rgba(51, 65, 85, 0.8) !important;
                border-radius: 10px !important;
                height: 14px !important;
                box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.3);
            }
            
            [data-testid="stProgressBar"] > div > div {
                background: linear-gradient(90deg, #3b82f6 0%, #8b5cf6 50%, #ec4899 100%) !important;
                border-radius: 10px !important;
                height: 14px !important;
                box-shadow: 0 2px 12px rgba(59, 130, 246, 0.5);
            }
            
            /* Select box - Enhanced */
            [data-baseweb="select"] {
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.9), rgba(51, 65, 85, 0.7)) !important;
                border: 2px solid rgba(59, 130, 246, 0.3) !important;
                border-radius: 16px !important;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2) !important;
            }
            
            [data-baseweb="select"]:hover {
                border-color: rgba(59, 130, 246, 0.5) !important;
            }
            [data-baseweb="select"] * {
                color: #e2e8f0 !important;
            }
            
            /* Info boxes - Gradient */
            [data-testid="stAlert"] {
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.8), rgba(51, 65, 85, 0.6)) !important;
                border-radius: 16px !important;
                border-left: 4px solid #3b82f6 !important;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2) !important;
            }
            
            /* Badges - More Vibrant */
            .badge {
                display: inline-block;
                padding: 0.5rem 1.25rem;
                border-radius: 16px;
                font-weight: 800;
                font-size: 0.8rem;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
            }
            
            .badge-powder {
                background: linear-gradient(135deg, #dc2626, #991b1b);
                color: white;
                animation: pulse-badge 2s ease-in-out infinite;
            }
            
            @keyframes pulse-badge {
                0%, 100% { transform: scale(1); }
                50% { transform: scale(1.05); }
            }
            
            /* Text input - Enhanced */
            [data-testid="stTextInput"] input {
                background: rgba(30, 41, 59, 0.8) !important;
                border: 2px solid rgba(59, 130, 246, 0.3) !important;
                border-radius: 12px !important;
                color: white !important;
                font-weight: 500 !important;
                padding: 0.75rem !important;
            }
            
            [data-testid="stTextInput"] input:focus {
                border-color: #3b82f6 !important;
                box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2) !important;
            }
            
            /* Divider - Gradient */
            hr {
                margin: 3rem 0 !important;
                border: none !important;
                height: 2px !important;
                background: linear-gradient(90deg, transparent, rgba(59, 130, 246, 0.5), rgba(139, 92, 246, 0.5), transparent) !important;
            }
            
            /* Footer - Enhanced */
            .footer {
                text-align: center;
                color: #94a3b8;
                padding: 2.5rem 0;
                font-size: 0.95rem;
                margin-top: 3rem;
                border-top: 2px solid rgba(59, 130, 246, 0.2);
                background: linear-gradient(180deg, transparent, rgba(30, 41, 59, 0.3));
                border-radius: 16px;
            }
        </style>
    """, unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────
# PAGE RENDER
# ──────────────────────────────────────────────────────────────
load_css()
db = initialize_firebase()
if not db:
    st.stop()

df = load_latest_data(db)
if df.empty:
    st.markdown("""
        <div class='hero'>
            <h1 class='hero-title'>❄️ Northern Rockies Snow Report</h1>
            <p class='hero-subtitle'>☁️ No data available yet. Check back soon!</p>
        </div>
    """, unsafe_allow_html=True)
    st.stop()

df_hist = load_historical_data(db, days=5)

# --- NEW, CORRECTED HERO SECTION (Localizes Time) ---
now_utc = datetime.now(ZoneInfo("UTC")) # Get time as UTC
now_local = now_utc.astimezone(LOCAL_TZ) # Convert to MST/MDT
st.markdown(f"""
    <div class='hero'>
        <h1 class='hero-title'>❄️ Northern Rockies Snow Report</h1>
        <p class='hero-subtitle'>Real-time conditions • Live forecasts • Backcountry data</p>
        <p class='hero-subtitle' style='font-size: 0.9rem; margin-top: 0.5rem; opacity: 0.9;'>
            {now_local.strftime('%A, %B %d, %Y at %I:%M %p %Z')} 
        </p>
    </div>
""", unsafe_allow_html=True)
# --- END NEW ---

# --- NEW Powder Alert ---
powder_resorts = df[df['is_powder'] == True]
powder_count = len(powder_resorts)
if powder_count > 0:
    st.markdown(f"""
        <div class='powder-alert'>
            <div class='powder-alert-title'>🔥 POWDER ALERT!</div>
            <div class='powder-alert-text'>
                <strong>{powder_count}</strong> {'resort is' if powder_count == 1 else 'resorts are'} reporting 6" or more of fresh snow in the last 24 hours
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    cols = st.columns(min(powder_count, 4))
    for i, (idx, resort) in enumerate(powder_resorts.head(4).iterrows()):
        with cols[i]:
            st.metric(
                label=resort['display_name'],
                value=f"{resort['snow_24h_summit']:.0f}\"",
                delta=resort['state']
            )
    
    if powder_count > 4:
        st.caption(f"...plus {powder_count - 4} more resorts! ⬇️ Check the leaderboard")
    
    st.markdown("<br>", unsafe_allow_html=True)

# --- KEPT Leaderboard (with new header) ---
st.markdown("<div class='section-header'>📊 Today's Snow Leaderboard</div>", unsafe_allow_html=True)
cols = {
    "display_name": "Resort",
    "snow_24h_summit": "24h Snow",
    "base_depth": "Base Depth",
    "summit_depth": "Summit Depth",
    "lifts_open": "Lifts",
    "runs_open": "Runs",
    "conditions_surface": "Surface",
    "last_updated": "Last Updated",
}
df_ld = df[[k for k in cols.keys() if k in df.columns]].rename(columns=cols)

numeric_cols = ["24h Snow", "Base Depth", "Summit Depth"]
for c in numeric_cols:
    if c in df_ld.columns:
        df_ld[c] = pd.to_numeric(df_ld[c], errors="coerce").fillna(0)

for col in ["Lifts", "Runs", "Surface", "Last Updated"]:
    if col in df_ld.columns:
        df_ld[col] = df_ld[col].astype(str).fillna("N/A")

# --- FIX: Replaced use_container_width with width='stretch' and removed height=400 ---
st.dataframe(
    df_ld.style.format({"24h Snow": '{:.0f}"', "Base Depth": '{:.0f}"', "Summit Depth": '{:.0f}"'}),
    hide_index=True,
    width='stretch'
)

# --- KEPT 5-day chart (with new header) ---
st.markdown("<div class='section-header'>📈 5-Day Snowfall Trends</div>", unsafe_allow_html=True)
cdf = prepare_chart_data(df_hist, df)
if cdf.empty or cdf["snow"].sum() == 0:
    st.info("❄️ No 5-day snowfall data available.")
else:
    sorted_totals = cdf.groupby("display_name", as_index=False)["total_snow"].max().sort_values("total_snow", ascending=False)
    sorted_resort_names = sorted_totals["display_name"].tolist()

    today = datetime.now(LOCAL_TZ).date()
    days_for_legend = [(today - timedelta(days=i)) for i in range(4, -1, -1)]
    sorted_day_labels = [d.strftime("%a %m/%d") for d in days_for_legend]

    x_axis = alt.Axis(
        labelColor="white",
        grid=True,
        gridColor="rgba(148,163,184,0.1)",
        titleColor="white",
        tickMinStep=1,
        tickCount=7,
        format="d",
    )

    colors = ["#cbd5e1", "#38bdf8", "#a78bfa", "#14b8a6", "#1e40af"]
    cdf["day_label"] = pd.to_datetime(cdf["date"]).dt.strftime("%a %m/%d")

    bars = alt.Chart(cdf).mark_bar().encode(
        y=alt.Y(
            "display_name:N",
            title=None,
            axis=alt.Axis(labelColor="white", labelFontSize=14),
            sort=sorted_resort_names
        ),
        x=alt.X(
            "snow:Q",
            stack="zero",
            title="5-Day Snowfall (inches)",
            axis=x_axis,
        ),
        color=alt.Color(
            "day_label:N",
            title="Day",
            scale=alt.Scale(range=colors),
            sort=sorted_day_labels,
            legend=alt.Legend(
                labelColor="white",
                titleColor="white",
                orient="top",
                direction="horizontal",
            ),
        ),
        tooltip=[
            alt.Tooltip("display_name:N", title="Resort"),
            alt.Tooltip("day_label:N", title="Day"),
            alt.Tooltip("snow:Q", title="Snow", format=".1f"),
        ],
    )

    segment_labels = (
        alt.Chart(cdf)
        .transform_window(
            sort=[alt.SortField("date", order="ascending")],
            frame=[None, 0],
            groupby=["display_name"],
            cumulative_sum="sum(snow)",
        )
        .transform_calculate(
            snow_start="datum.cumulative_sum - datum.snow",
            snow_midpoint="(datum.cumulative_sum + datum.snow_start) / 2"
        )
        .transform_filter("datum.snow >= 1")
        .mark_text(align="center", baseline="middle", fontSize=12, fontWeight="bold", color="black")
        .encode(
            y=alt.Y("display_name:N", sort=sorted_resort_names),
            x=alt.X("snow_midpoint:Q", axis=None),
            text=alt.Text("snow:Q", format=".0f"),
        )
    )

    totals = cdf.groupby("display_name", as_index=False)["total_snow"].max()
    totals_chart = alt.Chart(totals).transform_calculate(
        total_label = "datum.total_snow > 0 ? format(datum.total_snow, '.0f') + ' Total' : ''"
    ).mark_text(
        align="left", baseline="middle", dx=6, color="white", fontWeight="bold", fontSize=14
    ).encode(
        y=alt.Y("display_name:N", sort=sorted_resort_names),
        x=alt.X("total_snow:Q", axis=None),
        text=alt.Text("total_label:N")
    )

    chart = (
        alt.layer(bars, segment_labels, totals_chart)
        .configure_view(strokeWidth=0)
        .properties(background="transparent", height=alt.Step(50))
    )
    st.altair_chart(chart, width="stretch", theme=None)

st.caption("💡 Chart shows 5-day snowfall (only fresh daily reports counted).")

# --- KEPT Forecast map (with new header) ---
st.markdown("<div class='section-header'>🌨️ Snowfall Forecast</div>", unsafe_allow_html=True)
st.caption("💡 Click anywhere on the map for detailed point forecasts. Use the menu (⋮) to switch layers.")
components.html(
    """
<div style="border-radius: 16px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.3);">
  <iframe width="100%" height="550"
    src="https://embed.windy.com/embed.html?type=map&zoom=6&lat=46.4&lon=-113.4&overlay=snowAccu&product=ecmwf"
    frameborder="0"></iframe></div>""",
    height=570,
    scrolling=False,
)

st.divider()

# --- REPLACED: New Detailed Resort Info Section ---
st.markdown("<div class='section-header'>🔍 Detailed Resort Information</div>", unsafe_allow_html=True)

# --- FIX: Added caption text ---
st.caption("💡 Select any resort to view comprehensive conditions, forecasts, and backcountry data")

selected_display_name = st.selectbox(
    "Select a resort:",
    df["display_name"].tolist(),
    index=0,
    label_visibility="collapsed"
)

if selected_display_name:
    r = df[df["display_name"] == selected_display_name].iloc[0]
    
    # --- Native Header Card ---
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"<h2 style='color: white; font-size: 2rem; font-weight: 800; margin: 0;'>{r['display_name']}</h2>", unsafe_allow_html=True)
            st.caption(f"⏱️ Last updated: {r.get('last_updated') or 'N/A'}")
        with c2:
            if r.get("is_powder"):
                st.markdown('<span class="badge badge-powder">🔥 POWDER!</span>', unsafe_allow_html=True)
        
        st.markdown("---") 

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("24-Hour Snow", f"{r.get('snow_24h_summit', 0):.0f}\"")
        c2.metric("Base Depth", f"{r.get('base_depth', 0):.0f}\"")
        c3.metric("Summit Depth", f"{r.get('summit_depth', 0):.0f}\"")
        c4.metric("Overnight", f"{r.get('snow_overnight', 0):.0f}\"")

    
    # --- Native Two-Column Layout ---
    col_left, col_right = st.columns([1, 1], gap="large")
    
    # --- LAYOUT FIX: "Operations Status" moved to left column, first ---
    with col_left:
        # Native Operations
        with st.container(border=True):
            st.markdown("<h3 style='color: #a78bfa;'>🎿 Operations Status</h3>", unsafe_allow_html=True)
            lift_pct, lift_text = parse_open_status(r.get('lifts_open'))
            st.progress(lift_pct, text=f"Lifts: {lift_text}")
            
            run_pct, run_text = parse_open_status(r.get('runs_open'))
            st.progress(run_pct, text=f"Runs: {run_text}")

        # Native Snow & Weather
        with st.container(border=True):
            st.markdown("<h3 style='color: #60a5fa;'>❄️ Snow & Weather</h3>", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            c1.metric("Base Temp", f"{r.get('temp_base', 0):.0f}°F" if pd.notna(r.get('temp_base')) and r.get('temp_base') != 0 else "N/A")
            c2.metric("Summit Temp", f"{r.get('temp_summit', 0):.0f}°F" if pd.notna(r.get('temp_summit')) and r.get('temp_summit') != 0 else "N/A")
            c1.metric("Wind Speed", f"{r.get('wind_speed', 0):.0f} mph" if pd.notna(r.get('wind_speed')) and r.get('wind_speed') != 0 else "N/A")
            c2.metric("Surface", r.get('conditions_surface', 'N/A') or "N/A")
            weather = r.get('weather_current', 'N/A') or "N/A"
            if weather != "N/A":
                st.caption(f"Current Weather: **{weather}**")

    with col_right:
        # Native NWS Forecast
        with st.container(border=True):
            st.markdown("<h3 style='color: #5eead4;'>🌦️ 48-Hour Forecast</h3>", unsafe_allow_html=True)
            nws = r.get("nws_forecast", {}) or {}
            if isinstance(nws, dict) and nws:
                c1, c2 = st.columns(2)
                c1.metric("Forecast Snow", f"{float(nws.get('total_snow_inches', 0) or 0):.1f}\"")
                c2.metric("Precip. Chance", f"{nws.get('precip_probability_max', 0)}%")
                c1.metric("Low Temp", f"{nws.get('temp_low_f', 'N/A')}°F")
                c2.metric("High Temp", f"{nws.get('temp_high_f', 'N/A')}°F")
            else:
                st.info("Forecast data unavailable")
        
        # Native SNOTEL
        with st.container(border=True):
            st.markdown("<h3 style='color: #fbbf24;'>🏔️ SNOTEL Backcountry</h3>", unsafe_allow_html=True)
            snotel = r.get("snotel_data", {}) or {}
            triplet = snotel.get("triplet")
            station_name = snotel.get("station_name")
            
            if triplet and station_name:
                st.caption(f"Station: **{station_name}**")
                c1, c2 = st.columns(2)
                c1.metric("24h Snow Δ", f"{snotel.get('snow_depth', 'N/A')}\"")
                c2.metric("% Median SWE", f"{snotel.get('percent_of_median', 'N/A')}%")
                c1.metric("24h SWE Δ", f"{snotel.get('swe', 'N/A')}\"")
                c2.metric("Density", f"{snotel.get('density', 'N/A')}%")
                
                # --- LAYOUT FIX: SNOTEL chart & input moved here ---
                st.markdown("<br>", unsafe_allow_html=True)
                show_years = st.text_input(
                    "Compare year:",
                    value="",
                    placeholder="e.g. 2023",
                    key=f"snotel_{r['resort']}"
                ) or None
                
                html = get_snotel_iframe_html(triplet, station_name, show_years)
                components.html(html, height=SNOTEL_VIEW_HEIGHT, scrolling=False)

            else:
                st.info("SNOTEL data unavailable")
    
    # Comments section (full width below)
    if r.get("comments"):
        st.markdown("<br>", unsafe_allow_html=True)
        st.info(r['comments'])
    
    # --- LAYEAYOUT FIX: SNOTEL Chart section removed from here ---

# --- END REPLACED SECTION ---


# --- NEW Footer ---
st.markdown("""
    <div class='footer'>
        <strong>Northern Rockies Snow Report</strong><br>
        Data refreshes every 10 minutes • Built with ❤️ for powder seekers<br>
        <span style='font-size: 0.8rem; color: #475569;'>
            Combining resort reports, NWS forecasts, and SNOTEL backcountry data
        </span>
    </div>
""", unsafe_allow_html=True)
