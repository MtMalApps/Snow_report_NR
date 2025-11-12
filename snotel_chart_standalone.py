#!/usr/bin/env python3
"""
SNOTEL SWE Chart — Standalone HTML (NRCS-style bands + legend)

- Fetches SWE (WTEQ) daily data (current WY + 1991–2020 history) via AWDB REST API.
- Builds Plotly chart with percentile shading (min–10, 10–30, 30–70, 70–90, 90–max),
  median line, previous WY (optional), and current WY.
- Writes a single self-contained HTML file you can open in any browser.

Usage:
  python3 snotel_chart_standalone.py --triplet 877:ID:SNTL --station "LOOKOUT" [--no-prev] [--height 480]

If you want to tweak colors to match NRCS exactly, change PALETTE (hex/rgba) below.
"""

import argparse
from datetime import date, datetime, timedelta
import os
import json
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests

AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

# ---------- NRCS-like palette (adjust these to match official swatches exactly) ----------
PALETTE = {
    "band_min_10": "#E8EEF9",
    "band_10_30": "#CFE0F6",
    "band_30_70": "#B5D1F2",
    "band_70_90": "#9AC1ED",
    "band_90_max": "#7DAEE6",
    "median":      "#000000",
    "current":     "#D62728",   # red
    "previous":    "#1F77B4",   # blue
    "bg":          "#FFFFFF",
    "plot_bg":     "#FFFFFF",
    "grid":        "rgba(0,0,0,0.15)",
    "font":        "#111111",
}


def wy_bounds(d: date):
    """Return (WY begin, WY end) date objects for the water year containing d (Oct 1..Sep 30)."""
    wy_start_year = d.year if d.month >= 10 else d.year - 1
    begin = date(wy_start_year, 10, 1)
    end = date(wy_start_year + 1, 9, 30)
    return begin, end


def iter_days(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def awdb_get_daily(triplet: str, element: str, begin: str, end: str):
    """
    GET /services/v1/data with duration=DAILY for one station/element.
    Returns list[(YYYY-MM-DD, value|None)]
    """
    params = {
        "stationTriplets": triplet,
        "elements": element,
        "duration": "DAILY",
        "beginDate": begin,
        "endDate": end,
        "ordinal": "1",
        "format": "json",
    }
    url = f"{AWDB_BASE}/data"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    if not isinstance(js, list) or not js:
        return []
    data_block = js[0].get("data", [])
    if not data_block:
        return []
    values = data_block[0].get("values", [])
    return [(v.get("date"), v.get("value")) for v in values]


def fetch_history_1991_2020(triplet: str, element: str = "WTEQ") -> pd.DataFrame:
    """Return a DataFrame of daily values for WY1991…WY2020 inclusive."""
    begin = "1990-10-01"
    end   = "2020-09-30"
    rows = awdb_get_daily(triplet, element, begin, end)
    df = pd.DataFrame(rows, columns=["date", "value"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["mmdd"] = df["date"].dt.strftime("%m-%d")
    return df


def compute_percentile_bands(df_hist: pd.DataFrame):
    """
    From historical DF [date, value, mmdd], compute for each MM-DD:
      min, p10, p30, p70, p90, max, median.
    Return dict: mmdd -> {keys above}.
    """
    bands = {}
    if df_hist.empty:
        return bands
    g = df_hist.groupby("mmdd")["value"]
    for mmdd, s in g:
        arr = s.dropna().to_numpy(dtype=float)
        if arr.size == 0:
            continue
        bands[mmdd] = {
            "min": float(np.nanmin(arr)),
            "p10": float(np.nanpercentile(arr, 10)),
            "p30": float(np.nanpercentile(arr, 30)),
            "p70": float(np.nanpercentile(arr, 70)),
            "p90": float(np.nanpercentile(arr, 90)),
            "max": float(np.nanmax(arr)),
            "med": float(np.nanmedian(arr)),
        }
    return bands


def build_chart_html(triplet: str, station_name: str, height: int = 480, show_prev: bool = True) -> str:
    """Build Plotly figure and return an HTML string (self-contained)."""
    # Axis: full current WY
    today = date.today()
    wy_begin, wy_end = wy_bounds(today)
    day_list = list(iter_days(wy_begin, wy_end))
    mmdd_list = [d.strftime("%m-%d") for d in day_list]

    # Current WY SWE
    cur_rows = awdb_get_daily(triplet, "WTEQ",
                              wy_begin.strftime("%Y-%m-%d"),
                              wy_end.strftime("%Y-%m-%d"))
    cur_df = pd.DataFrame(cur_rows, columns=["date", "value"])
    cur_df["date"] = pd.to_datetime(cur_df["date"], errors="coerce")
    cur_df = cur_df.dropna(subset=["date"])
    cur_df["mmdd"] = cur_df["date"].dt.strftime("%m-%d")
    cur_map = dict(zip(cur_df["mmdd"], cur_df["value"]))
    cur_y = [cur_map.get(mmdd, None) for mmdd in mmdd_list]

    # Previous WY SWE (optional)
    prev_y = None
    if show_prev:
        prev_begin, prev_end = wy_bounds(wy_begin - timedelta(days=1))
        prev_rows = awdb_get_daily(triplet, "WTEQ",
                                   prev_begin.strftime("%Y-%m-%d"),
                                   prev_end.strftime("%Y-%m-%d"))
        prev_df = pd.DataFrame(prev_rows, columns=["date", "value"])
        prev_df["date"] = pd.to_datetime(prev_df["date"], errors="coerce")
        prev_df = prev_df.dropna(subset=["date"])
        prev_df["mmdd"] = prev_df["date"].dt.strftime("%m-%d")
        prev_map = dict(zip(prev_df["mmdd"], prev_df["value"]))
        prev_y = [prev_map.get(mmdd, None) for mmdd in mmdd_list]

    # Bands from 1991–2020
    hist = fetch_history_1991_2020(triplet, "WTEQ")
    bands = compute_percentile_bands(hist)
    y_min = [bands.get(mmdd, {}).get("min") for mmdd in mmdd_list]
    y_p10 = [bands.get(mmdd, {}).get("p10") for mmdd in mmdd_list]
    y_p30 = [bands.get(mmdd, {}).get("p30") for mmdd in mmdd_list]
    y_p70 = [bands.get(mmdd, {}).get("p70") for mmdd in mmdd_list]
    y_p90 = [bands.get(mmdd, {}).get("p90") for mmdd in mmdd_list]
    y_max = [bands.get(mmdd, {}).get("max") for mmdd in mmdd_list]
    y_med = [bands.get(mmdd, {}).get("med") for mmdd in mmdd_list]

    # Build Plotly figure (legend ALWAYS visible)
    fig = go.Figure()

    # Shaded bands (stacked fills). Each band has its own legend entry.
    fig.add_trace(go.Scatter(x=day_list, y=y_min, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_min_10"]),
                             name="Min", showlegend=False))
    fig.add_trace(go.Scatter(x=day_list, y=y_p10, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_min_10"]),
                             fill="tonexty", fillcolor=PALETTE["band_min_10"],
                             name="Min–10th"))
    fig.add_trace(go.Scatter(x=day_list, y=y_p30, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_10_30"]),
                             fill="tonexty", fillcolor=PALETTE["band_10_30"],
                             name="10th–30th"))
    fig.add_trace(go.Scatter(x=day_list, y=y_p70, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_30_70"]),
                             fill="tonexty", fillcolor=PALETTE["band_30_70"],
                             name="30th–70th"))
    fig.add_trace(go.Scatter(x=day_list, y=y_p90, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_70_90"]),
                             fill="tonexty", fillcolor=PALETTE["band_70_90"],
                             name="70th–90th"))
    fig.add_trace(go.Scatter(x=day_list, y=y_max, mode="lines",
                             line=dict(width=0.5, color=PALETTE["band_90_max"]),
                             fill="tonexty", fillcolor=PALETTE["band_90_max"],
                             name="90th–Max"))

    # Median
    fig.add_trace(go.Scatter(
        x=day_list, y=y_med, mode="lines", name="Median (1991–2020)",
        line=dict(width=2, color=PALETTE["median"], dash="dot")
    ))

    # Previous WY
    if prev_y is not None:
        fig.add_trace(go.Scatter(
            x=day_list, y=prev_y, mode="lines", name="Previous WY",
            line=dict(width=2, color=PALETTE["previous"], dash="dash")
        ))

    # Current WY
    fig.add_trace(go.Scatter(
        x=day_list, y=cur_y, mode="lines", name="Current WY",
        line=dict(width=3, color=PALETTE["current"])
    ))

    # Layout (legend under title; white/bright like NRCS)
    fig.update_layout(
        title=f"{station_name} — SWE (WTEQ) with Percentile Shading (1991–2020)",
        xaxis_title="Date",
        yaxis_title="SWE (inches)",
        paper_bgcolor=PALETTE["bg"],
        plot_bgcolor=PALETTE["plot_bg"],
        font_color=PALETTE["font"],
        xaxis=dict(gridcolor=PALETTE["grid"]),
        yaxis=dict(gridcolor=PALETTE["grid"]),
        margin=dict(t=50, l=24, r=24, b=24),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=height,
        showlegend=True,
    )

    # Export self-contained HTML
    return fig.to_html(full_html=True, include_plotlyjs="cdn")


def main():
    p = argparse.ArgumentParser(description="Generate a standalone NRCS-style SWE chart HTML for a SNOTEL station.")
    p.add_argument("--triplet", required=True, help="Station triplet (e.g., 877:ID:SNTL)")
    p.add_argument("--station", required=True, help="Station name (for title/filename)")
    p.add_argument("--height", type=int, default=480, help="Chart height in px (default 480)")
    p.add_argument("--no-prev", action="store_true", help="Hide previous WY line")
    args = p.parse_args()

    html = build_chart_html(
        triplet=args.triplet,
        station_name=args.station,
        height=args.height,
        show_prev=(not args.no_prev),
    )

    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in args.station.strip())
    out = f"snotel_chart_{safe_name}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Wrote {out}")
    print("Open it in your browser to view the chart.")


if __name__ == "__main__":
    main()

