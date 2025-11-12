# dashboard.py

import os
import json
from datetime import datetime
import urllib.parse as urlparse

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import firebase_admin
from firebase_admin import credentials, firestore


# -----------------------------
# 1) PAGE CONFIG
# -----------------------------
st.set_page_config(
    page_title="Northern Rockies Snow Report",
    page_icon="❄️",
    layout="wide",
)

db = None  # global Firestore handle


# -----------------------------
# 2) FIREBASE INIT
# -----------------------------
@st.cache_resource
def initialize_firebase():
    """Initialize Firebase Admin SDK and return Firestore client (or None)."""
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
# 3) DATA LOADING (HARDENED)
# -----------------------------
@st.cache_data(ttl=600)
def load_latest_data():
    """
    Fetch today's snow reports and coerce columns safely so we never end up
    with scalar defaults or mixed dtypes that break Arrow serialization.
    """
    if db is None:
        return pd.DataFrame()

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        docs = (
            db.collection("snow_reports")
              .where(field_path="date", op_string="==", value=today_str)
              .stream()
        )
        rows = [d.to_dict() for d in docs]
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # Helper ensures requested column exists and returns a Series
        def ensure_series(col, default, dtype="str"):
            if col in df.columns:
                s = df[col]
            else:
                s = pd.Series([default] * len(df), index=df.index)
                df[col] = s

            if dtype == "num":
                return pd.to_numeric(s, errors="coerce").fillna(0)
            else:
                # keep as string to avoid Arrow issues when rendering
                return s.astype(str).replace(["nan", "None", "N/A"], "", regex=False)

        # Object-like
        obj_cols = [
            "resort", "state", "last_updated", "lifts_open", "runs_open",
            "conditions_surface", "weather_current", "comments"
        ]
        for c in obj_cols:
            df[c] = ensure_series(c, default="", dtype="str")

        # Numeric-like
        num_cols = [
            "snow_24h_summit", "snow_24h_base", "base_depth", "summit_depth",
            "snow_overnight", "temp_base", "temp_summit", "wind_speed"
        ]
        for c in num_cols:
            df[c] = ensure_series(c, default=0, dtype="num")

        # Dict-like
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

        # Timestamps / flags
        df["last_updated_dt"] = pd.to_datetime(df["last_updated"], errors="coerce")
        df["is_powder"] = (df["snow_24h_summit"] >= 6)

        # Ensure state exists
        if "state" not in df.columns:
            df["state"] = ""

        return df

    except Exception as e:
        st.error(f"Error loading data from Firestore: {e}")
        return pd.DataFrame()


# -----------------------------
# 4) SNOTEL SWE CHART EMBED (CROPPED IFRAME)
# -----------------------------
def snotel_wteq_iframe_html(
    triplet: str,
    station_name: str,
    show_years: str | None = None,
    chart_height: int = 440,
    crop_top: int = 410,
    crop_bottom: int = 480,
) -> str:
    """
    Returns an HTML snippet embedding the NRCS WTEQ (SWE) page via iframe,
    visually cropped to show only the chart.

    - triplet: 'stationId:STATE:SNTL' (e.g., '901:MT:SNTL')
    - station_name: exact NRCS station name (e.g., 'Stuart Mountain')
    - show_years: e.g., '2026' (optional overlay year)
    """
    try:
        state = triplet.split(":")[1].strip().upper()
    except Exception:
        state = ""

    name_enc = urlparse.quote(station_name)
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/WTEQ/{state}/{name_enc}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years}")
    url = base + "?" + "&".join(params)

    # The inner iframe is taller so we can shift it up to crop out headers/footers.
    inner_height = chart_height + crop_top + crop_bottom

    # We clip the iframe by wrapping it and translating it up by -crop_top pixels
    html = f"""
<div style="width:100%; height:{chart_height}px; overflow:hidden; border-radius:8px; box-shadow:0 1px 10px rgba(0,0,0,.25);">
  <iframe
    src="{url}"
    style="
      width:100%;
      height:{inner_height}px;
      border:0;
      transform: translateY(-{crop_top}px);
      "
    loading="lazy"
  ></iframe>
</div>
"""
    return html


# -----------------------------
# 5) PAGE RENDER
# -----------------------------
st.title("❄️ Northern Rockies Snow Report")

db = initialize_firebase()
if not db:
    st.stop()

df = load_latest_data()
if df.empty:
    st.info("No data found for today yet.")
    st.stop()

# --- KPIs ---
powder_count = int(df["is_powder"].sum()) if "is_powder" in df.columns else 0
best_snow = df.loc[df["snow_24h_summit"].idxmax()] if not df["snow_24h_summit"].empty else None
best_base = df.loc[df["base_depth"].idxmax()] if not df["base_depth"].empty else None
last_dt = pd.to_datetime(df["last_updated_dt"], errors="coerce").max()

cols = st.columns(3)
cols[0].metric(
    "Today's Best Snow",
    f'{best_snow["snow_24h_summit"]:.0f}"' if best_snow is not None else "—",
    best_snow["resort"] if best_snow is not None else "",
)
cols[1].metric(
    "Deepest Base",
    f'{best_base["base_depth"]:.0f}"' if best_base is not None else "—",
    best_base["resort"] if best_base is not None else "",
)
cols[2].metric("Last Update", last_dt.strftime("%I:%M %p") if pd.notna(last_dt) else "—")

st.divider()

# --- Drill-down ---
st.header("Resort-Specific Drill-Down")

sorted_by_snow = df.sort_values("snow_24h_summit", ascending=False) if "snow_24h_summit" in df.columns else df
selected_resort = st.selectbox("Select a resort for detailed info:", sorted_by_snow["resort"])

if selected_resort:
    r = df[df["resort"] == selected_resort].iloc[0]

    st.subheader(f"{r['resort']}")
    st.caption(f"Last updated: {r.get('last_updated','')}")

    c1, c2, c3 = st.columns(3)
    c1.metric("24-Hour Snow", f"{float(r.get('snow_24h_summit',0)):.0f}\"")
    c2.metric("Base Depth", f"{float(r.get('base_depth',0)):.0f}\"")
    c3.metric("Summit Depth", f"{float(r.get('summit_depth',0)):.0f}\"")

    st.write("")

    left, right = st.columns([2, 1])

    with left:
        # Compact “details” table (strings only to keep Arrow happy)
        details = pd.DataFrame({
            "Metric": [
                "Overnight", "24h Base", "24h Summit", "Temp Base", "Temp Summit",
                "Wind", "Lifts", "Runs", "Surface", "Weather"
            ],
            "Value": [
                r.get("snow_overnight", ""),
                r.get("snow_24h_base", ""),
                r.get("snow_24h_summit", ""),
                r.get("temp_base", ""),
                r.get("temp_summit", ""),
                (f"{int(r['wind_speed'])} mph" if pd.notna(r.get("wind_speed", None)) else "—"),
                r.get("lifts_open", ""),
                r.get("runs_open", ""),
                r.get("conditions_surface", ""),
                r.get("weather_current", ""),
            ],
        }).astype(str)

        st.write("**Full Report Data**")
        st.dataframe(details, width="stretch", hide_index=True)

        # NWS box (lightweight)
        st.write("**48-Hour NWS Outlook**")
        nws = r.get("nws_forecast", {}) or {}
        if isinstance(nws, dict) and nws:
            with st.container(border=True):
                st.metric("Est. Snowfall", f"{float(nws.get('total_snow_inches', 0) or 0):.1f}\"")
                st.text(f"Temp: {nws.get('temp_low_f','N/A')}°F – {nws.get('temp_high_f','N/A')}°F")
                st.text(f"Precip: {nws.get('precip_probability_max', 0)}% chance")
        else:
            st.info("NWS data not available for this resort.")

    with right:
        st.write("**SNOTEL (SWE) Chart**")
        snotel = r.get("snotel_data", {}) or {}
        triplet = snotel.get("triplet")
        station_name = snotel.get("station_name")

        if triplet and station_name:
            # Optional: year overlay control (kept simple)
            col_y1, col_y2 = st.columns([1, 1])
            with col_y1:
                add_overlay = st.toggle("Overlay Water Year", value=False)
            show_years = None
            if add_overlay:
                with col_y2:
                    show_years = st.text_input("Year (e.g., 2026)", value="", placeholder="YYYY") or None

            # Render cropped iframe
            html = snotel_wteq_iframe_html(
                triplet=triplet,
                station_name=station_name,
                show_years=show_years,
                chart_height=440,
                crop_top=410,
                crop_bottom=480,
            )
            components.html(html, height=440, scrolling=False)
        else:
            st.info("SNOTEL station details not available for this resort.")

st.divider()

# --- Full table (strings only) ---
st.header("Today's Full Data Table")
drop_cols = ["nws_forecast", "snotel_data", "last_updated_dt"]
display_df = df.drop(columns=drop_cols, errors="ignore")
st.dataframe(display_df.astype(str), width="stretch")
