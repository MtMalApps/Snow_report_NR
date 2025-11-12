import streamlit as st
from urllib.parse import quote

# --- PAGE SETUP ---
st.set_page_config(
    page_title="SNOTEL NRCS Chart Viewer",
    page_icon="❄️",
    layout="centered"
)

# --- SETTINGS (CROPS + CHART SIZE) ---
CROP_TOP = 410      # hides NRCS header/menus
CROP_BOTTOM = 480   # hides NRCS footer
VIEW_HEIGHT = 440   # visible chart height


# --- URL BUILDER ---
def nrcs_chart_url(station_name: str, state_code: str, chart_type="WTEQ", show_years=None) -> str:
    """
    Build NRCS site-plots URL for the requested station.
    """
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/{chart_type}/{state_code}/{quote(station_name)}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years}")
    return base + "?" + "&".join(params)


# --- IFRAME CROPPING HTML ---
def cropped_iframe_html(url: str, crop_top_px: int, crop_bottom_px: int, view_height_px: int) -> str:
    inner_height = view_height_px + crop_top_px + crop_bottom_px
    return f"""
    <style>
      .clip {{
        width: 100%;
        height: {view_height_px}px;
        overflow: hidden;
        border: 0;
        border-radius: 8px;
        box-shadow: 0 2px 14px rgba(0,0,0,0.08);
      }}
      .clip iframe {{
        width: 100%;
        height: {inner_height}px;
        border: 0;
        transform: translateY(-{crop_top_px}px);
      }}
    </style>
    <div class="clip">
      <iframe
        src="{url}"
        referrerpolicy="no-referrer-when-downgrade"
        loading="lazy"
        allowfullscreen
      ></iframe>
    </div>
    """


# --- UI HEADER ---
st.title("❄️ SNOTEL NRCS Chart Viewer (Clean Embed)")
st.caption("Displays the official NRCS chart only — matching colors, legend, and layout.")

# --- USER INPUTS ---
col1, col2 = st.columns(2)
with col1:
    station_name = st.text_input("Station name (exact NRCS title)", "Stuart Mountain")
    chart_type = st.selectbox("Chart type", ["WTEQ", "PCTMED"], index=0)
with col2:
    state_code = st.text_input("State (2-letter)", "MT").strip().upper()
    show_years = st.text_input("Show years (optional, e.g., 2025 or 2024,2025)", "")

# --- BUILD URL + EMBED ---
url = nrcs_chart_url(
    station_name.strip(),
    state_code,
    chart_type=chart_type,
    show_years=(show_years or None)
)

st.markdown("**Embedding URL:**")
st.code(url, language="text")

html = cropped_iframe_html(
    url,
    crop_top_px=CROP_TOP,
    crop_bottom_px=CROP_BOTTOM,
    view_height_px=VIEW_HEIGHT
)
st.components.v1.html(html, height=VIEW_HEIGHT, scrolling=False)
