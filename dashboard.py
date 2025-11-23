import os
import ast
import urllib.parse as urlparse
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import altair as alt

import folium
from folium.plugins import Fullscreen
from streamlit_folium import st_folium

import firebase_admin
from firebase_admin import credentials, firestore

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Northern Rockies Snow Report",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

LOCAL_TZ = ZoneInfo("America/Denver")
FRESHNESS_TOLERANCE_HOURS = 18

# ──────────────────────────────────────────────────────────────
# COORDINATES (MASTER LIST)
# ──────────────────────────────────────────────────────────────
RESORTS_DATA = {
    "Snowbowl": {"lat": 47.032417, "lon": -113.9915282},
    "Discovery": {"lat": 46.262206, "lon": -113.246187},
    "Lookout Pass": {"lat": 47.4531005, "lon": -115.706537},
    "Big Mountain": {"lat": 48.502127, "lon": -114.341252},
    "Lost Trail": {"lat": 45.695247, "lon": -113.965263},
    "Teton Pass": {"lat": 47.929804, "lon": -112.816723},
    "Showdown": {"lat": 46.837747, "lon": -110.715599},
    "Blacktail": {"lat": 48.011676, "lon": -114.365251},
    "Bridger Bowl": {"lat": 45.813919, "lon": -110.921873},
    "Big Sky": {"lat": 45.280943, "lon": -111.440644},
    "Red Lodge Mountain": {"lat": 45.181125, "lon": -109.354325},
    "Maverick": {"lat": 45.438286, "lon": -113.142233},
    "Great Divide": {"lat": 46.748900, "lon": -112.328513},
    "Bear Paw": {"lat": 48.162084, "lon": -109.679937},
    "Silver Mountain": {"lat": 47.499070, "lon": -116.119163},
    "Turner Mountain": {"lat": 48.609788, "lon": -115.648756},
    "Schweitzer": {"lat": 48.377785, "lon": -116.633436},
}

# ──────────────────────────────────────────────────────────────
# CSS STYLING
# ──────────────────────────────────────────────────────────────
def load_css():
    st.markdown("""
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
            
            html, body, [class*="css"] {
                font-family: 'Inter', sans-serif;
            }
            
            /* FORCE DARK BACKGROUND GLOBALLY */
            .stApp {
                background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%);
                background-attachment: fixed;
                color: #e2e8f0;
            }
            
            .block-container {
                padding: 2rem 3rem !important;
                max-width: 1800px;
            }
            
            h1, h2, h3, p, li, span, label, div {
                color: #e2e8f0;
            }

            /* Hero Section */
            .hero {
                background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 50%, #ec4899 100%);
                padding: 2.5rem;
                border-radius: 24px;
                margin-bottom: 2rem;
                box-shadow: 0 20px 60px rgba(59, 130, 246, 0.4);
                text-align: center;
            }
            .hero-title {
                font-size: 3rem !important;
                font-weight: 900 !important;
                color: white !important;
                margin: 0 !important;
                text-shadow: 0 2px 10px rgba(0,0,0,0.3);
            }
            .hero-subtitle {
                color: rgba(255, 255, 255, 0.95) !important;
                font-size: 1.1rem;
                margin-top: 0.5rem;
            }

            /* Powder Alert */
            .powder-alert {
                background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
                padding: 1.5rem;
                border-radius: 20px;
                margin-bottom: 2rem;
                box-shadow: 0 12px 40px rgba(220, 38, 38, 0.5);
                border: 2px solid rgba(255, 255, 255, 0.2);
                animation: pulse-glow 2s ease-in-out infinite;
            }
            .powder-alert-title { font-size: 1.5rem; font-weight: 900; color: white !important; }
            .powder-alert-text { color: rgba(255,255,255,0.95) !important; font-size: 1.1rem; }
            
            @keyframes pulse-glow {
                0%, 100% { box-shadow: 0 12px 40px rgba(220, 38, 38, 0.5); transform: scale(1); }
                50% { box-shadow: 0 12px 60px rgba(220, 38, 38, 0.7); transform: scale(1.01); }
            }

            /* Section Headers */
            .section-header {
                font-size: 1.75rem;
                font-weight: 800;
                color: white !important;
                margin: 2.5rem 0 1.5rem 0;
                padding: 1rem 1.5rem;
                background: linear-gradient(135deg, rgba(59, 130, 246, 0.15), rgba(139, 92, 246, 0.1));
                border-left: 4px solid #3b82f6;
                border-radius: 12px;
            }
            
            /* Custom Table Styling */
            .styled-table {
                width: 100%;
                border-collapse: collapse;
                margin: 25px 0;
                font-size: 0.8rem;
                font-family: 'Inter', sans-serif;
                box-shadow: 0 0 20px rgba(0, 0, 0, 0.15);
                border-radius: 12px 12px 0 0;
                overflow: hidden;
            }
            .styled-table th, .styled-table td {
                padding: 10px 15px;
                text-align: center;
            }
            .styled-table td:first-child, .styled-table th:first-child {
                text-align: left;
            }

            .styled-table thead tr {
                background-color: #3b82f6;
                color: #ffffff;
            }
            .styled-table thead th {
                background-color: rgba(59, 130, 246, 0.3);
                color: #e2e8f0;
                text-transform: uppercase;
                font-weight: 700;
                letter-spacing: 0.05em;
                border-bottom: 2px solid #475569;
            }
            
            .styled-table tbody tr {
                border-bottom: 1px solid #334155;
                background-color: rgba(30, 41, 59, 0.4);
                color: #f1f5f9;
            }
            .styled-table tbody tr:nth-of-type(even) {
                background-color: rgba(15, 23, 42, 0.4);
            }
            .styled-table tbody tr:hover {
                background-color: rgba(59, 130, 246, 0.2);
                color: white;
            }

            /* Data Grid Styling */
            .data-grid {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 10px;
                margin-bottom: 15px;
            }
            .data-item {
                background: rgba(255, 255, 255, 0.05);
                padding: 12px;
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            .data-label {
                font-size: 0.7rem;
                color: #94a3b8;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 4px;
            }
            .data-value {
                font-size: 1.1rem;
                font-weight: 700;
                color: #ffffff;
            }
            .data-sub {
                font-size: 0.8rem;
                color: #cbd5e1;
                margin-top: 2px;
            }
            .full-width {
                grid-column: span 2;
            }

            /* Metric Cards */
            [data-testid="stMetric"] {
                background-color: rgba(30, 41, 59, 0.6) !important;
                border-radius: 12px !important;
                padding: 1rem !important;
                border: 1px solid rgba(255,255,255,0.1) !important;
            }
            [data-testid="stMetricLabel"] { color: #94a3b8 !important; }
            [data-testid="stMetricValue"] { color: white !important; }
            
            /* LARGER TABS CSS */
            .stTabs [data-baseweb="tab-list"] button {
                font-size: 1.3rem !important;
                font-weight: 900 !important;
                padding-top: 10px !important;
                padding-bottom: 10px !important;
            }
        </style>
    """, unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────
# CHART HELPER
# ──────────────────────────────────────────────────────────────
def get_snotel_iframe_html(triplet: str, station_name: str, show_years: str | None) -> str:
    """Return HTML snippet for NRCS SNOTEL graph."""
    try:
        state = triplet.split(":")[1].strip().upper()
    except Exception:
        state = ""
    name_enc = urlparse.quote(station_name)
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/WTEQ/{state}/{name_enc}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years.strip()}")
    url = base + "?" + "&".join(params)
    
    # CROP SETTINGS
    SNOTEL_CROP_TOP = 270    
    SNOTEL_VIEW_HEIGHT = 440 
    SNOTEL_CROP_BOTTOM = 480
    
    inner_height = SNOTEL_VIEW_HEIGHT + SNOTEL_CROP_TOP + SNOTEL_CROP_BOTTOM
    
    return f"""
    <div style="width:100%; height:{SNOTEL_VIEW_HEIGHT}px; overflow:hidden; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.15); margin-top: 16px; background: white;">
      <iframe
        src="{url}"
        style="width:100%; height:{inner_height}px; border:0; transform: translateY(-{SNOTEL_CROP_TOP}px);"
        loading="lazy"
      ></iframe>
    </div>
    """

# ──────────────────────────────────────────────────────────────
# MODAL (DIALOG) LOGIC
# ──────────────────────────────────────────────────────────────
@st.dialog("Resort Details", width="large")
def show_resort_modal(row):
    # TIGHT HEADER LAYOUT
    st.markdown(f"""
        <div style="line-height: 1.1; margin-bottom: 40px;">
            <div style="font-size: 2.2rem; font-weight: 900; color: white; line-height: 1.0;">{row['display_name']}</div>
            <div style="font-size: 0.9rem; color: #94a3b8; margin-top: 4px;">Last Updated: {row['last_updated']}</div>
        </div>
    """, unsafe_allow_html=True)

    nws = row.get('nws_forecast', {})
    snotel = row.get('snotel_data', {})

    tab1, tab2, tab3 = st.tabs(["🎿 CONDITIONS", "🏔️ SNOTEL", "🌦️ FORECAST"])

    with tab1:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Base Depth", f"{row['base_depth']:.0f}\"")
        c2.metric("Summit", f"{row['summit_depth']:.0f}\"")
        c3.metric("Overnight", f"{row['snow_overnight']:.0f}\"")
        
        f_wind = f"{row.get('wind_speed', 0):.0f} mph" if pd.notna(row.get('wind_speed')) else "N/A"
        c4.metric("Wind", f_wind)
        
        st.divider()
        
        details_to_show = []
        def is_valid(val):
            if val is None: return False
            s = str(val).strip().lower()
            return s not in ["", "n/a", "none", "0", "null"]

        if is_valid(row.get('lifts_open')): details_to_show.append(f"**Lifts Open:** {row['lifts_open']}")
        if is_valid(row.get('runs_open')): details_to_show.append(f"**Runs Open:** {row['runs_open']}")
        if is_valid(row.get('conditions_surface')): details_to_show.append(f"**Surface:** {row['conditions_surface']}")
            
        if details_to_show:
            for det in details_to_show: st.markdown(det)
        else:
            st.caption("No operational details reported.")
        
        if row.get('comments'):
            st.info(f"📝 {row['comments']}")

    with tab2:
        s_name = snotel.get('station_name', 'Station N/A')
        if "snotel" not in s_name.lower(): s_name += " SNOTEL"
        
        elev = snotel.get('elevation', '')
        if elev: s_name += f" ({elev} ft)"
        
        val_obs_raw = snotel.get('latest_observation', 'N/A')
        val_obs_display = val_obs_raw
        try:
            if len(val_obs_raw) > 10:
                dt_obs = datetime.strptime(val_obs_raw, "%Y-%m-%d %H:%M")
                val_obs_display = dt_obs.strftime("%b %d, %I:%M %p")
            else:
                dt_obs = datetime.strptime(val_obs_raw, "%Y-%m-%d")
                val_obs_display = dt_obs.strftime("%b %d, %Y")
        except: pass
            
        st.markdown(f"""
        <div style="line-height: 1.2; margin-bottom: 15px;">
            <div style="font-size: 1.5rem; font-weight: 700; color: white; line-height: 1.0;">{s_name}</div>
            <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;">Observed: {val_obs_display}</div>
        </div>
        """, unsafe_allow_html=True)
        
        if snotel.get('unavailable') is True:
            st.error(f"Data Unavailable: {snotel.get('error_reason', 'Unknown error')}")
        
        val_snow = snotel.get('snow_depth', 'N/A')
        if isinstance(val_snow, (int, float)): val_snow = f"{val_snow}\""
            
        val_swe = snotel.get('swe', 'N/A')
        if isinstance(val_swe, (int, float)): val_swe = f"{val_swe}\""

        val_density = snotel.get('density', 'N/A')
        val_qual = snotel.get('snow_category', 'N/A')
        
        pct_raw = snotel.get('percent_of_median', 'N/A')
        if str(pct_raw) == "N/A" or pct_raw is None:
            val_pct = "N/A"
        else:
            val_pct = f"{pct_raw}%"
        
        if val_density == "N/A":
             density_display = "N/A"
        else:
             density_display = f"{val_density}<div class='data-sub'>{val_qual}</div>"

        st.markdown(f"""
        <div class="data-grid">
            <div class="data-item">
                <div class="data-label">24h Snow</div>
                <div class="data-value">{val_snow}</div>
            </div>
            <div class="data-item">
                <div class="data-label">24h SWE</div>
                <div class="data-value">{val_swe}</div>
            </div>
            <div class="data-item">
                <div class="data-label">Density</div>
                <div class="data-value">{density_display}</div>
            </div>
            <div class="data-item">
                <div class="data-label">Median %</div>
                <div class="data-value">{val_pct}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        triplet = snotel.get("triplet")
        station_name = snotel.get("station_name")
        
        if triplet and station_name:
            col_input, col_label = st.columns([1, 2])
            with col_input:
                compare_year = st.text_input("Compare Year:", placeholder="e.g. 2011", key=f"year_{row['display_name']}")
            with col_label:
                st.write("") 

            html = get_snotel_iframe_html(triplet, station_name, compare_year)
            components.html(html, height=440, scrolling=False)
        else:
            st.info("Chart unavailable (Missing triplet ID)")

    with tab3:
        st.markdown("### 48-Hour Weather Outlook")
        
        f_precip = f"{nws.get('total_precip_inches', 'N/A')}\""
        f_prob = f"{nws.get('precip_probability_max', 'N/A')}%"
        f_snow = f"{nws.get('total_snow_inches', 'N/A')}\""
        f_level = f"{nws.get('snow_level_feet', 'N/A')} ft"
        f_high = f"{nws.get('temp_high_f', 'N/A')}°F"
        f_low = f"{nws.get('temp_low_f', 'N/A')}°F"
        f_cond = nws.get('conditions', 'N/A')
        
        st.markdown(f"""
        <div class="data-grid">
            <div class="data-item">
                <div class="data-label">Temperatures</div>
                <div class="data-value">{f_high} / {f_low}</div>
            </div>
            <div class="data-item">
                <div class="data-label">Precip Chance</div>
                <div class="data-value">{f_prob}</div>
            </div>
            <div class="data-item">
                <div class="data-label">Snow Forecast</div>
                <div class="data-value">{f_snow}</div>
                <div class="data-sub">Level: {f_level}</div>
            </div>
            <div class="data-item">
                <div class="data-label">Total Precip</div>
                <div class="data-value">{f_precip}</div>
            </div>
            <div class="data-item full-width">
                <div class="data-label">Short Forecast</div>
                <div class="data-value" style="font-size: 1rem;">{f_cond}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────
# DATA HELPERS
# ──────────────────────────────────────────────────────────────
@st.cache_resource
def initialize_firebase():
    try:
        cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        if cred_path:
            if not os.path.exists(cred_path): return None
            cred = credentials.Certificate(cred_path)
        else:
            creds = st.secrets["firebase_service_account"]
            if isinstance(creds, str): creds_dict = ast.literal_eval(creds)
            else: creds_dict = dict(creds)
            cred = credentials.Certificate(creds_dict)
        try: firebase_admin.get_app()
        except ValueError: firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception: return None

def get_display_name(resort_name: str) -> str:
    name_map = {
        "LookoutPass": "Lookout Pass", "BigMountain": "Big Mountain", "LostTrail": "Lost Trail",
        "TetonPass": "Teton Pass", "Blacktail": "Blacktail", "Snowbowl": "Snowbowl",
        "Discovery": "Discovery", "Showdown": "Showdown", "BridgerBowl": "Bridger Bowl",
        "BigSky": "Big Sky", "RedLodge": "Red Lodge Mountain", "RedLodgeMountain": "Red Lodge Mountain",
        "GreatDivide": "Great Divide", "BearPaw": "Bear Paw", "SilverMountain": "Silver Mountain",
        "Schweitzer": "Schweitzer", "TurnerMountain": "Turner Mountain"
    }
    return name_map.get(resort_name, resort_name)

@st.cache_data(ttl=600)
def load_latest_data(_db):
    master_rows = [{"display_name": k, "lat": v["lat"], "lon": v["lon"]} for k, v in RESORTS_DATA.items()]
    df_master = pd.DataFrame(master_rows)

    if _db is None:
        for col in ["snow_24h_summit", "base_depth"]: df_master[col] = 0
        return df_master

    try:
        latest_q = _db.collection("snow_reports").order_by("date", direction=firestore.Query.DESCENDING).limit(1).stream()
        latest = [d.to_dict() for d in latest_q]
        df_final = df_master.copy()

        if latest:
            latest_date = latest[0]["date"]
            docs = _db.collection("snow_reports").where(filter=firestore.FieldFilter("date", "==", latest_date)).stream()
            rows = [d.to_dict() for d in docs]
            if rows:
                df_fb = pd.DataFrame(rows)
                if "resort" in df_fb.columns:
                    df_fb["display_name"] = df_fb["resort"].apply(get_display_name)
                    df_final = pd.merge(df_master, df_fb, on="display_name", how="left")
        
        num_cols = ["snow_24h_summit", "base_depth", "summit_depth", "snow_overnight", "temp_base", "temp_summit", "wind_speed"]
        for c in num_cols:
            if c not in df_final.columns: df_final[c] = 0
            df_final[c] = pd.to_numeric(df_final[c], errors="coerce").fillna(0)
            
        for c in ["nws_forecast", "snotel_data"]:
            if c not in df_final.columns: df_final[c] = [{}] * len(df_final)
            df_final[c] = df_final[c].apply(lambda x: x if isinstance(x, dict) else {})

        str_cols = ["lifts_open", "runs_open", "conditions_surface", "last_updated", "comments"]
        for c in str_cols:
            if c not in df_final.columns: df_final[c] = "N/A"
            df_final[c] = df_final[c].fillna("N/A").astype(str)

        df_final["last_updated_dt"] = pd.to_datetime(df_final["last_updated"], errors="coerce").apply(
            lambda x: x.replace(tzinfo=LOCAL_TZ) if pd.notna(x) and x.tzinfo is None else x
        )
        today = datetime.now(LOCAL_TZ).date()
        season_year = today.year if today.month >= 10 else today.year - 1
        season_start = pd.Timestamp(season_year, 10, 1, tz=LOCAL_TZ)
        
        # --- FRESHNESS LOGIC FIX (CALENDAR DAY CHECK) ---
        now_dt = datetime.now(LOCAL_TZ)
        
        is_report_today = df_final['last_updated_dt'].dt.date == now_dt.date()
        is_season = df_final['last_updated_dt'] >= season_start
        
        # If it's this season AND the report date is NOT today's date, zero out fresh snow.
        mask_stale_snow = is_season & ~is_report_today
        df_final.loc[mask_stale_snow, ["snow_24h_summit", "snow_overnight"]] = 0
        
        df_final["is_powder"] = (df_final["snow_24h_summit"] >= 6)
        
        return df_final.sort_values(["snow_24h_summit", "display_name"], ascending=[False, True]).reset_index(drop=True)

    except Exception as e:
        st.error(f"Data error: {e}")
        return df_master

@st.cache_data(ttl=600)
def load_historical_data(_db, days=5):
    if _db is None: return pd.DataFrame()
    try:
        dates = [(datetime.now(LOCAL_TZ).date() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        all_rows = []
        for d in dates:
            docs = _db.collection("snow_reports").where(filter=firestore.FieldFilter("date", "==", d)).stream()
            for doc in docs:
                r = doc.to_dict()
                r["query_date"] = d
                all_rows.append(r)
        df = pd.DataFrame(all_rows)
        if df.empty: return df
        
        df["query_date"] = pd.to_datetime(df["query_date"], errors="coerce").dt.tz_localize(LOCAL_TZ)
        df["last_updated_dt"] = pd.to_datetime(df["last_updated"], errors="coerce").apply(
            lambda x: x.replace(tzinfo=LOCAL_TZ) if pd.notna(x) and x.tzinfo is None else 
            (x.astimezone(LOCAL_TZ) if pd.notna(x) and x.tzinfo is not None else x)
        )
        return df
    except Exception: return pd.DataFrame()

def prepare_chart_data(df_hist, df_current):
    if df_hist.empty or df_current.empty: return pd.DataFrame()
    resorts = df_current["display_name"].unique().tolist()
    rows = []
    today = datetime.now(LOCAL_TZ).date()
    days = [(today - timedelta(days=i)) for i in range(4, -1, -1)]

    df_hist["temp_disp"] = df_hist["resort"].apply(get_display_name)

    for r_display in resorts:
        subset = df_hist[df_hist["temp_disp"] == r_display]
        for d in days:
            qd = pd.Timestamp(d, tz=LOCAL_TZ)
            # Filter history by the date the data was SCRAPED (query_date)
            row = subset[subset["query_date"].dt.date == d]
            snow = 0.0
            if not row.empty:
                # NEW: Look for summit, fallback to base if summit is missing/zero
                val_summit = float(row.iloc[0].get("snow_24h_summit", 0) or 0)
                val_base = float(row.iloc[0].get("snow_24h_base", 0) or 0)
                
                raw = val_summit if val_summit > 0 else val_base
                
                # FIX: ONLY count snow if the report's internal LAST_UPDATED date matches the bar date (d)
                report_day = row.iloc[0]["last_updated_dt"].date()
                if raw > 0 and report_day == d:
                    snow = raw
            rows.append({"display_name": r_display, "date": d, "snow": snow})
    
    df = pd.DataFrame(rows)
    if df.empty: return pd.DataFrame()
    totals = df.groupby("display_name", as_index=False)["snow"].sum().rename(columns={"snow": "total_snow"})
    return df.merge(totals, on="display_name")

# ──────────────────────────────────────────────────────────────
# MAP FUNCTION
# ──────────────────────────────────────────────────────────────
def create_map(df):
    m = folium.Map(
        location=[46.8, -113.5], zoom_start=7,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr='Tiles &copy; Esri'
    )
    Fullscreen().add_to(m)

    for _, row in df.iterrows():
        if pd.isna(row['lat']) or pd.isna(row['lon']): continue
        
        snow_val = float(row['snow_24h_summit'])
        has_data = row['last_updated'] != "N/A"
        
        if not has_data:
            display_str = "n/a"
            bg_color = "rgba(100, 116, 139, 0.8)"
            text_color = "#e2e8f0"
            border = "2px solid #475569"
            is_powder = False
        else:
            display_str = f"{int(snow_val)}\"" if snow_val.is_integer() else f"{snow_val:.1f}\""
            if snow_val == 0: display_str = "0\""
            is_powder = snow_val >= 6
            bg_color = "rgba(220, 38, 38, 0.9)" if is_powder else "rgba(255, 255, 255, 0.95)"
            text_color = "white" if is_powder else "#1e293b"
            border = "2px solid #b91c1c" if is_powder else "2px solid #3b82f6"
        
        html_icon = f"""
        <div style="position: absolute; transform: translate(-50%, -50%); display: flex; flex-direction: column; align-items: center; justify-content: center; width: 120px;">
            <div style="background: {bg_color}; color: {text_color}; border: {border}; border-radius: 50%; width: 42px; height: 42px; display: flex; align-items: center; justify-content: center; font-family: sans-serif; font-weight: 900; font-size: 15px; box-shadow: 0 4px 8px rgba(0,0,0,0.3); margin-bottom: 4px;">
                {display_str}
            </div>
            <div style="background: rgba(255,255,255,0.9); padding: 2px 6px; border-radius: 8px; font-family: sans-serif; font-size: 11px; font-weight: 700; color: black; box-shadow: 0 2px 4px rgba(0,0,0,0.2); white-space: nowrap;">
                {row['display_name']}
            </div>
        </div>
        """
        # Tooltip is the ID we use to trigger the Modal
        folium.Marker(
            location=[row['lat'], row['lon']],
            icon=folium.DivIcon(html=html_icon),
            tooltip=row['display_name']
        ).add_to(m)
    return m

# ──────────────────────────────────────────────────────────────
# MAIN APP
# ──────────────────────────────────────────────────────────────
load_css()
db = initialize_firebase()
df = load_latest_data(db)
df_hist = load_historical_data(db, days=5)

now_local = datetime.now(LOCAL_TZ)
st.markdown(f"""
    <div class='hero'>
        <h1 class='hero-title'>❄️ Northern Rockies Snow Report</h1>
        <p class='hero-subtitle'>{now_local.strftime('%A, %B %d, %Y at %I:%M %p %Z')}</p>
    </div>
""", unsafe_allow_html=True)

# 1. Powder Alert
powder_resorts = df[df['is_powder'] == True]
powder_count = len(powder_resorts)
if powder_count > 0:
    st.markdown(f"""
        <div class='powder-alert'>
            <div class='powder-alert-title'>🔥 POWDER ALERT!</div>
            <div class='powder-alert-text'>
                <strong>{powder_count}</strong> {'resort is' if powder_count == 1 else 'resorts are'} reporting 6" or more fresh snow!
            </div>
        </div>
    """, unsafe_allow_html=True)
    cols_per_row = 4
    for i in range(0, powder_count, cols_per_row):
        cols = st.columns(cols_per_row)
        batch = powder_resorts.iloc[i:i+cols_per_row]
        for idx, (_, resort) in enumerate(batch.iterrows()):
            with cols[idx]:
                st.metric(label=resort['display_name'], value=f"{resort['snow_24h_summit']:.0f}\"", delta="POWDER")

# 2. Leaderboard
st.markdown("<div class='section-header'>📊 Today's Snow Leaderboard</div>", unsafe_allow_html=True)
cols_map = {"display_name": "Resort", "snow_24h_summit": "24h Snow", "base_depth": "Base Depth", "summit_depth": "Summit Depth", "lifts_open": "Lifts", "runs_open": "Runs", "conditions_surface": "Surface", "last_updated": "Last Updated"}
df_ld = df[df['last_updated'] != "N/A"][[k for k in cols_map.keys() if k in df.columns]].rename(columns=cols_map)
for c in ["24h Snow", "Base Depth", "Summit Depth"]:
    if c in df_ld.columns: df_ld[c] = df_ld[c].apply(lambda x: f'{x:.0f}"')
st.markdown(df_ld.to_html(classes="styled-table", index=False, border=0), unsafe_allow_html=True)

# 3. Chart
st.markdown("<div class='section-header'>📈 5-Day Snowfall Trends</div>", unsafe_allow_html=True)
cdf = prepare_chart_data(df_hist, df)
if cdf.empty or cdf["snow"].sum() == 0: st.info("❄️ No 5-day snowfall data available.")
else:
    max_snow = cdf.groupby("display_name")["snow"].sum().max()
    sorted_names = cdf.groupby("display_name")["total_snow"].max().sort_values(ascending=False).index.tolist()
    days_order = [(datetime.now(LOCAL_TZ).date() - timedelta(days=i)).strftime("%a %m/%d") for i in range(4, -1, -1)]
    cdf["day_label"] = pd.to_datetime(cdf["date"]).dt.strftime("%a %m/%d")
    
    base = alt.Chart(cdf).encode(
        y=alt.Y("display_name:N", sort=sorted_names, title=None, axis=alt.Axis(labelColor="white", labelFontSize=14)),
    )
    
    # Adding Zero Line (Vertical Line at X=0)
    zero_line = alt.Chart(pd.DataFrame({'zero': [0]})).mark_rule(color='white', size=2).encode(
        x=alt.X('zero:Q')
    )
    
    bars = base.mark_bar().encode(
        x=alt.X("snow:Q", title="Snow (in)", 
                axis=alt.Axis(labelColor="white", grid=False, tickMinStep=1, format='d', 
                              domain=True, domainColor="white", domainWidth=2),
                scale=alt.Scale(domain=[0, max_snow * 1.2])),
        color=alt.Color("day_label:N", title=None, sort=days_order, legend=alt.Legend(labelColor="white", titleColor="white", orient="top", title=None), scale=alt.Scale(range=["#cbd5e1", "#38bdf8", "#a78bfa", "#14b8a6", "#1e40af"])),
        tooltip=["display_name", "day_label", "snow"]
    )
    
    text = base.transform_stack(
        stack='snow',
        groupby=['display_name'],
        sort=[alt.SortField('day_label', order='ascending')],
        as_=['stack_start', 'stack_end']
    ).transform_calculate(
        midpoint="(datum.stack_start + datum.stack_end) / 2"
    ).mark_text(color='black', fontWeight='bold').encode(
        x=alt.X('midpoint:Q'),
        text=alt.Text("snow:Q", format=".0f"),
        order=alt.Order("day_label:N", sort="ascending"),
        opacity=alt.condition(alt.datum.snow > 0, alt.value(1), alt.value(0))
    )
    
    # CALCULATE TOTALS AND ADD CUSTOM LABEL FIELD
    totals = cdf.groupby("display_name", as_index=False)["total_snow"].max()
    
    # --- FINAL FIX HERE ---
    totals['total_label'] = totals['total_snow'].apply(lambda x: f"{x:.0f}\" Total")
    # ----------------------
    
    total_text = alt.Chart(totals).mark_text(align='left', dx=5, color='white', fontWeight='bold').encode(
        y=alt.Y("display_name:N", sort=sorted_names),
        x=alt.X("total_snow:Q"),
        text=alt.Text("total_label:N"), # Use the new custom column
    )

    st.altair_chart(alt.layer(bars, text, total_text, zero_line), width="stretch")

# 4. Map (With Session State Check)
st.markdown("<div class='section-header'>🗺️ Live Snow Map (Click for Details)</div>", unsafe_allow_html=True)
m = create_map(df)
map_output = st_folium(m, width="100%", height=700, return_on_hover=False)

if map_output and map_output.get("last_object_clicked_tooltip"):
    resort_name = map_output["last_object_clicked_tooltip"]
    
    if "last_clicked" not in st.session_state or st.session_state["last_clicked"] != resort_name:
        st.session_state["last_clicked"] = resort_name
        selected_row = df[df["display_name"] == resort_name]
        if not selected_row.empty:
            show_resort_modal(selected_row.iloc[0])
    else:
        # Logic for same-click: Force state reset for the next run.
        st.session_state["last_clicked"] = None
        st.rerun()

# 6. Windy
st.markdown("<div class='section-header'>🌨️ Regional Forecast Model</div>", unsafe_allow_html=True)
components.html(
    """<div style="border-radius: 16px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.3);">
      <iframe width="100%" height="550" src="https://embed.windy.com/embed.html?type=map&zoom=6&lat=46.4&lon=-113.4&overlay=snowAccu&product=ecmwf" frameborder="0"></iframe>
    </div>""", height=570
)

st.markdown("<br><div style='text-align: center; color: #94a3b8;'>Northern Rockies Snow Report • Built with 💙</div>", unsafe_allow_html=True)
