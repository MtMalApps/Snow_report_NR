import os
import json
from datetime import datetime, timedelta
import urllib.parse as urlparse
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import altair as alt  # <-- Added import

import firebase_admin
from firebase_admin import credentials, firestore

# -----------------------------
# 1) PAGE CONFIG
# -----------------------------
st.set_page_config(
    page_title="Northern Rockies Snow Report",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

db = None

# -----------------------------
# 2) FIREBASE INIT
# -----------------------------
@st.cache_resource
def initialize_firebase():
    global db
    cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not cred_path:
        st.error("FIREBASE_SERVICE_ACCOUNT environment variable not set.")
        return None
    if not os.path.exists(cred_path):
        st.error(f"Firebase credentials file not found: {cred_path}")
        return None
    try:
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        return db
    except Exception as e:
        st.error(f"Error initializing Firebase: {e}")
        return None

# -----------------------------
# 3) DATA LOADING
# -----------------------------
@st.cache_data(ttl=600)
def load_latest_data():
    if db is None:
        return pd.DataFrame()
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        docs = (
            db.collection("snow_reports")
            .where(filter=firestore.FieldFilter("date", "==", today_str))
            .stream()
        )
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

        def to_dict_safe(x):
            if isinstance(x, dict):
                return x
            if isinstance(x, str) and x.strip().startswith("{"):
                try:
                    return json.loads(x)
                except Exception:
                    try:
                        return eval(x)
                    except Exception:
                        return {}
            return {}

        if "nws_forecast" not in df.columns:
            df["nws_forecast"] = [{} for _ in range(len(df))]
        else:
            df["nws_forecast"] = df["nws_forecast"].apply(to_dict_safe)

        if "snotel_data" not in df.columns:
            df["snotel_data"] = [{} for _ in range(len(df))]
        else:
            df["snotel_data"] = df["snotel_data"].apply(to_dict_safe)

        df["last_updated_dt"] = pd.to_datetime(df["last_updated"], errors="coerce")
        df["is_powder"] = (df["snow_24h_summit"] >= 6)

        if "state" not in df.columns:
            df["state"] = ""
        
        df = df.sort_values("snow_24h_summit", ascending=False).reset_index(drop=True)
        df["display_name"] = df["resort"].apply(get_display_name)

        return df
    except Exception as e:
        st.error(f"Error loading data from Firestore: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600)
def load_historical_data():
    """Load 5 days of historical data from Firestore for charts."""
    if db is None:
        return pd.DataFrame()
    
    try:
        dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]
        
        all_data = []
        for date_str in dates:
            docs = (
                db.collection("snow_reports")
                .where(filter=firestore.FieldFilter("date", "==", date_str))
                .stream()
            )
            for doc in docs:
                data = doc.to_dict()
                data['query_date'] = date_str
                all_data.append(data)
        
        if not all_data:
            return pd.DataFrame()
        
        df = pd.DataFrame(all_data)
        df['query_date'] = pd.to_datetime(df['query_date'])
        df['last_updated_dt'] = pd.to_datetime(df['last_updated'], errors='coerce')
        
        for col in ['snow_24h_summit', 'snow_24h_base']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        
        return df
    except Exception as e:
        st.error(f"Error loading historical data: {e}")
        return pd.DataFrame()

# -----------------------------
# 4) HELPER FUNCTIONS
# -----------------------------
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

# --- REWRITTEN 5-DAY CHART DATA PREP ---
def prepare_chart_data(df_hist, df_current):
    """Prepare 5-day chart data for Altair chart."""
    chart_data = []
    today = datetime.now().date()
    date_range = [(today - timedelta(days=i)) for i in range(4, -1, -1)]  # Oldest to newest
    
    for resort in df_current['resort'].unique():
        display_name = get_display_name(resort)
        total_snow = 0
        
        for date in date_range:
            day_label = date.strftime('%a %m/%d')
            snow_val = 0
            
            day_data = df_hist[
                (df_hist['resort'] == resort) & 
                (df_hist['query_date'].dt.date == date)
            ]
            
            if not day_data.empty:
                row = day_data.iloc[0]
                last_updated_date = row['last_updated_dt'].date() if pd.notna(row['last_updated_dt']) else None
                is_stale = last_updated_date != date if last_updated_date else True
                
                if not is_stale:
                    snow = row['snow_24h_summit']
                    if pd.isna(snow) or snow == 0:
                        snow = row['snow_24h_base']
                    snow_val = float(snow) if pd.notna(snow) else 0
            
            chart_data.append({
                "resort": resort,
                "display_name": display_name,
                "date": date,
                "day_label": day_label,
                "snow": snow_val
            })
            total_snow += snow_val
            
    # Convert to DataFrame
    chart_df = pd.DataFrame(chart_data)
    
    # Get total snow for sorting
    total_snow_df = chart_df.groupby('display_name')['snow'].sum().reset_index().rename(columns={'snow': 'total_snow'})
    chart_df = chart_df.merge(total_snow_df, on='display_name')
    
    return chart_df

# -----------------------------
# 5) SNOTEL IFRAME
# -----------------------------
SNOTEL_CROP_TOP = 410
SNOTEL_CROP_BOTTOM = 480
SNOTEL_VIEW_HEIGHT = 440

def get_snotel_iframe_html(triplet: str, station_name: str, show_years: str | None) -> str:
    try:
        state = triplet.split(":")[1].strip().upper()
    except Exception:
        state = ""
    
    name_enc = urlparse.quote(station_name)
    base = f"https.nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/WTEQ/{state}/{name_enc}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years.strip()}")
    url = base + "?" + "&".join(params)

    inner_height = SNOTEL_VIEW_HEIGHT + SNOTEL_CROP_TOP + SNOTEL_CROP_BOTTOM

    html = f"""
    <div style="width:100%; height:{SNOTEL_VIEW_HEIGHT}px; overflow:hidden; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.15); margin-top: 16px; background: white;">
      <iframe
        src="{url}"
        style="
          width:100%;
          height:{inner_height}px;
          border:0;
          transform: translateY(-{SNOTEL_CROP_TOP}px);
        "
        loading="lazy"
        referrerpolicy="no-referrer-when-downgrade"
      ></iframe>
    </div>
    """
    return html

# -----------------------------
# 6) CSS
# -----------------------------
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
            
            [data-testid="stMetric"]::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #3b82f6, #8b5cf6, #ec4899);
                border-radius: 20px 20px 0 0;
            }
            
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
            
            [data-testid="stMetricValue"] {
                font-size: 2.75rem !important;
                font-weight: 900 !important;
                background: linear-gradient(135deg, #60a5fa, #a78bfa) !important;
                -webkit-background-clip: text !important;
                -webkit-text-fill-color: transparent !important;
                background-clip: text !important;
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
            /* This targets the containers we will make with st.container(border=True) */
            [data-testid="stVerticalBlockBorderWrapper"] {
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.7), rgba(51, 65, 85, 0.5)) !important;
                border-radius: 20px !important;
                border: 1px solid rgba(59, 130, 246, 0.15) !important;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3) !important;
                backdrop-filter: blur(10px) !important;
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
            
            /* Add subtle animations */
            @keyframes fadeInUp {
                from {
                    opacity: 0;
                    transform: translateY(20px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            
            .block-container > div {
                animation: fadeInUp 0.6s ease-out;
            }
        </style>
    """, unsafe_allow_html=True)

# -----------------------------
# 7) PAGE RENDER
# -----------------------------
load_css()

db = initialize_firebase()
if not db:
    st.stop()

df = load_latest_data()
if df.empty:
    st.markdown("""
        <div class='hero'>
            <h1 class='hero-title'>❄️ Northern Rockies Snow Report</h1>
            <p class='hero-subtitle'>☁️ No data available yet. Check back soon!</p>
        </div>
    """, unsafe_allow_html=True)
    st.stop()

# Load historical data for charts
df_hist = load_historical_data()

# Calculate KPIs
powder_resorts = df[df['is_powder'] == True]
powder_count = len(powder_resorts)
best_snow = df.iloc[0]
best_base = df.loc[df["base_depth"].idxmax()]

def get_nws_snow(forecast_dict):
    if isinstance(forecast_dict, dict):
        return forecast_dict.get('total_snow_inches', 0)
    return 0

df['nws_snow_48h'] = df['nws_forecast'].apply(get_nws_snow)
FORECAST_WATCH_THRESHOLD = 6.0
forecast_resorts = df[df['nws_snow_48h'] >= FORECAST_WATCH_THRESHOLD]
forecast_count = len(forecast_resorts)

# Hero Section
now = datetime.now()
st.markdown(f"""
    <div class='hero'>
        <h1 class='hero-title'>❄️ Northern Rockies Snow Report</h1>
        <p class='hero-subtitle'>Real-time conditions • Live forecasts • Backcountry data</p>
        <p class='hero-subtitle' style='font-size: 0.9rem; margin-top: 0.5rem; opacity: 0.9;'>
            {now.strftime('%A, %B %d, %Y at %I:%M %p')}
        </p>
    </div>
""", unsafe_allow_html=True)

# Powder Alert
if powder_count > 0:
    st.markdown(f"""
        <div class='powder-alert'>
            <div class='powder-alert-title'>🔥 POWDER ALERT!</div>
            <div class='powder-alert-text'>
                <strong>{powder_count}</strong> resorts reporting 6" or more of fresh snow in the last 24 hours
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

# KPI Cards - Using Streamlit metrics
cols = st.columns(4)
with cols[0]:
    st.metric("🏆 Best 24h Snow", f'{best_snow["snow_24h_summit"]:.0f}"', best_snow["display_name"], delta_color="off")
with cols[1]:
    st.metric("📏 Deepest Base", f'{best_base["base_depth"]:.0f}"', best_base["display_name"], delta_color="off")
with cols[2]:
    forecast_subtitle = f"{forecast_count} resorts" if forecast_count > 0 else "No major storms"
    st.metric("⛈️ Forecast Watch (48h)", f"{forecast_count}", forecast_subtitle, delta_color="off")
with cols[3]:
    st.metric("🏔️ Resorts Reporting", f"{len(df)}", "MT & ID", delta_color="off")

st.markdown("<div class='section-header'>📊 Today's Snow Leaderboard</div>", unsafe_allow_html=True)

# Leaderboard
leaderboard_cols = {
    "display_name": "Resort",
    "snow_24h_summit": "24h Snow",
    "base_depth": "Base Depth",
    "summit_depth": "Summit Depth",
    "lifts_open": "Lifts",
    "runs_open": "Runs",
    "conditions_surface": "Surface",
    "last_updated": "Last Updated"
}
leaderboard_df = df[leaderboard_cols.keys()].copy()
leaderboard_df = leaderboard_df.rename(columns=leaderboard_cols)
leaderboard_df.insert(0, "Rank", range(1, len(leaderboard_df) + 1))

st.dataframe(
    leaderboard_df.style.format({
        "24h Snow": '{:.0f}"',
        "Base Depth": '{:.0f}"',
        "Summit Depth": '{:.0f}"',
    }),
    hide_index=True,
    use_container_width=True, # This is the correct parameter
    height=400
)

# --- REWRITTEN 5-DAY CHART ---
# This now uses native st.altair_chart, which will not break.
if not df_hist.empty:
    st.markdown("<div class='section-header'>📈 5-Day Snowfall Trends</div>", unsafe_allow_html=True)
    
    chart_df = prepare_chart_data(df_hist, df)
    
    if not chart_df.empty:
        # Create a single, faceted Altair chart
        chart = alt.Chart(chart_df).mark_bar(
            cornerRadius=4
        ).encode(
            # X-axis: Date
            x=alt.X('day_label', sort=None, title=None, axis=alt.Axis(labelColor='white')),
            
            # Y-axis: Snowfall
            y=alt.Y('snow', title="Snow (in)", axis=alt.Axis(titleColor='white', labelColor='white')),
            
            # Color for the bars
            color=alt.Color('day_label', title="Day", legend=None),
            
            # Tooltip
            tooltip=[
                alt.Tooltip('display_name', title="Resort"),
                alt.Tooltip('day_label', title="Day"),
                alt.Tooltip('snow', title="Snow", format=".0f")
            ],
            
            # Facet by resort
            row=alt.Row(
                'display_name',
                title=None,
                sort=alt.EncodingSortField(field="total_snow", op="sum", order="descending"),
                header=alt.Header(titleColor='white', labelColor='white', labelFontSize=14, labelPadding=10)
            )
        ).properties(
            background='transparent',
            height=60 # Height of each individual chart row
        ).interactive()

        st.altair_chart(chart, use_container_width=True, theme=None)
    else:
        st.info("No historical data available to build 5-day trend chart.")
# --- END REWRITTEN 5-DAY CHART ---


st.divider()

# Forecast Map
st.markdown("<div class='section-header'>🌨️ 48-Hour Snowfall Forecast</div>", unsafe_allow_html=True)
st.caption("💡 Click anywhere on the map for detailed point forecasts. Use the menu (⋮) to switch layers.")

windy_embed = """
<div style="border-radius: 16px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.3);">
    <iframe 
        width="100%" 
        height="550" 
        src="https://embed.windy.com/embed.html?type=map&location=coordinates&metricRain=default&metricTemp=default&metricWind=default&zoom=6&lat=46.4&lon=-113.4&overlay=snowAccu&product=ecmwf&level=surface&legend=true&particles=true" 
        frameborder="0">
    </iframe>
</div>
"""
components.html(windy_embed, height=570, scrolling=False)

st.divider()

# --- REWRITTEN RESORT DETAIL SECTION ---
# This section is now built 100% with native Streamlit components
st.markdown("<div class='section-header'>🔍 Detailed Resort Information</div>", unsafe_allow_html=True)

selected_display_name = st.selectbox(
    "Select a resort:",
    df["display_name"].tolist(),
    index=0,
    label_visibility="collapsed"
)

if selected_display_name:
    r = df[df["display_name"] == selected_display_name].iloc[0]
    
    # --- Native Header Card ---
    with st.container(border=True): # This uses the CSS for .detail-section
        # Row 1: Title and Badge
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"<h2 style='color: white; font-size: 2rem; font-weight: 800; margin: 0;'>{r['display_name']}</h2>", unsafe_allow_html=True)
            st.caption(f"⏱️ Last updated: {r.get('last_updated') or 'N/A'}")
        with c2:
            if r.get("is_powder"):
                st.markdown('<span class="badge badge-powder">🔥 POWDER!</span>', unsafe_allow_html=True)
        
        st.markdown("---") # Simple divider

        # Row 2: Key Metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("24-Hour Snow", f"{r.get('snow_24h_summit', 0):.0f}\"")
        c2.metric("Base Depth", f"{r.get('base_depth', 0):.0f}\"")
        c3.metric("Summit Depth", f"{r.get('summit_depth', 0):.0f}\"")
        c4.metric("Overnight", f"{r.get('snow_overnight', 0):.0f}\"")

    
    # --- Native Two-Column Layout ---
    col_left, col_right = st.columns([1, 1], gap="large")
    
    with col_left:
        # Native Snow & Weather
        with st.container(border=True):
            st.markdown("<h3 style='color: #60a5fa;'>❄️ Snow & Weather</h3>", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            c1.metric("Base Temp", f"{r.get('temp_base', 0):.0f}°F" if pd.notna(r.get('temp_base')) and r.get('temp_base') != 0 else "N/A")
            c2.metric("Summit Temp", f"{r.get('temp_summit', 0):.0f}°F" if pd.notna(r.get('temp_summit')) and r.get('temp_summit') != 0 else "N/A")
            c1.metric("Wind Speed", f"{r.get('wind_speed', 0):.0f} mph" if pd.notna(r.get('wind_speed')) and r.get('wind_speed') != 0 else "N/A")
            c2.metric("Surface", r.get('conditions_surface', 'N/A') or "N/A")
            # `weather_current` is text, st.info is better
            weather = r.get('weather_current', 'N/A') or "N/A"
            if weather != "N/A":
                st.caption(f"Current Weather: **{weather}**")

        # Native Operations
        with st.container(border=True):
            st.markdown("<h3 style='color: #a78bfa;'>🎿 Operations Status</h3>", unsafe_allow_html=True)
            lift_pct, lift_text = parse_open_status(r.get('lifts_open'))
            st.progress(lift_pct, text=f"Lifts: {lift_text}")
            
            run_pct, run_text = parse_open_status(r.get('runs_open'))
            st.progress(run_pct, text=f"Runs: {run_text}")

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
                c2.metric("% Median SWE", f"{s.get('percent_of_median', 'N/A')}%")
                c1.metric("24h SWE Δ", f"{snotel.get('swe', 'N/A')}\"")
                c2.metric("Density", f"{snotel.get('density', 'N/A')}%")
            else:
                st.info("SNOTEL data unavailable")
    
    # Comments section (full width below)
    if r.get("comments"):
        st.markdown("<br>", unsafe_allow_html=True)
        st.info(r['comments'])
    
    # SNOTEL Chart (full width below)
    if triplet and station_name:
        st.markdown("<br>", unsafe_allow_html=True)
        col1, col2 = st.columns([1, 3])
        with col1:
            show_years = st.text_input(
                "Compare year:",
                value="",
                placeholder="e.g. 2023",
                key=f"snotel_{r['resort']}"
            ) or None
        
        # This is a self-contained HTML string, which is fine for components.html
        html = get_snotel_iframe_html(triplet, station_name, show_years)
        components.html(html, height=SNOTEL_VIEW_HEIGHT, scrolling=False)
# --- END REWRITTEN SECTION ---


# Footer
st.markdown("""
    <div class='footer'>
        <strong>Northern Rockies Snow Report</strong><br>
        Data refreshes every 10 minutes • Built with ❤️ for powder seekers<br>
        <span style='font-size: 0.8rem; color: #475569;'>
            Combining resort reports, NWS forecasts, and SNOTEL backcountry data
        </span>
    </div>
""", unsafe_allow_html=True)
