import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import os
import streamlit.components.v1 as components
import urllib.parse
import requests
import shutil
from pathlib import Path

# =========================
# 0) OPTIONAL AUTO-BACKUP (comment out if you don't want automatic backups on run)
# =========================
try:
    _src = Path(__file__)
    _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _backup = _src.with_name(f"{_src.stem}_{_ts}{_src.suffix}")
    shutil.copy2(_src, _backup)
except Exception:
    # Stay silent; don't block the app if backup fails.
    pass

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Northern Rockies Snow Report",
    page_icon="❄️",
    layout="wide"
)

# Global Firebase client
db = None

# --- 2. FIREBASE CONNECTION ---
@st.cache_resource
def initialize_firebase():
    """Initialize Firebase Admin SDK."""
    global db

    firebase_cred_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if not firebase_cred_path:
        st.error("FIREBASE_SERVICE_ACCOUNT environment variable not set")
        return None

    if not os.path.exists(firebase_cred_path):
        st.error(f"Firebase credentials file not found: {firebase_cred_path}")
        return None

    try:
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(firebase_cred_path)
            firebase_admin.initialize_app(cred)

        db = firestore.client()
        return db
    except Exception as e:
        st.error(f"Error initializing Firebase: {e}")
        return None

# --- 3. DATA LOADING FUNCTION ---
@st.cache_data(ttl=600)
def load_latest_data():
    """
    Fetch the latest snow report for all resorts by querying for today's date.
    """
    if db is None:
        st.error("Firebase is not connected.")
        return pd.DataFrame()

    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        collection_ref = db.collection('snow_reports')
        query = collection_ref.where(field_path='date', op_string='==', value=today_str)
        docs = query.stream()

        rows = [d.to_dict() for d in docs]
        if not rows:
            st.warning(f"No data found for today ({today_str}) yet. Please wait for the scraper to run.")
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # --- Data Cleaning ---
        num_cols = [
            'snow_24h_summit', 'snow_24h_base', 'base_depth', 'summit_depth',
            'snow_overnight', 'temp_base', 'temp_summit', 'wind_speed'
        ]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        obj_cols = [
            'resort', 'last_updated', 'lifts_open', 'runs_open',
            'conditions_surface', 'weather_current', 'comments'
        ]
        for col in obj_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).fillna('')
                df[col] = df[col].replace(['nan', 'None', 'N/A'], '', regex=False)

        # timestamps
        if 'last_updated' in df.columns:
            df['last_updated_dt'] = pd.to_datetime(df['last_updated'], errors='coerce')
        else:
            df['last_updated_dt'] = pd.NaT

        # dict-like fields that may be stored as strings
        def to_dict_safe(v):
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    # safe-ish literal eval alternative: look for JSON first
                    import json
                    return json.loads(v)
                except Exception:
                    try:
                        # fall back to eval as original logic intended
                        return eval(v)
                    except Exception:
                        return None
            return None

        if 'nws_forecast' in df.columns:
            df['nws_forecast'] = df['nws_forecast'].apply(to_dict_safe)
        if 'snotel_data' in df.columns:
            df['snotel_data'] = df['snotel_data'].apply(to_dict_safe)

        today = datetime.now().date()
        df['is_current'] = df['last_updated_dt'].dt.date == today
        df['is_powder'] = (df.get('snow_24h_summit', pd.Series(0)) >= 6) & (df['is_current'])

        return df

    except Exception as e:
        st.error(f"Error loading data from Firestore: {e}")
        return pd.DataFrame()

# --- 4. SNOTEL CHARTING (IFRAME EMBED, RELIABLE) ---

# Map known naming differences -> official SNOTEL station page names
SNOTEL_NAME_FIXES = {
    "Lookout": "Lookout Pass",
    # add more as needed
}

def snotel_chart_url(triplet: str, station_name: str, chart_type: str = "WTEQ", show_years: str | None = None) -> str:
    """
    Build the official SNOTEL chart URL with 'hide' params so it renders as just the chart.
    chart_type examples: WTEQ, PCTMED, SNWD, PREC, TOBS, etc.
    """
    state = triplet.split(':')[1].strip().upper()
    fixed_name = SNOTEL_NAME_FIXES.get(station_name, station_name)
    name_enc = urllib.parse.quote(fixed_name)
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/{chart_type}/{state}/{name_enc}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years}")
    return base + "?" + "&".join(params)

def render_snotel_iframe(triplet: str, station_name: str, chart_type: str, title: str, show_years: str | None = None, height: int = 540):
    url = snotel_chart_url(triplet, station_name, chart_type, show_years)
    st.caption(title)
    components.html(
        f'<iframe src="{url}" width="100%" height="{height}" style="border:0;" loading="lazy"></iframe>',
        height=height + 16,
        scrolling=False
    )

# --- 5. BUILD THE DASHBOARD PAGE ---

st.title("❄️ Northern Rockies Snow Report")

db = initialize_firebase()

if db:
    df = load_latest_data()

    if not df.empty:
        # --- AT-A-GLANCE METRICS ---
        powder_count = int(df['is_powder'].sum())
        best_snow = df.loc[df['snow_24h_summit'].idxmax()]
        best_base = df.loc[df['base_depth'].idxmax()]

        if powder_count > 0:
            st.success(f"**POWDER ALERT: {powder_count} resort(s) reported 6\" or more new snow today!**")

        cols = st.columns(3)
        cols[0].metric("Today's Best Snow", f"{best_snow['snow_24h_summit']:.0f}\"", f"{best_snow['resort']}")
        cols[1].metric("Deepest Base", f"{best_base['base_depth']:.0f}\"", f"{best_base['resort']}")
        last_dt = df['last_updated_dt'].max()
        cols[2].metric("Last Update", last_dt.strftime('%I:%M %p') if pd.notna(last_dt) else "—")

        st.divider()

        # --- RESORT-SPECIFIC DRILL-DOWN ---
        st.header("Resort-Specific Drill-Down")

        sorted_by_snow = df.sort_values('snow_24h_summit', ascending=False)
        selected_resort = st.selectbox(
            "Select a resort for detailed info:",
            sorted_by_snow['resort']
        )

        if selected_resort:
            resort_data = df[df['resort'] == selected_resort].iloc[0]

            st.subheader(f"{resort_data['resort']}")
            st.caption(f"Last updated: {resort_data['last_updated']}")

            col1, col2, col3 = st.columns(3)
            col1.metric("24-Hour Snow", f"{resort_data['snow_24h_summit']:.0f}\"")
            col2.metric("Base Depth", f"{resort_data['base_depth']:.0f}\"")
            col3.metric("Summit Depth", f"{resort_data['summit_depth']:.0f}\"")

            st.write("")

            col1, col2 = st.columns([2, 1])

            with col1:
                # Full data table
                st.write("**Full Report Data**")
                details_cols = [
                    'snow_overnight', 'snow_24h_base', 'snow_24h_summit',
                    'base_depth', 'summit_depth',
                    'temp_base', 'temp_summit',
                    'wind_speed', 'lifts_open', 'runs_open',
                    'conditions_surface', 'weather_current'
                ]
                details_cols = [c for c in details_cols if c in resort_data.index]
                details = resort_data[details_cols].rename(lambda x: x.replace('_', ' ').title())

                st.dataframe(details.astype(str), width='stretch')

                # NWS Box
                st.write("**48-Hour NWS Outlook**")
                nws_data = resort_data.get('nws_forecast')

                if nws_data:
                    with st.container(border=True):
                        st.metric("Est. Snowfall", f"{nws_data.get('total_snow_inches', 0):.1f}\"")
                        st.text(f"Temp: {nws_data.get('temp_low_f', 'N/A')}°F - {nws_data.get('temp_high_f', 'N/A')}°F")
                        st.text(f"Precip: {nws_data.get('precip_probability_max', 0)}% chance")
                else:
                    st.info("NWS data not available for this resort.")

            with col2:
                # SNOTEL Box
                st.write("**SNOTEL Data**")
                snotel_data = resort_data.get('snotel_data')

                if snotel_data and snotel_data.get('triplet') and snotel_data.get('station_name'):
                    try:
                        # Display quick stats
                        with st.container(border=True):
                            st.write(f"**{snotel_data.get('station_name')}**")
                            st.metric("24-hr Snow Δ", f"{snotel_data.get('snow_depth', 0)}\"")
                            st.text(f"Density: {snotel_data.get('density', 'N/A')}%")
                            st.text(f"Quality: {snotel_data.get('snow_category', 'N/A')}")

                        # --- EMBED OFFICIAL CHARTS VIA IFRAME ---
                        render_snotel_iframe(
                            snotel_data['triplet'],
                            snotel_data['station_name'],
                            chart_type="WTEQ",
                            title="Snow Water Equivalent (SWE) & Depth",
                            show_years=None  # e.g., '2026' to force a specific WY
                        )

                        render_snotel_iframe(
                            snotel_data['triplet'],
                            snotel_data['station_name'],
                            chart_type="PCTMED",
                            title="SWE Percent of 30-Year Median",
                            show_years=None
                        )

                    except Exception as e:
                        st.error(f"Error embedding SNOTEL charts: {e}")
                else:
                    st.info("SNOTEL data not available for this resort (missing triplet/name).")

        st.divider()

        # --- 7. FULL DATA TABLE ---
        st.header("Today's Full Data Table")

        columns_to_drop = ['nws_forecast', 'snotel_data', 'last_updated_dt']
        display_df = df.drop(columns=columns_to_drop, errors='ignore')

        st.dataframe(display_df, width='stretch')
