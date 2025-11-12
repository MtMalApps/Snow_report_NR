# file: snotel_iframe_demo.py
import streamlit as st
from urllib.parse import quote

st.set_page_config(page_title="SNOTEL Chart Embed", page_icon="❄️", layout="centered")

def nrcs_chart_url(station_name: str, state_code: str, chart_type="WTEQ", show_years=None):
    """
    station_name: the NRCS page station title (e.g., "Stuart Mountain", "LOOKOUT")
    state_code:   2-letter state (e.g., "MT", "ID")
    chart_type:   "WTEQ" (SWE), or "PCTMED" (percent-of-median)
    show_years:   e.g., "2025" or "2024,2025" or None
    """
    base = f"https://nwcc-apps.sc.egov.usda.gov/awdb/site-plots/POR/{chart_type}/{state_code}/{quote(station_name)}.html"
    params = ["hideAnno=true", "hideControls=true", "activeOnly=true"]
    if show_years:
        params.append(f"showYears={show_years}")
    return base + "?" + "&".join(params)

st.title("SNOTEL NRCS Chart Embed (iframe)")

station_name = st.text_input("Station name (exact NRCS title):", "Stuart Mountain")
state_code   = st.text_input("State code:", "MT")
chart_type   = st.selectbox("Chart type:", ["WTEQ", "PCTMED"], index=0)
show_years   = st.text_input("showYears (optional, e.g. 2025 or 2024,2025):", "")

url = nrcs_chart_url(station_name.strip(), state_code.strip().upper(), chart_type=chart_type, show_years=(show_years or None))

st.write("**Embedding URL:**")
st.code(url, language="text")

# Use an iframe; avoid sandbox so their JS can run.
st.components.v1.html(
    f'''
      <iframe
        src="{url}"
        style="width:100%;height:560px;border:0;"
        referrerpolicy="no-referrer-when-downgrade"
      ></iframe>
    ''',
    height=560,
)
