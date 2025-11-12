# dashboard.py

from __future__ import annotations

import os
import math
from datetime import datetime, timedelta, date
from typing import List, Tuple, Optional, Dict

import numpy as np
import pandas as pd
import altair as alt
import streamlit as st
import streamlit.components.v1 as components

# -------------------------------
# Firestore (if you use it)
# -------------------------------
from google.cloud import firestore  # make sure your venv has google-cloud-firestore installed


# ===============================
# Config & constants
# ===============================
APP_TITLE = "Northern Rockies Snow Report"
EMAIL_CONTACT = "mtmalamuteapps@gmail.com"

# If you previously load SNOTEL stations elsewhere, you can override this list or ignore it.
# Format: (triplet, station_display_name)
SNOTEL_STATIONS: List[Tuple[str, str]] = [
    # Example entries — replace with your live ones or keep your original loader.
    # ("667:MT:SNTL", "Stuart Mountain"),
    # ("822:MT:SNTL", "Noisy Basin"),
]

# Color palette (Altair will auto-color if you prefer; keeping defaults minimal here)
CHART_HEIGHT_STEP = 50  # bar height per resort row
LABEL_MIN_INCH = 1      # don't print segment labels when segment < 1"


# ===============================
# Utilities
# ===============================
def coerce_arrow_friendly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Arrow / Streamlit sometimes chokes on 'object' columns with mixed types.
    We coerce obvious numeric columns and stringify the rest to avoid ArrowTypeError.
    """
    df = df.copy()

    # Try to coerce common numeric columns if present
    numeric_candidates = [
        "Overnight", "24h", "48h", "Base", "Summit", "Temp", "SWE", "5-Day Total",
        "total_snow", "snow", "Lifts", "Runs"
    ]
    for col in numeric_candidates:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="ignore")

    # Any remaining 'object' columns that still contain ints/bytes -> stringify
    for col in df.columns:
        if df[col].dtype == "object":
            # If this column has mixed types, stringify to be safe
            if any(isinstance(x, (bytes, bytearray, int, float)) for x in df[col].dropna().head(100)):
                df[col] = df[col].astype(str)

    return df


def safe_strip(x) -> str:
    if isinstance(x, str):
        return x.strip()
    return x


def clean_blank_resort_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove the two blank rows that sometimes appear in the table:
    - Resort == "" (empty after stripping)
    - Resort string literal 'nan'
    - Resort is null
    """
    if "Resort" not in df.columns:
        return df

    df = df.copy()
    df["Resort"] = df["Resort"].astype(str).map(safe_strip)

    mask = (
        df["Resort"].notna()
        & (df["Resort"] != "")
        & (df["Resort"].str.lower() != "nan")
    )
    return df.loc[mask].copy()


def ordinal(n: int) -> str:
    return "%d%s" % (n, "tsnrhtdd"[(math.floor(n/10)%10 != 1)*(n % 10 < 4) * n % 10::4])


def get_snotel_iframe_html(triplet: str, station_name: str, water_year: int) -> str:
    """
    Returns a self-contained SNOTEL iframe embed.

    triplet example: '667:MT:SNTL'
    water_year: e.g., 2026
    """
    start = f"{water_year - 1}-10-01"
    end = f"{water_year}-09-30"
    src = (
        "https://wcc.sc.egov.usda.gov/reportGenerator/view_graph/"
        f"customSingleStationReport/daily/start_of_period/{triplet}"
        '%7Cid=%22WTEQ::value%22,legend%3D%22SWE%20(in)%22'
        '%7Cid=%22SNWD::value%22,legend%3D%22Snow%20Depth%20(in)%22'
        f"?fitToY=0&start={start}&end={end}"
    )

    return f"""
    <div style="border:1px solid #475569;border-radius:10px;overflow:hidden;background:#0f172a;">
      <div style="padding:10px 12px;font-weight:600;color:#e2e8f0;font-family:Arial, Helvetica, sans-serif;">
        SNOTEL • {station_name} ({triplet}) • WY {water_year}
      </div>
      <iframe
        src="{src}"
        width="100%"
        height="380"
        style="border:0; background:#0b1220;"
        loading="lazy"
        referrerpolicy="no-referrer"
      ></iframe>
    </div>
    """


# ===============================
# Data loading
# ===============================
def get_firestore_client() -> firestore.Client:
    """
    Create a Firestore client using default credentials/environment.
    Assumes GOOGLE_APPLICATION_CREDENTIALS or equivalent is configured.
    """
    return firestore.Client()


@st.cache_data(show_spinner=False)
def load_latest_data(_db: firestore.Client, *, collection: str = "resort_reports") -> pd.DataFrame:
    """
    Load your latest dataset from Firestore (or adapt to CSV if needed).
    NOTE: _db arg name starts with '_' to avoid Streamlit hashing issues.
    """
    # ---- Example Firestore structure: adapt to your schema! ----
    # We assume documents under `collection` where each doc has resort fields.
    # If you already have a working loader in your codebase, you can drop it in here.

    docs = list(_db.collection(collection).stream())

    rows = []
    for d in docs:
        data = d.to_dict() or {}

        # Expected normalized schema — align with your scraper's output
        # Favor your existing field names (resort, display_name, last_updated, etc.)
        rows.append({
            "resort": data.get("resort", ""),
            "display_name": data.get("display_name", ""),
            "state": data.get("state", ""),
            "last_updated": data.get("last_updated"),  # ISO string or timestamp
            # Snow windows (inches); adjust to your names if different:
            "snow_overnight": data.get("snow_overnight", 0) or 0,
            "snow_24h": data.get("snow_24h", 0) or 0,
            "snow_48h": data.get("snow_48h", 0) or 0,
            # Daily snapshot for last 5 days (if your scraper stores them). If not, we backfill down below.
            "d0": data.get("d0", 0) or 0,
            "d1": data.get("d1", 0) or 0,
            "d2": data.get("d2", 0) or 0,
            "d3": data.get("d3", 0) or 0,
            "d4": data.get("d4", 0) or 0,
            # Ops:
            "lifts": data.get("lifts", None),
            "runs": data.get("runs", None),
        })

    df = pd.DataFrame(rows)

    # Normalize last_updated to pandas datetime
    if "last_updated" in df.columns:
        df["last_updated"] = pd.to_datetime(df["last_updated"], errors="coerce")

    # Ensure display_name fallback if missing
    df["display_name"] = df["display_name"].where(df["display_name"].astype(str).str.len() > 0, df["resort"])

    return df


# ===============================
# Presentation / transforms
# ===============================
def build_leaderboard(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Build the 'Today's Snow Leaderboard' table with blanks removed and Arrow-safe types.
    """
    df = df_raw.copy()

    # Your existing mapping to presentation columns (adjust to your real field names)
    table = pd.DataFrame({
        "Resort": df["display_name"].astype(str).map(safe_strip),
        "State": df.get("state", pd.Series([""] * len(df))),
        "Overnight": pd.to_numeric(df.get("snow_overnight", 0), errors="coerce").fillna(0).astype(int),
        "24h": pd.to_numeric(df.get("snow_24h", 0), errors="coerce").fillna(0).astype(int),
        "48h": pd.to_numeric(df.get("snow_48h", 0), errors="coerce").fillna(0).astype(int),
        "Lifts": df.get("lifts", pd.Series([None] * len(df))),
        "Runs": df.get("runs", pd.Series([None] * len(df))),
        "Last Updated": df.get("last_updated", pd.Series([pd.NaT] * len(df))),
    })

    # Drop blank resort rows
    table = clean_blank_resort_rows(table)

    # Format Last Updated nicely (string), leave NaT as blank
    if "Last Updated" in table.columns:
        table["Last Updated"] = pd.to_datetime(table["Last Updated"], errors="coerce")
        table["Last Updated"] = table["Last Updated"].dt.strftime("%Y-%m-%d %H:%M").fillna("")

    # Arrow compatibility
    table = coerce_arrow_friendly(table)

    # Sort by 24h desc, then Overnight desc (feel free to adjust)
    table = table.sort_values(["24h", "Overnight"], ascending=[False, False]).reset_index(drop=True)
    return table


def compute_five_day_long(df: pd.DataFrame, *, as_of: Optional[date] = None) -> pd.DataFrame:
    """
    Build a long df for the 5-day chart:
    Columns: resort, display_name, date, day_label, snow, total_snow
    Uses d0..d4 fields per resort as the daily snow for the last 5 days.

    If your source doesn't have d0..d4, swap in your existing logic that computed
    daily values from your historic store. The debug you shared suggests you already had it.
    """
    as_of = as_of or pd.Timestamp.now().date()

    # Expect d0..d4 present; if not, set zeros to keep chart valid
    for i in range(5):
        col = f"d{i}"
        if col not in df.columns:
            df[col] = 0

    days = [as_of - timedelta(days=4 - i) for i in range(5)]
    day_labels = [pd.Timestamp(d).strftime("%a %m/%d") for d in days]

    frames = []
    for _, row in df.iterrows():
        name = str(row.get("display_name", row.get("resort", "")) or "").strip()
        if not name or name.lower() == "nan":
            continue

        series = []
        total = 0
        for i, (d, label) in enumerate(zip(days, day_labels)):
            snow_i = float(row.get(f"d{i}", 0) or 0)
            total += max(0.0, snow_i)
            series.append({
                "resort": row.get("resort", name),
                "display_name": name,
                "date": pd.Timestamp(d),
                "day_label": label,
                "snow": max(0.0, snow_i),  # ensure non-negative
                "total_snow": total  # cumulative if you prefer stacked interpretation
            })
        frames.extend(series)

    chart_df = pd.DataFrame(frames)

    # Order resorts by 5-day TOTAL (sum of the 5 daily values), not the cumulative tail value.
    totals = chart_df.groupby("display_name", as_index=False)["snow"].sum().rename(columns={"snow": "sum5"})
    chart_df = chart_df.merge(totals, on="display_name", how="left")

    return chart_df


def five_day_chart(chart_df: pd.DataFrame) -> alt.Chart:
    """
    Horizontal stacked bars by day per resort row.
    - Legend at top-left (anchor start)
    - Integer x-axis ticks
    - Per-segment labels (>=1")
    - Right-end total labels
    - Resorts ordered by 5-day total desc
    """
    if chart_df.empty:
        return alt.Chart(pd.DataFrame({"msg": ["No data for the last 5 days."]})).mark_text().encode(text="msg:N")

    # Sort resorts by 5-day total (sum5) desc
    order = (
        chart_df.groupby("display_name")["snow"]
        .sum()
        .sort_values(ascending=False)
        .index.tolist()
    )

    # For right-end totals (sum5 per resort)
    totals = (
        chart_df.groupby("display_name", as_index=False)["snow"]
        .sum()
        .rename(columns={"snow": "sum5"})
    )

    # Base chart
    base = alt.Chart(chart_df)

    # Stacked bars: x = snow (quant), y = resort, color by day label
    bars = (
        base.mark_bar()
        .encode(
            x=alt.X(
                "snow:Q",
                title="5-Day Snowfall (inches)",
                stack="zero",
                axis=alt.Axis(
                    format="d",           # integers, no decimals
                    tickMinStep=1,
                    grid=True,
                    gridColor="rgba(148,163,184,0.1)",
                ),
                scale=alt.Scale(nice=True),  # keep ticks clean
            ),
            y=alt.Y(
                "display_name:N",
                title=None,  # remove "Resort(s)" label
                sort=order,
                axis=alt.Axis(labelFontSize=14, labelFontWeight=600),
            ),
            color=alt.Color(
                "day_label:N",
                title="Day",
                legend=alt.Legend(orient="top", direction="horizontal", anchor="start",
                                  labelFontSize=13, titleFontSize=14, titleFontWeight=600),
            ),
            tooltip=[
                alt.Tooltip("display_name:N", title="Resort"),
                alt.Tooltip("day_label:N", title="Day"),
                alt.Tooltip("snow:Q", title="Snow", format=".1f"),
            ],
        )
    )

    # Segment labels at the midpoint of each stacked segment (only if >= 1")
    segment_labels = (
        base.transform_filter("datum.snow >= {}".format(LABEL_MIN_INCH))
        .transform_stack(
            stack="snow",
            as_=["snow_start", "snow_end"],
            sort=[alt.SortField("date", order="ascending")],
            groupby=["display_name"],
        )
        .transform_calculate(snow_midpoint="(datum.snow_start + datum.snow_end) / 2")
        .mark_text(align="center", baseline="middle", fontSize=12, fontWeight="bold", color="black")
        .encode(
            x=alt.X("snow_midpoint:Q"),
            y=alt.Y("display_name:N", sort=order),
            text=alt.Text("snow:Q", format=".0f"),
        )
    )

    # Right-end total labels (sum5) per resort
    totals_layer = (
        alt.Chart(totals)
        .transform_filter("datum.sum5 > 0")
        .mark_text(align="left", baseline="middle", dx=6, fontSize=14, fontWeight="bold")
        .encode(
            x=alt.X("sum5:Q"),
            y=alt.Y("display_name:N", sort=order),
            text=alt.Text("sum5:Q", format=".0f"),
        )
    )

    chart = alt.layer(bars, segment_labels, totals_layer).properties(
        height={"step": CHART_HEIGHT_STEP},
        background="transparent",
    )

    return chart


# ===============================
# Streamlit app
# ===============================
def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    st.title(APP_TITLE)

    # Firestore client
    db = get_firestore_client()

    # Load the latest data (from your working source/schema)
    df_raw = load_latest_data(db, collection="resort_reports")

    # --------------------
    # Leaderboard Table
    # --------------------
    st.subheader("Today's Snow Leaderboard")

    table = build_leaderboard(df_raw)

    # Ensure no blank rows sneaking in
    table = clean_blank_resort_rows(table)

    # Display with Streamlit
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
    )

    # --------------------
    # 5-Day Snowfall Chart
    # --------------------
    st.subheader("5-Day Snowfall Trends")

    # If your real data builds the last-5-day windows elsewhere, replace this call
    chart_df = compute_five_day_long(df_raw, as_of=pd.Timestamp.now().date())

    chart = five_day_chart(chart_df)
    st.altair_chart(chart, use_container_width=False, theme=None, width="stretch")  # width is set by chart; keep stretch

    # --------------------
    # SNOTEL Section (restored)
    # --------------------
    if SNOTEL_STATIONS:
        st.subheader("SNOTEL Stations")
        wy = pd.Timestamp.now().year if pd.Timestamp.now().month >= 10 else pd.Timestamp.now().year - 1
        for triplet, station_name in SNOTEL_STATIONS:
            components.html(
                get_snotel_iframe_html(triplet, station_name, wy),
                height=440,
                scrolling=False
            )

    # --------------------
    # Footer
    # --------------------
    st.markdown(
        f'<div style="margin-top:16px;color:#94a3b8;">'
        f'<a href="mailto:{EMAIL_CONTACT}" target="_blank" style="color:#93c5fd;text-decoration:none;">{EMAIL_CONTACT}</a>'
        f'</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
