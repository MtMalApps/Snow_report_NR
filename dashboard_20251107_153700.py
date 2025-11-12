import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import os
import sys
import streamlit.components.v1 as components
import urllib.parse
import re
import json
import requests  
from bs4 import BeautifulSoup 
import plotly.graph_objects as go  # NEW
import hashlib


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
    Fetches the latest snow report for all resorts
    by querying for today's date.
    """
    if db is None:
        st.error("Firebase is not connected.")
        return pd.DataFrame()

    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        collection_ref = db.collection('snow_reports')
        query = collection_ref.where(field_path='date', op_string='==', value=today_str)
        
        docs = query.stream()
        
        data = []
        for doc in docs:
            data.append(doc.to_dict())
            
        if not data:
            st.warning(f"No data found for today ({today_str}) yet. Please wait for the scraper to run.")
            return pd.DataFrame()
            
        df = pd.DataFrame(data)
        
        # --- Data Cleaning ---
        num_cols = ['snow_24h_summit', 'snow_24h_base', 'base_depth', 'summit_depth', 
                    'snow_overnight', 'temp_base', 'temp_summit', 'wind_speed']
        for col in num_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
            
        obj_cols = ['resort', 'last_updated', 'lifts_open', 'runs_open', 
                    'conditions_surface', 'weather_current', 'comments']
        for col in obj_cols:
            df[col] = df[col].astype(str).fillna('')
            df[col] = df[col].replace(['nan', 'None', 'N/A'], '', regex=False)

        df['last_updated_dt'] = pd.to_datetime(df['last_updated'], errors='coerce')
        
        def clean_dict_col(data):
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                try:
                    return eval(data) 
                except Exception:
                    return None
            return None

        df['nws_forecast'] = df['nws_forecast'].apply(clean_dict_col)
        df['snotel_data'] = df['snotel_data'].apply(clean_dict_col)

        today = datetime.now().date()
        df['is_current'] = df['last_updated_dt'].dt.date == today
        df['is_powder'] = (df['snow_24h_summit'] >= 6) & (df['is_current'])
        
        return df

    except Exception as e:
        st.error(f"Error loading data from Firestore: {e}")
        return pd.DataFrame()

# --- 4. NEW SNOTEL CHARTING FUNCTIONS ---

@st.cache_data(ttl=3600)
def fetch_plotly_embed_html(triplet: str, station_name: str, chart_type: str = "WTEQ", show_years: str | None = None) -> str | None:
    try:
        state = triplet.split(':')[1].strip().upper()
        station_name_encoded = urllib.parse.quote(station_name)

        base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/{chart_type}/{state}/{station_name_encoded}.html"
        params = ["hideAnno=true", "hideControls=true"]
        if show_years:
            params.append(f"showYears={show_years}")
        chart_url = base + "?" + "&".join(params)

        headers = {"User-Agent": "Mozilla/5.0 (compatible; Streamlit/1.0)"}
        resp = requests.get(chart_url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text

        div_match = re.search(r'<div[^>]+id="([^"]+)"[^>]*>\s*</div>', html, flags=re.IGNORECASE)
        plot_match = re.search(r'Plotly\.newPlot\(\s*"([^"]+)"\s*,\s*([\s\S]*?)\s*\);\s*', html)
        if not (div_match and plot_match):
            return None

        original_div_id = plot_match.group(1)
        full_args = plot_match.group(2)

        key = f"{station_name}|{state}|{chart_type}|{show_years or ''}"
        target_div_id = "snotel_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:10]

        newplot_js = f'Plotly.newPlot("{target_div_id}", {full_args});'

        embed_html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
      html, body {{ margin:0; padding:0; background: transparent; }}
    </style>
  </head>
  <body>
    <div id="{target_div_id}" style="width:100%;height:100%;"></div>
    <script>
      {newplot_js}
    </script>
  </body>
</html>"""
        return embed_html
    except requests.HTTPError as e:
        return f"__HTTP_ERROR__:{e}"
    except Exception:
        return None

def render_snotel_plotly_embed(triplet: str, station_name: str, chart_type: str, title: str, show_years: str | None = None, height: int = 520):
    result = fetch_plotly_embed_html(triplet, station_name, chart_type, show_years)
    if result is None:
        st.info(f"{title}: Unable to extract chart for station \"{station_name}\".")
        return
    if isinstance(result, str) and result.startswith("__HTTP_ERROR__:"):
        st.info(f"{title}: {result.replace('__HTTP_ERROR__:', '')}")
        return
    components.html(result, height=height, scrolling=False)



def build_snotel_chart(chart_data, chart_layout, title, chart_config=None):
    """
    Builds a Plotly Figure from the page-embedded JSON and applies our styling tweaks.
    """
    fig = go.Figure(data=chart_data, layout=chart_layout)

    # Dark theme + small layout fixes
    fig.update_layout(
        title=title,
        font_color="#e2e8f0",
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0b1220",
        xaxis=dict(gridcolor="rgba(148,163,184,.15)"),
        yaxis=dict(gridcolor="rgba(148,163,184,.15)"),  # FIX: 184, not '1BA4'
        legend=dict(bgcolor="rgba(0,0,0,0.3)", bordercolor="rgba(148,163,184,.15)", borderwidth=1),
        margin=dict(t=50, l=20, r=20, b=20)
    )

    # You can return config too (for modebar toggles etc.), but Streamlit ignores most config.
    return fig


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
                details = resort_data[[
                    'snow_overnight', 'snow_24h_base', 'snow_24h_summit',
                    'base_depth', 'summit_depth', 
                    'temp_base', 'temp_summit', 
                    'wind_speed', 'lifts_open', 'runs_open', 
                    'conditions_surface', 'weather_current'
                ]].rename(lambda x: x.replace('_', ' ').title())
                
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
                        # Display the info in a container
                        with st.container(border=True):
                            st.write(f"**{snotel_data.get('station_name')}**")
                            st.metric("24-hr Snow Δ", f"{snotel_data.get('snow_depth', 0)}\"")
                            st.text(f"Density: {snotel_data.get('density', 'N/A')}%")
                            st.text(f"Quality: {snotel_data.get('snow_category', 'N/A')}")

                        # --- CHARTS (embed Plotly calls from the SNOTEL page) ---
                        # 1) SWE (WTEQ)
                        render_snotel_plotly_embed(
                            snotel_data['triplet'],
                            snotel_data['station_name'],
                            chart_type="WTEQ",
                            title="Snow Water Equivalent (SWE) & Depth",
                            show_years=None    # e.g., '2026' to force one year
                        )

                        # 2) Percent of Median (PCTMED)
                        render_snotel_plotly_embed(
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
