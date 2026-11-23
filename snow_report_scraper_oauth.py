"""
Montana/Idaho Snow Report Scraper - Cleaned & Standardized Version
This script scrapes snow reports from multiple ski resorts and stores data in CSV files.
It calculates SNOTEL density/SWE but DOES NOT upload to Firebase directly.

Features:
- Standardized data schema across all resorts
- CSV backup storage
- SNOTEL backcountry data integration (Density & Category)
- Powder alert notifications (configurable threshold)
- UNIVERSAL STALE DATA DETECTION (Zombie Check)

Environment Variables Required:
- GMAIL_ADDRESS: Your Gmail address for sending reports
- GMAIL_APP_PASSWORD: Gmail app-specific password
"""
import requests
import pandas as pd
from datetime import datetime, timedelta
from abc import ABC, abstractmethod
from bs4 import BeautifulSoup
import re
import os
from typing import Dict, List, Optional, Any  
import time
import logging
import json
from math import ceil
import sys
import warnings  # to suppress Pandas warnings in output
from nws_storm_outlook_v3 import NWSStormOutlook
from zoneinfo import ZoneInfo
import firebase_admin
from firebase_admin import credentials, firestore



# =============================================================================
# CONFIGURATION
# =============================================================================

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6
# Resort coordinates for NWS forecasts
RESORT_COORDS = {
    # Existing
    "Snowbowl": {"lat": 46.9137, "lon": -113.9525},
    "Discovery": {"lat": 46.3861, "lon": -113.2144},
    "LookoutPass": {"lat": 47.4536, "lon": -115.7414},
    "BigMountain": {"lat": 48.4853, "lon": -114.3572},
    "LostTrail": {"lat": 45.7019, "lon": -113.9219},
    "TetonPass": {"lat": 47.9299, "lon": -112.8109},
    "Showdown": {"lat": 46.9939, "lon": -110.6647},
    "Blacktail": {"lat": 48.0067, "lon": -114.3589},
    # New Additions
    "SilverMountain": {"lat": 47.4991, "lon": -116.1192},
    "TurnerMountain": {"lat": 48.6098, "lon": -115.6488},
    "BearPaw": {"lat": 48.1621, "lon": -109.6799},
    "GreatDivide": {"lat": 46.7489, "lon": -112.3285},
    "Schweitzer": {"lat": 48.3778, "lon": -116.6334},
    "Maverick": {"lat": 45.4383, "lon": -113.1422},
    "BigSky": {"lat": 45.2809, "lon": -111.4406},
    "BridgerBowl": {"lat": 45.8139, "lon": -110.9219},
    "RedLodge": {"lat": 45.1811, "lon": -109.3543},
}

# Data directory
DATA_DIR = os.path.expanduser("~/snow_reports/data")
LOG_DIR = os.path.expanduser("~/snow_reports/logs")
# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

LOCAL_TZ = ZoneInfo("America/Denver")

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================
def normalize_text(s: str) -> str:
    """Replace curly quotes, dashes, and non-breaking spaces with ASCII equivalents."""
    if not s:
        return s

    replacements = {
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' ',  # non-breaking space -> normal space
    }
    for orig, repl in replacements.items():
        s = s.replace(orig, repl)
    
    return s
    
    
def validate_scrape_minimums(resort_name: str, data: Optional[Dict]) -> List[str]:
    """
    Conservative checks: only flag when parsing looks clearly broken.
    IMPORTANT: 0.0 is valid data and must NOT be treated as missing.
    """
    if not data:
        return ["no_data_returned"]

    problems: List[str] = []

    # Basic "wrong page" / interstitial hint
    blob = " ".join([
        str(data.get("comments") or ""),
        str(data.get("weather_current") or ""),
        str(data.get("conditions_surface") or ""),
    ]).lower()

    if "enable javascript" in blob or "please enable javascript" in blob:
        problems.append("javascript_gate_page")

    # Helper: missing means None or empty string only (NOT 0 / 0.0)
    def _missing(v):
        if v is None:
            return True
        if isinstance(v, str) and v.strip() == "":
            return True
        return False

    # Universal "completely empty scrape" check
    # If ALL of these are missing, the parser probably failed.
    universal_core = [
        data.get("snow_overnight"),
        data.get("snow_24h_base"),
        data.get("snow_48h_base"),
        data.get("base_depth"),
        data.get("summit_depth"),
        data.get("runs_open"),
        data.get("lifts_open"),
        data.get("comments"),
        data.get("weather_current"),
        data.get("conditions_surface"),
    ]
    if all(_missing(v) for v in universal_core):
        problems.append("missing_all_core_fields")

    # Resort-specific *light* checks (only if you want them)
    if resort_name == "Maverick":
        # Maverick is allowed to report 0.0 snowfall; only quarantine if EVERYTHING is missing.
        m_core = [
            data.get("snow_overnight"),
            data.get("snow_24h_base"),
            data.get("base_depth"),
            data.get("summit_depth"),
        ]
        if all(_missing(v) for v in m_core):
            problems.append("missing_maverick_core")

    if resort_name == "GreatDivide":
        # Great Divide often reports strings like "0 / 6" — that's valid.
        # Only quarantine if these are actually missing (None/blank), not "0 / 6".
        if _missing(data.get("runs_open")):
            problems.append("missing_runs_open")
        if _missing(data.get("lifts_open")):
            problems.append("missing_lifts_open")
        # base_depth can be 0 early season; only missing if None/blank
        if _missing(data.get("base_depth")):
            problems.append("missing_base_depth")

    # --- Range validation: flag physically impossible values ---
    # These catch hallucinations or misread year-to-date totals as daily snowfall.
    RANGES = {
        "snow_overnight":  (0, 36),
        "snow_24h_base":   (0, 48),
        "snow_24h_summit": (0, 48),
        "snow_48h_base":   (0, 72),
        "snow_48h_summit": (0, 72),
        "snow_7day":       (0, 200),
        "base_depth":      (0, 400),
        "summit_depth":    (0, 400),
        "temp_base":       (-60, 100),
        "temp_summit":     (-60, 100),
        "wind_speed":      (0, 200),
    }
    for field, (lo, hi) in RANGES.items():
        v = data.get(field)
        if v is None:
            continue
        try:
            fv = float(v)
            if fv < lo or fv > hi:
                problems.append(f"{field}_out_of_range({fv})")
        except (ValueError, TypeError):
            pass

    return problems




def quarantine_bad_scrape(resort_name: str, data: Dict, problems: List[str]) -> Dict:
    """
    Lenient mode: keep pipeline running, but prevent bad parses from poisoning output.

    CRITICAL FIX:
      - DO NOT wipe last_updated. Staleness logic already ran earlier.
      - Wipe only the fields that would create false powder alerts / leaderboards.
      - Add a clear warning + mark data_source.
    """
    # Keep whatever staleness logic decided earlier
    # data["last_updated"] stays as-is.

    wipe_keys = [
        # Snowfall / depth fields that can poison powder alert & charts
        "snow_overnight", "snow_24h_base", "snow_24h_summit",
        "snow_48h_base", "snow_48h_summit", "snow_7day",
        "base_depth", "summit_depth",
        # Optionally wipe these too if you want to avoid bad-looking report text
        "temp_base", "temp_summit", "wind_speed",
        "conditions_surface", "weather_current",
        "forecast_base_temp_high", "forecast_base_temp_low",
        "forecast_summit_temp_high", "forecast_summit_temp_low",
        "forecast_precipitation", "forecast_snow",
    ]

    for k in wipe_keys:
        data[k] = None

    prev = (data.get("comments") or "").strip()
    warn = f"SCRAPE_WARNING: {resort_name} quarantined ({', '.join(problems)})."
    data["comments"] = warn if not prev else f"{prev} | {warn}"

    # Keep original data_source if you want, but mark as quarantined for downstream filters
    data["data_source"] = "quarantined"
    data["scrape_warnings"] = problems  # machine-readable
    return data



def apply_quarantine_if_needed(scraper_name: str, snow_data: Optional[Dict]) -> Optional[Dict]:
    """
    One-liner helper for main():
      snow_data = apply_quarantine_if_needed(scraper.resort_name, snow_data)
    """
    if not snow_data:
        return snow_data

    problems = validate_scrape_minimums(scraper_name, snow_data)
    if problems:
        logging.warning(f"{scraper_name} scrape looks wrong; quarantining: {problems}")
        snow_data = quarantine_bad_scrape(scraper_name, snow_data, problems)

    return snow_data





# =============================================================================
# BASE SCRAPER CLASS (WITH UNIVERSAL STALENESS CHECK)
# =============================================================================
class BaseSnowReportScraper(ABC):
    """
    Abstract base class for ski resort snow report scrapers.
    Includes Universal 'Zombie Data' Detection.
    """

    STANDARD_COLUMNS = [
        'resort', 'date', 'timestamp', 'last_updated',
        'snow_overnight', 'snow_24h_base', 'snow_24h_summit',
        'snow_48h_base', 'snow_48h_summit', 'snow_7day',
        'base_depth', 'summit_depth',
        'temp_base', 'temp_summit', 'wind_speed',
        'conditions_surface', 'weather_current',
        'lifts_open', 'runs_open',
        'comments', 'data_source',
        'nws_forecast', 'snotel_data',
        'percent_of_median',  
        'forecast_base_temp_high', 'forecast_base_temp_low',
        'forecast_summit_temp_high', 'forecast_summit_temp_low',
        'forecast_precipitation', 'forecast_snow',
    ]

    def __init__(self, resort_name: str):
        self.resort_name = resort_name
        self.csv_filename = os.path.join(DATA_DIR, f"{resort_name}_snow_report.csv")
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        }

    
    def _sanitize_text_fields(self, data: Dict) -> Dict:
        """
        Final defensive cleanup for ALL string fields before persistence.
        This prevents hidden Unicode, broken CSV rows, and HTML junk.
        """
        if not data:
            return data

        for k, v in data.items():
            if isinstance(v, str):
                s = normalize_text(v)

                # Collapse whitespace
                s = re.sub(r"\s+", " ", s).strip()

                # Convert empty strings to None
                if s == "":
                    data[k] = None
                else:
                    data[k] = s

        return data

    
    
    
    #@staticmethod
    def clean_numeric(self, val):
        """
        Convert a value to float safely.
        Returns None if val is None/blank/non-numeric.
        """
        if val is None:
            return None
    
        # Already numeric
        if isinstance(val, (int, float)):
            return float(val)
    
        s = str(val).strip()
        if not s:
            return None
    
        s_lower = s.lower()
        if s_lower in {"na", "n/a", "none", "null", "nan", "--"}:
            return None
    
        # Remove common junk: quotes, commas, inches, etc.
        s = s.replace(",", "").replace('"', "").replace("in", "").strip()
    
        # Treat ranges like "4-5" as None (avoids false changes)
        if "-" in s and any(ch.isdigit() for ch in s):
            return None
    
        try:
            return float(s)
        except Exception:
            return None



    @staticmethod
    def now_string() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def blank_record(self) -> Dict:
        return {col: None for col in self.STANDARD_COLUMNS}

    def _fetch_last_report_from_firebase(self):
        """
        Universal helper to get the previous report for THIS resort from Firebase.
        Used for fingerprinting/staleness checks.
        """
        try:
            # Ensure we can access firestore (it should be initialized in main)
            if not firebase_admin._apps:
                return None
                
            db = firestore.client()
            docs = (db.collection('snow_reports')
                    .where(filter=firestore.FieldFilter('resort', '==', self.resort_name))
                    .order_by('date', direction=firestore.Query.DESCENDING)
                    .limit(1)
                    .stream())
            
            last_doc = next(docs, None)
            if not last_doc:
                return None
            return last_doc.to_dict()
        except Exception as e:
            # Silent fail is okay here, we just won't be able to check staleness
            return None

    def _check_and_fix_staleness(self, data: Dict) -> Dict:
        """
        If last_updated is missing, determine whether the report is stale by
        comparing a fingerprint of key values to the most recent Firebase record.
    
        Behavior:
          - If fingerprint matches last report -> reuse last_updated and mark stale
          - If fingerprint differs -> set last_updated to now
          - Uses ASCII-only stale flag
          - Treats None as None (NOT 0.0) to avoid false mismatches
        """
    
        # If scraper explicitly set last_updated, trust it
        if data.get("last_updated"):
            return data
    
        last_report = self._fetch_last_report_from_firebase()
        fallback_date = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    
        if not last_report:
            data["last_updated"] = fallback_date
            return data
    
        STALE_FLAG = " [STALE]"
    
        old_comments = str(last_report.get("comments", "") or "")
        if STALE_FLAG in old_comments:
            old_comments = old_comments.replace(STALE_FLAG, "").strip()
    
        # ------------------------------------------------------------------
        # Helper: normalize numeric fields
        # IMPORTANT: None stays None (no forced 0.0)
        # ------------------------------------------------------------------
        def norm(x):
            if x is None:
                return None
            return self.clean_numeric(x)
    
        # ------------------------------------------------------------------
        # Build fingerprints
        # ------------------------------------------------------------------
        current_fingerprint = (
            norm(data.get("snow_overnight")),
            norm(data.get("snow_24h_base")),
            norm(data.get("snow_24h_summit")),
            norm(data.get("snow_7day")),
            norm(data.get("base_depth")),
            norm(data.get("summit_depth")),
            len("" if str(data.get("comments") or "").strip().lower() == "none" else str(data.get("comments") or "")),
        )
    
        old_fingerprint = (
            norm(last_report.get("snow_overnight")),
            norm(last_report.get("snow_24h_base")),
            norm(last_report.get("snow_24h_summit")),
            norm(last_report.get("snow_7day")),
            norm(last_report.get("base_depth")),
            norm(last_report.get("summit_depth")),
            len("" if old_comments.strip().lower() == "none" else old_comments),

        )
    
        # ------------------------------------------------------------------
        # Decide stale vs updated
        # ------------------------------------------------------------------
        if current_fingerprint == old_fingerprint:
            # No change → stale
            data["last_updated"] = last_report.get("last_updated")
            comments = data.get("comments") or ""
            if STALE_FLAG not in comments:
                data["comments"] = (comments + STALE_FLAG).strip()
        else:
            # Change detected → new report
            data["last_updated"] = fallback_date
    
        return data


    @abstractmethod
    def get_url(self) -> str:
        pass

    @abstractmethod
    def process_data(self, response) -> Dict:
        pass

    def get_snow_report(self) -> Optional[Dict]:
        self._last_page_html = None  # reset each run; populated on success for LLM fallback
        try:
            response = requests.get(self.get_url(), headers=self.headers, timeout=20)
            response.raise_for_status()
            self._last_page_html = response.text
            data = self.process_data(response)

            if data:
                # 1. Run the Universal Staleness Check
                data = self._check_and_fix_staleness(data)

                # 2. Add Standard Metadata
                data.update({
                    'resort': self.resort_name,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
            return data

        except requests.exceptions.SSLError:
            # Retry without SSL
            try:
                response = requests.get(self.get_url(), headers=self.headers, timeout=20, verify=False)
                response.raise_for_status()
                self._last_page_html = response.text
                data = self.process_data(response)

                if data:
                    data = self._check_and_fix_staleness(data)
                    data.update({
                        'resort': self.resort_name,
                        'date': datetime.now().strftime('%Y-%m-%d'),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
                return data
            except Exception as e:
                print(f"Error fetching {self.resort_name} (SSL disabled): {e}")
                return None

        except Exception as e:
            print(f"Error fetching snow report for {self.resort_name}: {e}")
            return None

    def save_to_csv(self, data: Dict):
        try:
            if data is None:
                return

            row = {col: None for col in self.STANDARD_COLUMNS}
            row.update({
                k: normalize_text(v) if isinstance(v, str) else v
                for k, v in data.items()
                if v is not None
            })

            df_new = pd.DataFrame([row], columns=self.STANDARD_COLUMNS)

            if os.path.exists(self.csv_filename):
                df_old = pd.read_csv(self.csv_filename, dtype=str, keep_default_na=False)
                # Ensure schema consistency
                for col in self.STANDARD_COLUMNS:
                    if col not in df_old.columns:
                        df_old[col] = None
                df_all = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_all = df_new

            # Sort + dedupe
            df_all = df_all.sort_values(['date', 'timestamp'], ascending=True)
            df_all = df_all.drop_duplicates(subset='date', keep='last')

            df_all.to_csv(self.csv_filename, index=False, encoding='utf-8-sig')

        except Exception as e:
            print(f"Error saving to CSV: {e}")

# =============================================================================
# RESORT-SPECIFIC SCRAPERS
# =============================================================================
class SnowbowlScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Snowbowl")

    def get_url(self):
        return "https://www.montanasnowbowl.com/report.php"

    def process_data(self, response):
        data = self.blank_record()
        soup = BeautifulSoup(response.text, "html.parser")
        text = response.text

        # ---------------------------------------------------------
        # 1) Parse the Snowbowl embedded POLLING block (authoritative)
        #    <!-- BEGIN POLLING -- ... -- END POLLING -->
        #    Entries are: key|value|~
        # ---------------------------------------------------------
        data_dict = {}
        try:
            m = re.search(
                r"<!--\s*BEGIN\s+POLLING\s*--(.*?)--\s*END\s+POLLING\s*-->",
                text,
                re.DOTALL | re.IGNORECASE,
            )
            if m:
                body = m.group(1)

                # Grab all key|value|~ pairs, allowing value to contain newlines
                pairs = re.findall(r"\s*([A-Za-z0-9_]+)\|([\s\S]*?)\|\~", body)
                for k, v in pairs:
                    data_dict[k.strip().lower()] = v.strip()
        except Exception as e:
            logging.warning(f"Warning: Could not parse polling data for Snowbowl: {e}")

        # Helper: avoid assigning non-numeric junk into numeric-ish fields
        def _safe_num(val):
            if val is None:
                return None
            try:
                return self.clean_numeric(str(val))
            except Exception:
                return None

        def _safe_text(val):
            if val is None:
                return None
            s = str(val).strip()
            return s if s != "" else None

        # Apply polling keys if present
        if data_dict:
            # Depths
            bd = _safe_num(data_dict.get("surface_depth_base"))
            sd = _safe_num(data_dict.get("surface_depth_summit"))
            if bd is not None:
                data["base_depth"] = bd
            if sd is not None:
                data["summit_depth"] = sd

            # New snow totals
            # newstormtotal appears to be "New Snow" (overnight / storm)
            ns = _safe_num(data_dict.get("newstormtotal"))
            if ns is not None:
                data["snow_overnight"] = ns

            # 24h totals
            s24 = _safe_num(data_dict.get("24hourtotal"))
            if s24 is not None:
                data["snow_24h_base"] = s24
                data["snow_24h_summit"] = s24

            # Current base temp (numeric only)
            tbase = _safe_num(data_dict.get("current_temp_base"))
            if tbase is not None:
                data["temp_base"] = tbase

            # Current weather type is sometimes "Closed"
            wc = _safe_text(data_dict.get("current_weather_type"))
            if wc is not None:
                data["weather_current"] = wc

            # Operations (often blank when closed)
            lifts = _safe_num(data_dict.get("operations_lifts"))
            runs = _safe_num(data_dict.get("operations_trails"))
            if lifts is not None:
                data["lifts_open"] = int(lifts)
            if runs is not None:
                data["runs_open"] = int(runs)

            # Comments (can contain lots of newlines)
            c = _safe_text(data_dict.get("comments"))
            if c is not None:
                c = re.sub(r"\n{3,}", "\n\n", c).strip()
                data["comments"] = c

            # lastupdated (if present)
            raw_last = _safe_text(data_dict.get("lastupdated"))
            if raw_last:
                try:
                    parsed_date = datetime.strptime(raw_last, "%A %b %d, %Y %I:%M %p")
                    data["last_updated"] = parsed_date.strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    data["last_updated"] = raw_last

            data["data_source"] = "html_polling"

        # ---------------------------------------------------------
        # 2) Parse "Updated:" date from visible HTML (fallback)
        #    <center><b>Updated:</b> Monday Dec 15, 2025 04:56 AM</center>
        # ---------------------------------------------------------
        if not data.get("last_updated"):
            try:
                m = re.search(r"Updated:\s*</b>\s*([^<\r\n]+)", text, re.IGNORECASE)
                if m:
                    raw = m.group(1).strip()
                    try:
                        parsed_date = datetime.strptime(raw, "%A %b %d, %Y %I:%M %p")
                        data["last_updated"] = parsed_date.strftime("%Y-%m-%d %I:%M %p")
                    except Exception:
                        data["last_updated"] = raw
            except Exception as e:
                logging.warning(f"Snowbowl: error finding Updated: {e}")

        # ---------------------------------------------------------
        # 3) Parse the main Base/Summit table as fallback only
        #    Your previous logic was brittle and could mis-assign labels.
        #    We only fill fields that are still missing.
        # ---------------------------------------------------------
        try:
            # Find the table that contains headers "Snow Depth / New Snow / Temp / Wind"
            # then read rows where first cell is Base/Summit
            main_table = None
            for t in soup.find_all("table"):
                if "Snow Depth" in t.get_text(" ", strip=True) and "New Snow" in t.get_text(" ", strip=True):
                    main_table = t
                    break

            if main_table:
                for row in main_table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 5:
                        continue
                    label = cells[0].get_text(" ", strip=True).lower()

                    if "base" in label:
                        # columns: [Base, SnowDepth, NewSnow, Temp, Wind]
                        if data.get("base_depth") is None:
                            data["base_depth"] = _safe_num(cells[1].get_text(strip=True))
                        if data.get("snow_24h_base") in (None, "", 0, 0.0):
                            # Snowbowl's "New Snow" here is not necessarily 24h, but it matches the polling totals when present
                            v = _safe_num(cells[2].get_text(strip=True))
                            if v is not None:
                                data["snow_24h_base"] = v
                        if data.get("temp_base") is None:
                            v = _safe_num(cells[3].get_text(strip=True))
                            if v is not None:
                                data["temp_base"] = v
                        if data.get("wind_speed") is None:
                            v = _safe_num(cells[4].get_text(strip=True))
                            if v is not None:
                                data["wind_speed"] = v

                    if "summit" in label:
                        if data.get("summit_depth") is None:
                            data["summit_depth"] = _safe_num(cells[1].get_text(strip=True))
                        if data.get("snow_24h_summit") in (None, "", 0, 0.0):
                            v = _safe_num(cells[2].get_text(strip=True))
                            if v is not None:
                                data["snow_24h_summit"] = v
                        if data.get("temp_summit") is None:
                            v = _safe_num(cells[3].get_text(strip=True))
                            if v is not None:
                                data["temp_summit"] = v
                        if data.get("wind_speed") is None:
                            v = _safe_num(cells[4].get_text(strip=True))
                            if v is not None:
                                data["wind_speed"] = v

            # Surface conditions via checkmarks (still optional; Snowbowl often blanks these)
            if not data.get("conditions_surface"):
                conditions = []
                for row in soup.find_all("tr"):
                    t = row.get_text(" ", strip=True)
                    if not t:
                        continue
                    if any(
                        cond in t
                        for cond in [
                            "Powder:",
                            "Packed Powder:",
                            "Packed:",
                            "Machine Groomed:",
                            "Firm Packed:",
                            "Spring Snow:",
                        ]
                    ):
                        check_cell = row.find("img", {"src": re.compile(r"check\.gif$", re.I)})
                        if check_cell:
                            # Use the left cell label text without trailing colon
                            label = t.split(":")[0].strip()
                            conditions.append(label)

                if conditions:
                    data["conditions_surface"] = ", ".join(conditions)

        except Exception as e:
            logging.warning(f"Snowbowl: Error parsing HTML table data: {e}")

        # Final fallback for data_source
        data["data_source"] = data.get("data_source") or "html_static"
        return data



class DiscoveryScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Discovery")

    def _clean_numeric(self, value):
        if not value:
            return None
        try:
            cleaned = re.sub(r'[^\d.-]', '', str(value))
            return cleaned if cleaned else None
        except:
            return None

    def get_url(self):
        return "https://www.skidiscovery.com/snow-report/"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        updated = soup.find('span', class_='tablinks')
        if updated:
            try:
                date_str = updated.text.replace('Updated:', '').strip()
                date_str = date_str.replace(' at ', ' ')
                parsed_date = datetime.strptime(date_str, '%B %d, %Y %I:%M %p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError:
                data['last_updated'] = updated.text.replace('Updated:', '').strip()
        
        primary_condition = None
        secondary_condition = None
        
        rows = soup.find_all('div', class_='row')
        for row in rows:
            label = row.find('p')
            if not label:
                continue
                
            label_text = label.text.strip()
            value_elem = row.find('p', class_='little-text')
            if value_elem:
                value = value_elem.text.strip()
                
                if label_text == 'New Snow (24 hours)':
                    data['snow_24h_base'] = self.clean_numeric(value)
                elif label_text == 'New Snow (48 hours)':
                    data['snow_48h_base'] = self._clean_numeric(value)
                elif label_text == 'New Snow (since lifts closed)':
                    data['snow_overnight'] = self._clean_numeric(value)
                elif label_text == 'Snow Depth - Bottom':
                    data['base_depth'] = self._clean_numeric(value)
                elif label_text == 'Snow Depth - Top':
                    data['summit_depth'] = self._clean_numeric(value)
                elif label_text == 'Current Temperature':
                    data['temp_base'] = self._clean_numeric(value)
                elif label_text == 'Current Wind':
                    if value.lower() != 'no':
                        data['wind_speed'] = self.clean_numeric(value)
                elif label_text == 'Surface Conditions (Primary)':
                    primary_condition = value
                elif label_text == 'Surface Conditions (Secondary)':
                    secondary_condition = value
                elif label_text == 'Lifts Open':
                    data['lifts_open'] = value
                elif label_text == 'Trails Open':
                    data['runs_open'] = value
                elif label_text == 'Current Weather':
                    data['weather_current'] = value
        
        if primary_condition and secondary_condition:
            data['conditions_surface'] = f"{primary_condition}, {secondary_condition}".strip(', ')
        elif primary_condition:
            data['conditions_surface'] = primary_condition
        
        return data

class LookoutPassScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("LookoutPass")

    def get_url(self):
        return "https://skilookout.com/snow-report"

    def _extract_section_text(self, header_string: str, soup: BeautifulSoup) -> str:
        """
        Find the heading whose text == header_string, then collect text from its
        following siblings until the next heading of same or higher level.
        """
        header = soup.find(
            lambda tag: (
                tag.name in ["h2", "h3", "h4", "h5", "h6"]
                and tag.get_text(strip=True).lower() == header_string.lower()
            )
        )
        if not header:
            return ""

        texts = []
        for sib in header.find_next_siblings():
            if sib.name in ["h2", "h3", "h4", "h5", "h6"]:
                break
            txt = sib.get_text(" ", strip=True)
            if txt:
                texts.append(txt)
        return "\n".join(texts)

    def _clean_number_or_fraction(self, s: str):
        """
        Handles:
          - "1", "1.5", "9 mph", "29°F", '1″'
          - "1/2", "1/2″"
          - "½", "¼", "¾"
        Returns float or None.
        """
        if s is None:
            return None
        t = str(s).strip()

        # normalize common unicode fractions
        t = (
            t.replace("½", "1/2")
             .replace("¼", "1/4")
             .replace("¾", "3/4")
        )

        # remove units we commonly see
        t = t.replace("mph", "").replace("°F", "").replace("F", "")
        t = t.replace("″", "").replace('"', "")
        t = t.strip()

        # fraction like 1/2
        m = re.search(r"(-?\d+)\s*/\s*(\d+)", t)
        if m:
            num = float(m.group(1))
            den = float(m.group(2))
            if den != 0:
                return num / den
            return None

        # fall back to existing numeric cleaner
        return self.clean_numeric(t)

    def _extract_block(self, full_text: str, start_pat: str, end_pat: str = None) -> str:
        """
        Extract substring after start_pat up to end_pat (or end of text).
        start_pat/end_pat are regex patterns.
        """
        m = re.search(start_pat, full_text, flags=re.IGNORECASE)
        if not m:
            return ""
        start = m.end()
        if end_pat:
            n = re.search(end_pat, full_text[start:], flags=re.IGNORECASE)
            if n:
                return full_text[start:start + n.start()].strip()
        return full_text[start:].strip()

    def _extract_value(self, text_block: str, label: str):
        """
        Extracts value from lines like:
          '24h Snow: 1″'
          'Depth: 15″'
          'Wind Speed: 9 mph'
        """
        m = re.search(rf"{re.escape(label)}\s*:\s*([^\n]+)", text_block, flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1).strip()

    def process_data(self, response) -> Dict:
        soup = BeautifulSoup(response.text, "html.parser")
        data = self.blank_record()

        # Use normalized plain text for robust parsing
        full_text = soup.get_text("\n", strip=True)

        # ---------------------------------------------------------
        # 1) Last updated: "Last updated: December 23, 2025 at 6:05 PM, PST"
        # ---------------------------------------------------------
        m = re.search(r"Last updated:\s*(.+)", full_text, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # keep timezone suffix if present (PST) but try a parse without it
            raw_no_tz = re.sub(r",\s*(PST|PDT|MST|MDT|CST|CDT|EST|EDT)\s*$", "", raw, flags=re.I)
            try:
                raw_for_parse = raw_no_tz.replace(" at ", " ")
                parsed = datetime.strptime(raw_for_parse, "%B %d, %Y %I:%M %p")
                data["last_updated"] = parsed.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                data["last_updated"] = raw

        # ---------------------------------------------------------
        # 2) Summit block (between "Summit" and "Base")
        # ---------------------------------------------------------
        summit_block = self._extract_block(
            full_text,
            start_pat=r"\bSummit\b",
            end_pat=r"\bBase\b"
        )

        if summit_block:
            v24 = self._extract_value(summit_block, "24h Snow")
            v48 = self._extract_value(summit_block, "48h Snow")
            vdepth = self._extract_value(summit_block, "Depth")
            vtemp = self._extract_value(summit_block, "Temperature")

            if v24 is not None:
                data["snow_24h_summit"] = self._clean_number_or_fraction(v24)
            if v48 is not None:
                data["snow_48h_summit"] = self._clean_number_or_fraction(v48)
            if vdepth is not None:
                data["summit_depth"] = self._clean_number_or_fraction(vdepth)
            if vtemp is not None:
                data["temp_summit"] = self._clean_number_or_fraction(vtemp)

        # ---------------------------------------------------------
        # 3) Base block (between "Base" and "Weather & Conditions" or "Weather")
        # ---------------------------------------------------------
        base_block = self._extract_block(
            full_text,
            start_pat=r"\bBase\b",
            end_pat=r"\bWeather\s*&\s*Conditions\b|\bWeather\b"
        )

        if base_block:
            v24 = self._extract_value(base_block, "24h Snow")
            v48 = self._extract_value(base_block, "48h Snow")
            vdepth = self._extract_value(base_block, "Depth")
            vtemp = self._extract_value(base_block, "Temperature")

            if v24 is not None:
                data["snow_24h_base"] = self._clean_number_or_fraction(v24)
            if v48 is not None:
                data["snow_48h_base"] = self._clean_number_or_fraction(v48)
            if vdepth is not None:
                data["base_depth"] = self._clean_number_or_fraction(vdepth)
            if vtemp is not None:
                data["temp_base"] = self._clean_number_or_fraction(vtemp)

        # ---------------------------------------------------------
        # 4) Weather & Conditions (Conditions + Wind)
        # ---------------------------------------------------------
        wc_block = self._extract_block(
            full_text,
            start_pat=r"\bWeather\s*&\s*Conditions\b",
            end_pat=r"\bLift\s*&\s*Slope\s*Status\b|\bLift\b"
        )

        if wc_block:
            vcond = self._extract_value(wc_block, "Conditions")
            if vcond:
                data["weather_current"] = vcond

            vwind = self._extract_value(wc_block, "Wind Speed")
            if vwind is not None:
                data["wind_speed"] = self._clean_number_or_fraction(vwind)

            vprim = self._extract_value(wc_block, "Primary")
            vsec = self._extract_value(wc_block, "Secondary")
            if vprim and vsec:
                data["conditions_surface"] = f"{vprim}, {vsec}"
            elif vprim:
                data["conditions_surface"] = vprim

        # ---------------------------------------------------------
        # 5) Notes + Comments (keep your existing logic)
        # ---------------------------------------------------------
        notes_text = self._extract_section_text("Snow & Weather Notes", soup)
        comments_text = self._extract_section_text("Comments", soup)

        comment_parts = []
        if notes_text:
            comment_parts.append(normalize_text(notes_text))
        if comments_text:
            comment_parts.append(normalize_text(comments_text))
        if comment_parts:
            data["comments"] = "\n\n".join(comment_parts)

        # ---------------------------------------------------------
        # 6) snow_overnight (try to parse from notes: "1/2″ snow overnight")
        # ---------------------------------------------------------
        notes_for_overnight = (notes_text or "") + "\n" + (comments_text or "")
        m = re.search(r"([\d¼½¾\.]+(?:\s*/\s*\d+)?)\s*(?:\"|″)?\s*snow\s*overnight", notes_for_overnight, flags=re.I)
        if m:
            data["snow_overnight"] = self._clean_number_or_fraction(m.group(1))

        data["data_source"] = "html_static"
        return data



class BigMountainScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("BigMountain")

    def get_url(self):
        return "https://skiwhitefish.com/snowreport/"

    def _get_weather_data(self, soup):
        weather_data = {
            'temp_summit': None,
            'wind_speed': None
        }
        try:
            # 1. Try standard columns first
            temp_cols = soup.find_all('div', class_='wp-block-column')
            for col in temp_cols:
                text = col.get_text(strip=True)
                if 'Temperature' in text:
                    match = re.search(r'(-?\d+)', text)
                    if match: weather_data['temp_summit'] = match.group(1)
                elif 'Wind' in text:
                    match = re.search(r'@\s*(\d+(?:\.\d+)?)', text)
                    if match: weather_data['wind_speed'] = self.clean_numeric(match.group(1))
            
            # 2. Fallback: Search all H5/H4 tags for temps
            if not weather_data['temp_summit']:
                for tag in soup.find_all(['h4', 'h5', 'p']):
                    text = tag.get_text(strip=True)
                    if 'Temp' in text or 'degrees' in text:
                        match = re.search(r'(-?\d+)\s*degrees', text, re.I)
                        if match: 
                            weather_data['temp_summit'] = match.group(1)
                            break

        except Exception as e:
            print(f"Error getting weather data: {e}")
            
        return weather_data

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # --- 1. DATE PARSING ---
        for p_tag in soup.find_all('p'):
            strong_tag = p_tag.find('strong')
            if strong_tag:
                text = strong_tag.text
                if re.search(r'\w+,\s+\w+\.?\s+\d{1,2},\s+\d{4}', text):
                    try:
                        clean_date = text.replace('.', '').replace('  ', ' ').strip().rstrip(':')
                        if 'm' not in clean_date.lower(): clean_date += " 6:00 AM"
                        clean_date = re.sub(r'(\d)(am|pm)', r'\1 \2', clean_date, flags=re.I)
                        parsed_date = datetime.strptime(clean_date, '%A, %b %d, %Y %I:%M %p')
                        data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                        break
                    except:
                        data['last_updated'] = text

        # --- 2. EXTRACT DATA ---
        summit_cols = soup.find_all('div', class_='wp-block-column')
        for col in summit_cols:
            h5 = col.find('h5')
            p = col.find('p')
            
            if not h5 or not p:
                text = col.get_text(separator='|', strip=True)
                parts = text.split('|')
                if len(parts) >= 2:
                    val_candidate = parts[0]
                    label_candidate = parts[1]
                else:
                    continue
            else:
                val_candidate = h5.get_text(strip=True)
                label_candidate = p.get_text(strip=True)

            val_clean = val_candidate.replace('"', '').replace('"', '').strip()
            
            if 'Overnight Fresh' in label_candidate:
                data['snow_overnight'] = val_clean
            elif 'Past 24 Hours' in label_candidate:
                data['snow_24h_summit'] = self.clean_numeric(val_clean)
                data['snow_24h_base'] = self.clean_numeric(val_clean)
            elif 'Past 7 Days' in label_candidate:
                data['snow_7day'] = val_clean
            elif 'Settled Base' in label_candidate:
                data['base_depth'] = val_clean
                data['summit_depth'] = val_clean
            elif 'Season to Date' in label_candidate:
                if data['comments']:
                    data['comments'] += f" | Season Total: {val_clean}\""
                else:
                    data['comments'] = f"Season Total: {val_clean}\""

        # --- 3. WEATHER ---
        weather_data = self._get_weather_data(soup)
        if weather_data:
            data.update(weather_data)
        
        return data

"""
LOST TRAIL SCRAPER FIX v2 — February 2026
==========================================

CHANGES FROM v1:
- FIXED: Comments now correctly extracts the morning narrative by finding
  text between "Snow Report for..." heading and first data label
- FIXED: All numeric fields now go through clean_numeric() so Firebase
  stores them as numbers (31) not strings ("31")
- FIXED: Fallback heuristic excluded to avoid grabbing webcam blurb

Replace the entire LostTrailScraper class in snow_report_scraper_oauth.py
"""


class LostTrailScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("LostTrail")

    def get_url(self):
        return "https://losttrail.com/snow-report/"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # ---------------------------------------------------------------
        # 1) LAST UPDATED — "Snow Report for February 13, 2026 6:00 am"
        # ---------------------------------------------------------------
        last_updated_element = soup.find(
            'div',
            class_='elementor-heading-title elementor-size-default',
            string=re.compile(r'Snow Report for', re.I)
        )
        
        if last_updated_element:
            last_updated_text = last_updated_element.get_text(strip=True)
            match = re.search(r'Snow Report for (.+)', last_updated_text)
            if match:
                try:
                    date_str = match.group(1).strip()
                    parsed_date = datetime.strptime(date_str, "%B %d, %Y %I:%M %p")
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                except ValueError as e:
                    print(f"Error parsing last updated date for Lost Trail: {e}")
        
        # ---------------------------------------------------------------
        # 2) COMMENTS — Morning narrative paragraph
        #
        #    Strategy: The narrative is the text block that appears AFTER
        #    the "Snow Report for..." heading and BEFORE the first data
        #    label ("New Snow (since lifts closed)").
        #
        #    We find the heading element, then walk forward through
        #    sibling/cousin elements looking for a substantial text block
        #    that does NOT match a known data label.
        # ---------------------------------------------------------------
        comments_text = None
        
        # Known data labels to skip (these are NOT the narrative)
        data_labels = {
            "new snow", "week total", "temperature", "weather",
            "season snowfall", "snow depth", "surface conditions",
            "number of lifts", "trails open", "terrain parks",
            "chair 1", "chair 2", "chair 3", "chair 4", "chair 5",
            "lift report", "terrain report", "web cam",
            "take a look at our mountainside camera",
        }
        
        if last_updated_element:
            # Get the parent widget of the heading
            heading_widget = last_updated_element.find_parent(
                'div', class_=lambda c: c and any('elementor-widget' in x for x in c) if c else False
            )
            
            if heading_widget:
                # Walk through the next sibling widgets
                for sib in heading_widget.find_next_siblings():
                    # Get all text widgets within this sibling
                    text_containers = sib.find_all('div', class_='elementor-widget-container')
                    if not text_containers:
                        text_containers = [sib]
                    
                    for container in text_containers:
                        candidate = container.get_text(strip=True)
                        if not candidate or len(candidate) < 40:
                            continue
                        
                        # Skip if it matches a known data label
                        candidate_lower = candidate.lower()
                        if any(label in candidate_lower for label in data_labels):
                            continue
                        
                        # Skip if it looks like navigation or boilerplate
                        if candidate_lower.startswith(('home', 'skip to', 'mountain', 'ski & ride')):
                            continue
                        
                        # This should be the morning narrative
                        comments_text = candidate
                        break
                    
                    if comments_text:
                        break
        
        # Second approach: use full page text and extract between heading and first label
        if not comments_text:
            full_text = soup.get_text('\n', strip=True)
            # Find text between "Snow Report for..." line and "New Snow" line
            m = re.search(
                r'Snow Report for .+?\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm)\s*\n(.+?)\nNew Snow',
                full_text,
                re.IGNORECASE | re.DOTALL
            )
            if m:
                comments_text = m.group(1).strip()
        
        if comments_text:
            data['comments'] = normalize_text(comments_text)
        
        # ---------------------------------------------------------------
        # 3) SNOW DATA — label-value pairs
        # ---------------------------------------------------------------
        labels = {
            "New Snow (since lifts closed)": "snow_overnight",
            "New Snow, 24 hours": "snow_24h_base",
            "New Snow, 48 hours": "snow_48h_base",
            "Week Total Snow": "snow_7day",
            "Snow Depth - Bottom": "base_depth",
            "Snow Depth - Top": "summit_depth",
        }
        
        for label, key in labels.items():
            element = soup.find(string=re.compile(re.escape(label), re.I))
            if element:
                next_sibling = element.find_next("span", class_="elementor-heading-title")
                if next_sibling:
                    raw = next_sibling.text.strip().replace('"', '').replace('\u2033', '')
                    # FIX: Convert to numeric so Firebase stores as number, not string
                    data[key] = self.clean_numeric(raw)
        
        # Temperature — "18°" → 18
        temp_element = soup.find(string=re.compile(r'^Temperature$', re.I))
        if temp_element:
            temp_val = temp_element.find_next("span", class_="elementor-heading-title")
            if temp_val:
                temp_match = re.search(r'(-?\d+)', temp_val.text)
                if temp_match:
                    data['temp_base'] = self.clean_numeric(temp_match.group(1))
        
        # Weather — "Partly Sunny"
        weather_element = soup.find(string=re.compile(r'^Weather$', re.I))
        if weather_element:
            weather_val = weather_element.find_next("span", class_="elementor-heading-title")
            if weather_val:
                data['weather_current'] = weather_val.text.strip()

        # ---------------------------------------------------------------
        # 4) SURFACE CONDITIONS
        # ---------------------------------------------------------------
        primary_condition = soup.find(string=re.compile(r"Surface Conditions \(Primary\)", re.I))
        secondary_condition = soup.find(string=re.compile(r"Surface Conditions \(Secondary\)", re.I))
        if primary_condition:
            primary_text = primary_condition.find_next("span").text.strip()
            if secondary_condition:
                secondary_text = secondary_condition.find_next("span").text.strip()
                data['conditions_surface'] = f"{primary_text}, {secondary_text}"
            else:
                data['conditions_surface'] = primary_text
        
        # ---------------------------------------------------------------
        # 5) LIFTS & TRAILS
        # ---------------------------------------------------------------
        lifts_section = soup.find(string=re.compile("Number of Lifts Open", re.I))
        if lifts_section:
            lifts_value = lifts_section.find_next("span", class_="elementor-heading-title")
            if lifts_value:
                data['lifts_open'] = lifts_value.text.strip()
        
        trails_section = soup.find(string=re.compile(r"^Trails Open$", re.I))
        if trails_section:
            trails_value = trails_section.find_next("span", class_="elementor-heading-title")
            if trails_value:
                data['runs_open'] = trails_value.text.strip()
        
        # ---------------------------------------------------------------
        # 6) SEASON TOTAL — append to comments
        # ---------------------------------------------------------------
        season_element = soup.find(string=re.compile(r'^Season Snowfall$', re.I))
        if season_element:
            season_val = season_element.find_next("span", class_="elementor-heading-title")
            if season_val:
                season_text = season_val.text.strip()
                if data['comments']:
                    data['comments'] += f" | Season Total: {season_text}"
                else:
                    data['comments'] = f"Season Total: {season_text}"

        return data


class TetonPassScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("TetonPass")

    def get_url(self):
        return "https://www.skitetonmt.com/mountain-info/current-conditions"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        updated_elem = soup.find('p', class_='CDt4Ke', string=lambda text: 'Updated' in str(text) if text else False)
        if updated_elem:
            try:
                date_str = updated_elem.text.replace('Updated', '').strip()
                parsed_date = datetime.strptime(date_str, '%m/%d/%Y %I:%M%p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError:
                data['last_updated'] = updated_elem.text.replace('Updated', '').strip()
        
        paragraphs = soup.find_all('p', class_='CDt4Ke')
        for p in paragraphs:
            text = p.get_text().strip()
            
            if "New Snow:" in text:
                snow_match = re.search(r'(\d+)\"', text)
                if snow_match:
                    data['snow_24h_base'] = self.clean_numeric(snow_match.group(1))
            
            elif "Settled Base" in text:
                depth_match = re.search(r'(\d+)\"', text)
                if depth_match:
                    if "mid-mountain" in text:
                        data['base_depth'] = depth_match.group(1)
                    elif "top of lift" in text:
                        data['summit_depth'] = depth_match.group(1)
            
            elif "Temperature:" in text:
                temp_match = re.search(r'(-?\d+)\s*degrees', text)
                if temp_match:
                    data['temp_base'] = temp_match.group(1)
            
            elif "Conditions:" in text:
                data['conditions_surface'] = text.replace('Conditions:', '').strip()
            
            elif "Wind:" in text:
                data['wind_speed'] = self.clean_numeric(text.replace('Wind:', '').strip())
        
        return data

class ShowdownScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Showdown")

    def get_url(self):
        return "https://www.showdownmontana.com/snow-report"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        time_str = None
        large_paragraphs = soup.find_all('p', class_='sqsrte-large')
        for p in large_paragraphs:
            if 'Last Update' in p.text:
                time_str = p.text.strip()
                break
        
        if time_str:
            try:
                date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', time_str)
                if date_match:
                    date_str = date_match.group(1)
                    parsed_date = datetime.strptime(date_str, '%m/%d/%Y')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d') + ' 06:00 AM'
            except Exception as e:
                print(f"Error parsing Showdown date: {e}")
                data['last_updated'] = time_str
        
        snow_blocks = soup.find_all('h2', style=lambda value: value and 'white-space:pre-wrap;' in value)
        for block in snow_blocks:
            value = block.get_text(strip=True)
            if not value:
                continue
            
            sibling = block.find_next_sibling('p', class_='sqsrte-large')
            if sibling:
                label = sibling.get_text(strip=True)
                if 'Base' in label and 'elv. 6,800' in label:
                    data['base_depth'] = value.replace('"', '')
                elif 'Summit' in label and 'elv. 8,200' in label:
                    data['summit_depth'] = value.replace('"', '')
                elif 'Last 24 Hour' in label:
                    data['snow_24h_base'] = self.clean_numeric(value)
                elif 'Overnight' in label:
                    data['snow_overnight'] = value.replace('"', '')
        
        text_blocks = soup.find_all('p', class_='sqsrte-large')
        for p in text_blocks:
            text = p.get_text(strip=True)
            
            if 'Current Temperature:' in text:
                temp_match = re.search(r'(-?\d+)', text)
                if temp_match:
                    data['temp_base'] = temp_match.group(1)
            
            elif 'Trails Open:' in text:
                data['runs_open'] = text.replace('Trails Open:', '').strip()
            
            elif 'Lifts Open:' in text:
                data['lifts_open'] = text.replace('Lifts Open:', '').strip()
            
            elif 'Primary Surface:' in text:
                primary_surface = text.replace('Primary Surface:', '').strip()
                secondary_block = p.find_next('p', class_='sqsrte-large')
                if secondary_block and 'Secondary Surface:' in secondary_block.get_text():
                    secondary_surface = secondary_block.get_text().replace('Secondary Surface:', '').strip()
                    data['conditions_surface'] = f"{primary_surface}, {secondary_surface}"
                else:
                    data['conditions_surface'] = primary_surface
            
            elif "Today's forecast:" in text:
                data['comments'] = text.replace("Today's forecast:", '').strip()
        
        return data

class BlacktailScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Blacktail")

    def get_url(self):
        return "https://blacktailmountain.com/snow-report/"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        article_date = None
        article_header = soup.find('div', class_='byline')
        if article_header:
            reported_h3 = article_header.find('h3')
            if reported_h3 and 'Reported:' in reported_h3.text:
                date_match = re.search(r'\d{2}/\d{2}/\d{2}', reported_h3.text)
                if date_match:
                    try:
                        article_date = datetime.strptime(date_match.group(), '%m/%d/%y').strftime('%Y-%m-%d')
                    except ValueError:
                        pass
        
        snow_sidebar = soup.find('ul', class_='reports')
        if snow_sidebar:
            list_items = snow_sidebar.find_all('li')
            update_time = None
            for item in list_items:
                strong_text = item.find('strong')
                if strong_text:
                    label = strong_text.get_text(strip=True).rstrip(':')
                    value = item.get_text(strip=True).replace(strong_text.get_text(strip=True), '').strip()
                    
                    if label == 'Updated':
                        update_time = value
                        if article_date and update_time:
                            time_str = update_time.lower().replace('am', ' AM').replace('pm', ' PM').strip()
                            data['last_updated'] = f"{article_date} {time_str}"
                    elif label == 'Current Temp':
                        temp_match = re.search(r'(-?\d+\.?\d*)', value)
                        if temp_match:
                            data['temp_base'] = temp_match.group(1)
                    elif label == 'New Snow Overnight':
                        data['snow_overnight'] = value.replace('"', '').strip()
                    elif label == 'New Snow 24 Hours':
                        data['snow_24h_base'] = self.clean_numeric(value)
                    elif label == 'Summit Depth':
                        data['summit_depth'] = value.replace('"', '').strip()
                    elif label == 'Wind':
                        data['wind_speed'] = self.clean_numeric(value)
        
        grooming_sidebar = soup.find('div', id='groomingSidebar')
        if grooming_sidebar:
            list_items = grooming_sidebar.find_all('li')
            for item in list_items:
                strong_text = item.find('strong')
                if strong_text:
                    label = strong_text.get_text(strip=True).rstrip(':')
                    value = item.get_text(strip=True).replace(strong_text.get_text(strip=True), '').strip()
                    
                    if label == 'Number of lifts in operation':
                        data['lifts_open'] = value.strip()
                    elif label == 'Number of trails groomed':
                        data['runs_open'] = value.strip()
        
        entry_content = soup.find('div', class_='entry-content')
        if entry_content:
            data['comments'] = entry_content.get_text(strip=True)
        
        return data




class SilverMountainScraper(BaseSnowReportScraper):
    """
    Silver Mountain scraper - calls their API directly (no Playwright needed)
    Tested and working as of Nov 2025
    Date format fixed to match other scrapers
    """
    
    def __init__(self):
        super().__init__("SilverMountain")
        self.BASE_URL = "https://www.silvermt.com"
    
    def get_url(self):
        return f"{self.BASE_URL}/mountain-info/expanded-conditions"
    
    def get_token(self) -> Optional[str]:
        """Get API token from Silver Mountain"""
        try:
            url = f"{self.BASE_URL}/api/access_token"
            params = {'xid': '6c153b1f29c65c918831da5de0e73d2e0aca6f2a'}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.text.strip('"')
        except Exception as e:
            print(f"Silver Mountain: Error getting token: {e}")
            return None
    
    def process_data(self, response) -> Dict:
        """
        Get Silver Mountain data from their APIs.
        Calls 2 endpoints:
          1. /api - weather/bulletin
          2. /api/snow-api - snow depths
        """
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        try:
            # Get token
            token = self.get_token()
            if not token:
                print("Silver Mountain: Failed to get API token")
                return data
            
            # Call API #1: Weather/Bulletin data
            weather_url = f"{self.BASE_URL}/api"
            weather_params = {'token': token}
            
            weather_response = requests.get(weather_url, params=weather_params, timeout=10)
            weather_response.raise_for_status()
            weather_data = weather_response.json()
            
            # Call API #2: Snow data
            snow_url = f"{self.BASE_URL}/api/snow-api"
            snow_params = {
                'xid': '6c153b1f29c65c918831da5de0e73d2e0aca6f2a',
                'token': token
            }
            
            snow_response = requests.get(snow_url, params=snow_params, timeout=10)
            snow_response.raise_for_status()
            snow_data = snow_response.json()
            
            # Parse weather/bulletin data
            if weather_data.get('content', {}).get('resorts'):
                resort = weather_data['content']['resorts'][0]
                
                # Bulletin - strip HTML tags
                if resort.get('weatherSkyInfos'):
                    for bulletin_obj in resort['weatherSkyInfos']:
                        if bulletin_obj.get('type') == 'WEATHER_BULLETIN_OF_THE_DAY':
                            bulletin_html = bulletin_obj['data'][0]['data']
                            # Strip HTML tags
                            bulletin_text = re.sub(r'<[^>]+>', ' ', bulletin_html)
                            bulletin_text = re.sub(r'\s+', ' ', bulletin_text).strip()
                            data['comments'] = bulletin_text
                
                # Last updated timestamp - FORMAT TO MATCH OTHER SCRAPERS
                weather_zones = resort.get('weatherZones', [])
                if len(weather_zones) > 1:
                    summit_zone = weather_zones[1]
                    if summit_zone.get('weatherInfos'):
                        last_updated_raw = summit_zone['weatherInfos'][0].get('lastModified')
                        if last_updated_raw:
                            try:
                                # Parse ISO 8601 format: 2025-11-20T23:37:00.000-07:00
                                parsed = datetime.fromisoformat(last_updated_raw.replace('Z', '+00:00'))
                                # Format to match Showdown: 2025-11-20 03:17 PM
                                data['last_updated'] = parsed.strftime('%Y-%m-%d %I:%M %p')
                            except Exception as e:
                                logging.warning(f"Silver Mountain: Could not parse date '{last_updated_raw}': {e}")
                                data['last_updated'] = last_updated_raw
            
            # Parse snow data
            if snow_data.get('content', {}).get('resorts'):
                snow_zones = snow_data['content']['resorts'][0]['snowZones']
                
                # Base/Lodge (snowZones[0])
                lodge = snow_zones[0]
                data['snow_overnight'] = self.clean_numeric(lodge['freshSnowFallDepth12H']['countryValue'])
                data['snow_24h_base'] = self.clean_numeric(lodge['freshSnowFallDepth24H']['countryValue'])
                data['snow_48h_base'] = self.clean_numeric(lodge['freshSnowFallDepth48H']['countryValue'])
                data['base_depth'] = self.clean_numeric(lodge['snowTotalDepth']['countryValue'])
                data['conditions_surface'] = lodge.get('snowQuality')  # Pass through as-is (e.g., "EARLY_SEASON")
                
                # Summit (snowZones[1])
                summit = snow_zones[1]
                data['snow_24h_summit'] = self.clean_numeric(summit['freshSnowFallDepth24H']['countryValue'])
                data['snow_48h_summit'] = self.clean_numeric(summit['freshSnowFallDepth48H']['countryValue'])
                data['summit_depth'] = self.clean_numeric(summit['snowTotalDepth']['countryValue'])
            
        except Exception as e:
            print(f"Silver Mountain: Error processing data: {e}")
            import traceback
            traceback.print_exc()
        
        return data



class SchweitzerScraper(BaseSnowReportScraper):
    """
    Schweitzer Mountain scraper - uses mtnpowder.com API
    Tested and working as of Nov 2025
    Date format fixed to match other scrapers
    """
    
    def __init__(self):
        super().__init__("Schweitzer")
        self.base_url = "https://mtnpowder.com/feed/v3.json"
        self.bearer_token = "KfmBnMqN4SP3Y4A3Qfy0achT8VGoWOfQtyRm7jwgQFE"
        self.resort_id = 168
    
    def get_url(self):
        """Return base URL (not used directly, but required by base class)"""
        return self.base_url
    
    def strip_html(self, html_text):
        """Strip HTML tags and clean up text"""
        if not html_text or html_text == "--":
            return None
        
        from html import unescape
        # Unescape HTML entities
        text = unescape(html_text)
        
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        
        return text if text else None
    
    def safe_get(self, value):
        """Convert '--' or empty strings to None"""
        if value == "--" or value is None or value == "":
            return None
        return value
    
    def get_snow_report(self) -> Optional[Dict]:
        """
        Override base class method to call Schweitzer API directly.
        Bearer token is passed as query parameter, not header.
        """
        try:
            # Bearer token is passed as query parameter
            params = {
                "bearer_token": self.bearer_token,
                "resortId[]": self.resort_id  # Note the brackets!
            }
            
            logging.info(f"Fetching Schweitzer data from mtnpowder.com API...")
            response = requests.get(
                self.base_url,
                params=params,
                timeout=30
            )
            response.raise_for_status()
            
            data = self.process_data(response)
            
            if data:
                # Run the Universal Staleness Check
                data = self._check_and_fix_staleness(data)
                
                data.update({
                    'resort': self.resort_name,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
            
            return data
            
        except Exception as e:
            logging.error(f"Error fetching Schweitzer snow report: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def process_data(self, response) -> Dict:
        """Parse mtnpowder.com API response"""
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        try:
            api_data = response.json()
            
            # Navigate to snow report data
            resort = api_data.get('Resorts', [{}])[0]
            snow_report = resort.get('SnowReport', {})
            
            # Extract area data
            base_area = snow_report.get('BaseArea', {})
            summit_area = snow_report.get('SummitArea', {})
            
            # Map snowfall data
            data['snow_overnight'] = self.safe_get(base_area.get('SinceLiftsClosedIn'))
            data['snow_24h_base'] = self.safe_get(base_area.get('Last24HoursIn'))
            data['snow_48h_base'] = self.safe_get(base_area.get('Last48HoursIn'))
            data['base_depth'] = self.safe_get(base_area.get('BaseIn'))
            
            data['snow_24h_summit'] = self.safe_get(summit_area.get('Last24HoursIn'))
            data['snow_48h_summit'] = self.safe_get(summit_area.get('Last48HoursIn'))
            data['summit_depth'] = self.safe_get(summit_area.get('BaseIn'))
            
            # Conditions
            data['conditions_surface'] = self.safe_get(snow_report.get('BaseConditions'))
            data['comments'] = self.strip_html(snow_report.get('Report', ''))
            
            # Lifts and runs
            total_lifts = snow_report.get('TotalLifts', 0)
            open_lifts = snow_report.get('TotalOpenLifts', 0)
            total_trails = snow_report.get('TotalTrails', 0)
            open_trails = snow_report.get('TotalOpenTrails', 0)
            
            data['lifts_open'] = f"{open_lifts} / {total_lifts}"
            data['runs_open'] = f"{open_trails} / {total_trails}"
            
            # Operating status
            data['weather_current'] = resort.get('OperatingStatus', '')
            
            # Last update - FORMAT TO MATCH OTHER SCRAPERS
            last_updated_raw = self.safe_get(snow_report.get('LastUpdate'))
            if last_updated_raw:
                try:
                    # Parse ISO 8601 format: 2025-11-12T15:17:22-0800
                    parsed = datetime.fromisoformat(last_updated_raw.replace('Z', '+00:00'))
                    # Format to match Showdown: 2025-11-20 03:17 PM
                    data['last_updated'] = parsed.strftime('%Y-%m-%d %I:%M %p')
                except Exception as e:
                    logging.warning(f"Schweitzer: Could not parse date '{last_updated_raw}': {e}")
                    data['last_updated'] = last_updated_raw
            else:
                data['last_updated'] = None
            
            # Clean numeric values
            for key in ['snow_overnight', 'snow_24h_base', 'snow_48h_base', 
                        'snow_24h_summit', 'snow_48h_summit', 'base_depth', 'summit_depth']:
                if data[key] is not None:
                    data[key] = self.clean_numeric(data[key])
            
            return data
            
        except Exception as e:
            logging.error(f"Error parsing Schweitzer data: {e}")
            import traceback
            traceback.print_exc()
            return data


class BridgerBowlScraper(BaseSnowReportScraper):
    """
    Bridger Bowl scraper - uses a GraphQL POST request to fetch the snow report data.
    The required query was identified via network inspection.
    This version corrects the base_depth/seasonal_snowfall mapping based on testing.
    """
    
    def __init__(self):
        super().__init__("BridgerBowl")
        self.BASE_URL = "https://api.bridgerbowl.com/graphql"

    # The GraphQL query payload identified from the network logs.
    GRAPHQL_PAYLOAD = {
        "operationName": "Query",
        "variables": {},
        "query": """query Query {
          snow_report {
            new_snow
            recent_snow
            seasonal_snow
            settled_snow
            snow_density
            icon
            blue_light
            conditions_primary
            conditions_secondary
            __typename
          }
        }"""
    }

    def get_url(self) -> str:
        """Returns the primary API endpoint URL for this resort."""
        return self.BASE_URL

    def get_snow_report(self) -> Optional[Dict]:
        """
        Overrides the base method to execute the GraphQL POST request.
        """
        try:
            logging.info(f"Fetching {self.resort_name} data from GraphQL API...")

            headers = {
                'Content-Type': 'application/json',
                **self.headers 
            }
            
            # The POST request sends the GraphQL query as the JSON body
            response = requests.post(
                self.BASE_URL,
                headers=headers,
                json=self.GRAPHQL_PAYLOAD,
                timeout=30
            )
            response.raise_for_status()
            
            data = self.process_data(response)
            
            # Add core metadata (resort, date, timestamp, data_source)
            if data:
                # Run the Universal Staleness Check
                data = self._check_and_fix_staleness(data)
                data = self._sanitize_text_fields(data)
                
                data.update({
                    'resort': self.resort_name,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'data_source': 'api_graphql',
                })
            return data
            
        except Exception as e:
            logging.error(f"Error fetching {self.resort_name} data (GraphQL): {e}", exc_info=True)
            return None

    def process_data(self, response) -> Dict:
        """Parses the GraphQL JSON response into the STANDARD_COLUMNS schema."""
        data = self.blank_record()
        
        try:
            api_data = response.json()
            snow_report = api_data.get('data', {}).get('snow_report', {})
            
            primary = snow_report.get('conditions_primary')
            
            # 1. Snowfall and Depth Mapping
            data['snow_overnight'] = self.clean_numeric(snow_report.get('new_snow'))
            data['snow_24h_base'] = self.clean_numeric(snow_report.get('recent_snow')) 
            
            # FIX: Map the high, incorrect "base_depth" (settled_snow) to seasonal_snowfall
            # and map the true seasonal total to a new column or drop it, since the numbers are confusing.
            # We'll prioritize the current Base Depth to be 0 if the resort is closed,
            # and set the high number (e.g., 65") to the seasonal total.
            
            # Based on the error, "settled_snow" (65) is the total S.T.D.
            data['seasonal_snowfall'] = self.clean_numeric(snow_report.get('settled_snow'))
            
            # Set base depth to 0 if closed, otherwise use 0 (since GraphQL doesn't specify top/bottom depth well)
            if primary and 'Closed' in primary:
                 data['base_depth'] = 0.0
                 data['summit_depth'] = 0.0
            else:
                 # If open, default to a safe value or None, as the API structure is ambiguous
                 data['base_depth'] = self.clean_numeric(snow_report.get('settled_snow')) if snow_report.get('settled_snow') < 100 else 0.0
            
            # 2. Conditions and Comments
            secondary = snow_report.get('conditions_secondary')
            
            data['conditions_surface'] = primary
            
            comment_parts = []
            if primary:
                 comment_parts.append(f"Primary Condition: {primary}")
            if secondary:
                 comment_parts.append(f"Secondary Condition: {secondary}")
            
            data['comments'] = " | ".join(comment_parts) if comment_parts else None
            
            # 3. Last Updated (Using fetch time)
            data['last_updated'] = datetime.now().strftime('%Y-%m-%d %I:%M %p')
            
        except Exception as e:
            logging.error(f"{self.resort_name}: Error parsing data: {e}", exc_info=True)
            return data
            
        return data




class RedLodgeScraper(BaseSnowReportScraper):
    """
    Red Lodge Mountain scraper - scrapes snow report data directly from the
    static HTML generated by their custom WordPress plugin.
    
    FINAL FIX: Uses simple text extraction and robust date parsing logic.
    """
    
    def __init__(self):
        super().__init__("RedLodge")

    def get_url(self) -> str:
        """Return the URL to scrape for Red Lodge Mountain."""
        return "https://www.redlodgemountain.com/mountain/snow-report/"

    def _get_area_data(self, soup, area_name: str) -> Dict[str, str]:
        """Helper to extract data (snowfall, temp, depth) from a specific area block."""
        
        # Find the specific area wrapper (Base or Mid-Mountain)
        area_wrapper = soup.find('div', class_='area-wrapper', 
                                 string=lambda text: text and area_name in text)
        
        data = {}
        if not area_wrapper:
            return data

        # Define data point mappings based on HTML classes (Remains stable)
        mappings = {
            'snow_overnight': '.snow-12-hr',
            'snow_24h_base': '.snow-24-hr',
            'snow_48h_base': '.storm-total:nth-of-type(3)',
            'snow_7day': '.storm-total:nth-of-type(4)',
            'base_depth': '.base-depth',
            'temp_base': '.temp',
        }

        # Adjust keys based on the area being scraped
        if area_name == 'Mid-Mountain':
            mappings = {
                k.replace('_base', '_summit') if '_base' in k else k: v 
                for k, v in mappings.items()
            }
        
        # Extract values
        for key, selector in mappings.items():
            if 'temp' in key:
                 item_elem = area_wrapper.select_one(f"{selector} .temp")
            elif 'depth' in key or 'snow' in key:
                 item_elem = area_wrapper.select_one(selector)
            else:
                 item_elem = area_wrapper.select_one(selector)


            if item_elem:
                raw_text = item_elem.get_text(strip=True)
                value_match = re.search(r'(-?\d+\.?\d*)', raw_text)
                if value_match:
                    data[key] = value_match.group(1)
        
        return data

    def process_data(self, response) -> Dict:
        """Parses the Red Lodge HTML response."""
        soup = BeautifulSoup(response.text, 'html.parser')
        data = self.blank_record()
        
        # --- 1. Get Numeric Data (Still reliable) ---
        base_data = self._get_area_data(soup, 'Base')
        mid_data = self._get_area_data(soup, 'Mid-Mountain')
        
        data['snow_overnight'] = self.clean_numeric(base_data.get('snow_overnight'))
        data['snow_24h_base'] = self.clean_numeric(base_data.get('snow_24h_base'))
        data['snow_48h_base'] = self.clean_numeric(base_data.get('snow_48h_base'))
        data['snow_7day'] = self.clean_numeric(base_data.get('snow_7day'))
        data['base_depth'] = self.clean_numeric(base_data.get('base_depth'))
        data['temp_base'] = self.clean_numeric(base_data.get('temp_base'))

        data['snow_24h_summit'] = self.clean_numeric(mid_data.get('snow_24h_summit'))
        data['snow_48h_summit'] = self.clean_numeric(mid_data.get('snow_48h_summit'))
        data['summit_depth'] = self.clean_numeric(mid_data.get('base_depth'))
        data['temp_summit'] = self.clean_numeric(mid_data.get('temp_base'))

        # --- 2. FINAL FIX: Get Last Updated Date (Text-only search) ---
        # Search the entire HTML body text for the pattern "Daily Report for: [Date]"
        full_text = soup.body.get_text('\n', strip=True)
        date_match = re.search(r'Daily Report for:\s*(\w+,\s*(\w+\s*\d{1,2},\s*\d{4}))', full_text, re.IGNORECASE)
        
        if date_match:
            raw_date = date_match.group(2).strip() # Should be "November 22, 2025"
            try:
                parsed_date = datetime.strptime(raw_date, '%B %d, %Y')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d 06:00 AM')
            except Exception as e:
                logging.warning(f"Red Lodge: Could not parse date '{raw_date}': {e}")
                data['last_updated'] = raw_date
        else:
             data['last_updated'] = None

        # --- 3. FINAL FIX: Get Comments / Bulletin Text (Target the unique rescheduling text) ---
        comment_start_text = soup.find(string=re.compile(r'Due to unseasonably warm temperatures'))
        
        if comment_start_text:
             # Find the closest parent element that contains the whole paragraph of text
             announcement_p = comment_start_text.find_parent('p')
             if announcement_p:
                 # Capture all the text inside the main announcement paragraph
                 full_bulletin = announcement_p.get_text(strip=True)
                 data['comments'] = normalize_text(f"RESCHEDULE NOTICE: {full_bulletin}")
             else:
                 # Fallback if the parent paragraph is not found
                 data['comments'] = "Rescheduling announcement found, but extraction failed. See original page."
        else:
            # Fallback if the specific text isn't found (i.e., if resort is open)
            data['comments'] = "No major bulletin found."


        # --- 4. Get Current Weather ---
        current_weather_elem = soup.select_one('#current-weather .wpc-weather- .now .time_temperature + div')
        if current_weather_elem:
             data['weather_current'] = normalize_text(current_weather_elem.get_text(strip=True))

        # --- 5. Get Operations (Lifts/Runs Open) ---
        lifts_open = soup.select_one('.subhead.lifts + span.numerator')
        lifts_total = soup.select_one('.subhead.lifts + span.numerator + span.denominator')
        
        runs_open = soup.select_one('.subhead.runs + span.numerator')
        runs_total = soup.select_one('.subhead.runs + span.numerator + span.denominator')
        
        if lifts_open and lifts_total:
            data['lifts_open'] = f"{self.clean_numeric(lifts_open.text)} / {self.clean_numeric(lifts_total.text.strip('/').strip())}"
        
        if runs_open and runs_total:
            data['runs_open'] = f"{self.clean_numeric(runs_open.text)} / {self.clean_numeric(runs_total.text.strip('/').strip())}"

        data['conditions_surface'] = None 
        data['data_source'] = 'html_scrape'
        
        return data



### BIG SKY Scrapter  ###

class BigSkyScraper(BaseSnowReportScraper):
    """
    Big Sky scraper - uses Big Sky's ReportPal JSON API.
    Normalized to STANDARD_COLUMNS and integrated with the
    BaseSnowReportScraper staleness + CSV pipeline.
    """

    def __init__(self):
        super().__init__("BigSky")
        self.API_URL = "https://www.bigskyresort.com/api/reportpal?resortName=bs&useReportPal=true"

    def get_url(self) -> str:
        return self.API_URL

    def process_data(self, response) -> Dict:
        api = response.json()
        data = self.blank_record()

        now_local = datetime.now(tz=LOCAL_TZ)

        # -----------------------------
        # Last updated (API gives UTC)
        # -----------------------------
        updated_raw = api.get("updated")
        if updated_raw:
            # Example: "2025-12-03T17:07:32Z"
            try:
                dt_utc = datetime.strptime(updated_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=ZoneInfo("UTC")
                )
                dt_local = dt_utc.astimezone(LOCAL_TZ)
                data["last_updated"] = dt_local.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                # If parsing ever breaks, at least keep the raw string
                data["last_updated"] = updated_raw

        # -----------------------------
        # Core snow + operations data
        # -----------------------------
        current_conditions = api.get("currentConditions") or {}
        resortwide = current_conditions.get("resortwide") or {}
        resort_locations = current_conditions.get("resortLocations") or {}
        locations = resort_locations.get("location") or []
        location = locations[0] if locations else {}

        def _get_inches(branch: Optional[Dict], key: str) -> Optional[float]:
            if not branch:
                return None
            node = branch.get(key) or {}
            inches = node.get("inches")
            return self.clean_numeric(inches)

        # Snow amounts (all in inches)
        data["snow_overnight"] = _get_inches(location, "snowOverNight")
        data["snow_24h_base"] = _get_inches(location, "snow24Hours")
        data["snow_48h_base"] = _get_inches(location, "snow48Hours")
        data["snow_7day"] = _get_inches(location, "snow7Days")
        data["base_depth"] = _get_inches(location, "base")
        data["summit_depth"] = None  # API does not clearly separate summit depth

        # Lifts / runs (store as "open / total" like other scrapers)
        lifts_open = resortwide.get("numLiftsOpen")
        lifts_total = resortwide.get("numLiftsTotal")
        trails_open = resortwide.get("numTrailsOpen")
        trails_total = resortwide.get("numTrailsTotal")

        if lifts_open is not None and lifts_total is not None:
            data["lifts_open"] = f"{lifts_open} / {lifts_total}"
        if trails_open is not None and trails_total is not None:
            data["runs_open"] = f"{trails_open} / {trails_total}"

        # Status string → treat as current weather / operating status
        operations = api.get("operations") or {}
        resort_status = operations.get("resortStatus")
        if resort_status:
            data["weather_current"] = resort_status

        # -----------------------------
        # Comments (strip HTML from first comment block)
        # -----------------------------
        comments_block = api.get("comments") or {}
        comments_list = comments_block.get("comment") or []
        comment_html = ""
        for c in comments_list:
            text = (c or {}).get("text") or ""
            if text.strip():
                comment_html = text
                break

        if comment_html:
            # Remove simple HTML tags and normalize whitespace
            comment_clean = re.sub(r"<[^>]+>", " ", comment_html)
            comment_clean = re.sub(r"\s+", " ", comment_clean).strip()
            data["comments"] = comment_clean

        data["data_source"] = "api_reportpal"
        return data




"""
FIXED Great Divide Scraper - January 2026
Based on the new page structure at https://www.skigd.com/weather-and-conditions

Replace the entire GreatDivideScraper class (around line ~2800) with this version
"""

"""
REPLACEMENT FOR GreatDivideScraper CLASS
=========================================

Instructions:
1. Find the GreatDivideScraper class in snow_report_scraper_oauth.py
2. Replace the ENTIRE class (from "class GreatDivideScraper" to the line before "class MaverickScraper")
3. Paste this code in its place

This version uses Selenium to render the JavaScript-loaded Bubble.io content.
"""

class GreatDivideScraper(BaseSnowReportScraper):
    """
    Great Divide scraper - uses Playwright to render JavaScript from Bubble.io iframe:
    https://www.skigd.com/weather-and-conditions

    The page loads data dynamically via a Bubble.io iframe that requires JavaScript execution.
    Uses Playwright (replaced Selenium Jan 2026 → Oct 2026) for better GitHub Actions reliability.
    """
    def __init__(self):
        super().__init__("GreatDivide")

    def get_url(self) -> str:
        return "https://snow-conditions.bubbleapps.io/public_output"

    def get_snow_report(self) -> Optional[Dict]:
        """Override base method to use Playwright instead of requests."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logging.error(f"{self.resort_name}: playwright not installed. Run: pip install playwright && playwright install chromium")
            return None

        logging.info(f"{self.resort_name}: Launching headless Chromium via Playwright...")
        page_source = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(self.get_url(), wait_until='networkidle', timeout=30000)
                page_source = page.content()
                browser.close()
        except Exception as e:
            logging.error(f"{self.resort_name}: Playwright fetch failed: {e}")
            return None

        soup = BeautifulSoup(page_source, 'html.parser')
        data = self.process_data_from_soup(soup)

        # Store page source for potential LLM fallback
        self._last_page_html = page_source

        if data:
            data = self._check_and_fix_staleness(data)
            data.update({
                'resort': self.resort_name,
                'date': datetime.now().strftime('%Y-%m-%d'),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })

        logging.info(f"{self.resort_name}: Successfully scraped with Playwright")
        return data

    def process_data(self, response) -> Dict:
        """Not used — get_snow_report() is overridden to use Playwright."""
        raise NotImplementedError("GreatDivideScraper uses Playwright via get_snow_report()")
    
    def process_data_from_soup(self, soup) -> Dict:
        """Process data from BeautifulSoup object (after Selenium render)"""
        data = self.blank_record()
        
        # Get all text content
        full_text = soup.get_text()
        
        # -------------------------
        # 1) Last Updated - "Report Date: Wed, Jan 7, 2026 7:36 am"
        # -------------------------
        date_match = re.search(r'Report Date:\s*(.+?)(?:\n|Resort)', full_text)
        if date_match:
            raw_date = date_match.group(1).strip()
            try:
                # Parse: "Wed, Jan 7, 2026 7:36 am"
                parsed = datetime.strptime(raw_date, "%a, %b %d, %Y %I:%M %p")
                data["last_updated"] = parsed.strftime("%Y-%m-%d %I:%M %p")
            except Exception as e:
                logging.warning(f"Great Divide: Could not parse date '{raw_date}': {e}")
                data["last_updated"] = raw_date
        
        # -------------------------
        # 2) Lifts & Runs - "Lifts 4 / 6 open" and "Trails 45 / 123 open"
        # -------------------------
        lifts_match = re.search(r'Lifts\s*(\d+)\s*/\s*(\d+)\s*open', full_text)
        if lifts_match:
            data['lifts_open'] = f"{lifts_match.group(1)} / {lifts_match.group(2)}"
        
        trails_match = re.search(r'Trails\s*(\d+)\s*/\s*(\d+)\s*open', full_text)
        if trails_match:
            data['runs_open'] = f"{trails_match.group(1)} / {trails_match.group(2)}"
        
        # -------------------------
        # 3) Snowfall - "Overnight snowfall" or variations
        # -------------------------
        overnight_match = re.search(r'(?:Overnight|overnight).*?(\d+)\s*(?:inches|")', full_text)
        if overnight_match:
            data['snow_overnight'] = self.clean_numeric(overnight_match.group(1))
        
        # -------------------------
        # 4) Snow Depth - "Base Depth 6"" and "Summit Depth 14""
        # -------------------------
        base_match = re.search(r'Base Depth[:\s]*(\d+)\s*(?:inches|")', full_text, re.IGNORECASE)
        if base_match:
            data['base_depth'] = self.clean_numeric(base_match.group(1))
        
        summit_match = re.search(r'Summit Depth[:\s]*(\d+)\s*(?:inches|")', full_text, re.IGNORECASE)
        if summit_match:
            data['summit_depth'] = self.clean_numeric(summit_match.group(1))
        
        # -------------------------
        # 5) Comments - Extract condition report
        # -------------------------
        report_match = re.search(r"Today's Condition Report:(.+?)(?:Overnight|Snow|Grooming|$)", full_text, re.DOTALL)
        if report_match:
            condition_report = report_match.group(1).strip()
            # Clean up whitespace
            condition_report = re.sub(r'\s+', ' ', condition_report)
            condition_report = condition_report[:1000]  # Cap at 1000 chars
            data['comments'] = condition_report
        
        data['data_source'] = 'selenium_dynamic'
        return data

class MaverickScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Maverick")

    def get_url(self) -> str:
        return "https://www.skimaverick.com/weather"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _first_match(pattern: str, text: str, flags=re.IGNORECASE):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    @staticmethod
    def _brace_extract_json_object(text: str, start: int) -> str:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        raise ValueError("Unterminated JSON")

    def _extract_bootstrap_state(self, html: str):
        key = "window.__BOOTSTRAP_STATE__"
        i = html.find(key)
        if i == -1:
            return None
        j = html.find("{", i)
        if j == -1:
            return None
        try:
            blob = self._brace_extract_json_object(html, j)
            return json.loads(blob)
        except Exception:
            return None

    def _walk_quill_inserts(self, obj):
        inserts = []

        def walk(x):
            if isinstance(x, dict):
                q = x.get("quill")
                if isinstance(q, dict):
                    ops = q.get("ops", [])
                    for op in ops:
                        if isinstance(op, dict) and isinstance(op.get("insert"), str):
                            inserts.append(op["insert"])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(obj)
        return inserts

    # ------------------------------------------------------------------
    # CORE TEXT PARSER (authoritative + sanitized)
    # ------------------------------------------------------------------
    def _parse_snow_report_blob(self, blob: str, data: dict):
        """
        Parse Maverick snow report text from a clean text blob.

        Defensive by design:
          - Hard-clips Square / Quill / JSON pollution
          - Extracts only authoritative snow report text
          - Guarantees validator-required fields
        """
        if not blob:
            return

        # ------------------------------------------------------------
        # HARD CLIP — remove Square / site-state pollution
        # ------------------------------------------------------------
        for marker in (
            "storeInfo",
            "featureFlags",
            "autoNavLinks",
            "datadog",
            "commerceLinks",
            "window.__BOOTSTRAP_STATE__",
        ):
            idx = blob.find(marker)
            if idx != -1:
                blob = blob[:idx]

        blob = normalize_text(blob)
        blob = re.sub(r"\n{3,}", "\n\n", blob).strip()

        # ------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------
        def inches(label):
            m = re.search(
                rf"{label}\s*:\s*([0-9]+(?:\.[0-9]+)?)",
                blob,
                flags=re.IGNORECASE,
            )
            return self.clean_numeric(m.group(1)) if m else None

        def line_after(label):
            m = re.search(
                rf"{label}\s*:\s*([^\n\r]+)",
                blob,
                flags=re.IGNORECASE,
            )
            return normalize_text(m.group(1).strip()) if m else None

        # ------------------------------------------------------------
        # REQUIRED NUMERIC FIELDS (validator-safe)
        # ------------------------------------------------------------
        data["snow_overnight"] = inches("Overnight")
        data["snow_24h_base"]  = inches("Last 24 hours")
        data["snow_48h_base"]  = inches("Last 48 hours")
        data["snow_7day"]      = inches("Last 7 days")
        data["base_depth"]     = inches(r"Snow Depth\s*\(base\)")
        data["summit_depth"]   = inches(r"Snow Depth\s*\(summit\)")

        # ------------------------------------------------------------
        # Surface conditions (STRICT)
        # ------------------------------------------------------------
        primary = line_after("Primary Surface Conditions")
        secondary = line_after("Secondary Surface Conditions")

        if primary and secondary:
            data["conditions_surface"] = f"{primary}, {secondary}"
        elif primary:
            data["conditions_surface"] = primary
        else:
            data["conditions_surface"] = None

        # ------------------------------------------------------------
        # Timestamp
        # ------------------------------------------------------------
        date = self._first_match(r"(\d{1,2}/\d{1,2}/\d{4})", blob)
        time = self._first_match(r"(\d{1,2}:\d{2}\s*(?:am|pm))", blob)

        if date:
            try:
                if time:
                    dt = datetime.strptime(
                        f"{date} {time.lower().replace(' ', '')}",
                        "%m/%d/%Y %I:%M%p",
                    )
                else:
                    dt = datetime.strptime(date, "%m/%d/%Y")
                data["last_updated"] = dt.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                data["last_updated"] = date

    # ------------------------------------------------------------------
    # MAIN ENTRY
    # ------------------------------------------------------------------
    def process_data(self, response):
        data = self.blank_record()
        html = response.text

        # 1️⃣ PRIMARY PATH — Square bootstrap → Quill Snow Report
        state = self._extract_bootstrap_state(html)
        if state:
            inserts = self._walk_quill_inserts(state)
            candidates = [
                s for s in inserts
                if isinstance(s, str)
                and (
                    "Last 24 hours" in s
                    or "Snow Depth (base)" in s
                    or "Snow Depth (summit)" in s
                )
            ]


            if candidates:
                blob = normalize_text(max(candidates, key=len))
                self._parse_snow_report_blob(blob, data)

                if any(data.get(k) is not None for k in (
                    "snow_overnight",
                    "snow_24h_base",
                    "base_depth",
                    "summit_depth",
                )):
                    data["data_source"] = "bootstrap_quill"
                    return data

        # 2️⃣ FALLBACK — visible text
        soup = BeautifulSoup(html, "html.parser")
        text_blob = normalize_text(soup.get_text("\n", strip=True))
        self._parse_snow_report_blob(text_blob, data)

        if any(data.get(k) is not None for k in (
            "snow_overnight",
            "snow_24h_base",
            "base_depth",
            "summit_depth",
        )):
            data["data_source"] = "html_text"
            return data

        # 3️⃣ LAST RESORT — raw HTML
        self._parse_snow_report_blob(normalize_text(html), data)
        data["data_source"] = "html_payload"
        return data








# =============================================================================
# SNOTEL DATA INTEGRATION
# =============================================================================


class SNOTELDataFetcher:
    """
    Fetches backcountry snow data from SNOTEL stations.

    Primary data (new snow, depth, temp, SWE, NWS 7-day forecast) comes from
    the station API (SNOTEL_API_KEY).  Median SWE for percent-of-median is
    still fetched from the USDA AWDB REST API.
    """

    STATION_API_URL = "https://station-api-627869388838.us-west1.run.app/v1/stations"
    AWDB_URL = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"

    STATION_INFO = {
            # Existing
            "Showdown":     {"triplet": "437:MT:SNTL",  "name": "Deadman Creek",   "lat": 46.79, "lon": -110.68, "elevation": 6440},
            "TetonPass":    {"triplet": "649:MT:SNTL",  "name": "Mount Lockhart",  "lat": 47.92, "lon": -112.82, "elevation": 6430},
            "Discovery":    {"triplet": "850:MT:SNTL",  "name": "Warm Springs",    "lat": 46.27, "lon": -113.16, "elevation": 7840},
            "LostTrail":    {"triplet": "727:MT:SNTL",  "name": "Saddle Mountain", "lat": 45.69, "lon": -113.97, "elevation": 7890},
            "Snowbowl":     {"triplet": "901:MT:SNTL",  "name": "Stuart Mountain", "lat": 47.00, "lon": -113.93, "elevation": 7270},
            "LookoutPass":  {"triplet": "594:ID:SNTL",  "name": "Lookout",         "lat": 47.46, "lon": -115.70, "elevation": 5180},
            "Blacktail":    {"triplet": "1144:MT:SNTL", "name": "Blacktail Mtn",   "lat": 47.98, "lon": -114.35, "elevation": 5660},
            "BigMountain":  {"triplet": "469:MT:SNTL",  "name": "Emery Creek",     "lat": 48.43, "lon": -113.94, "elevation": 4340},

            # New Additions
            "SilverMountain": {"triplet": "803:ID:SNTL", "name": "Sunset",            "lat": 47.56, "lon": -115.82, "elevation": 5580},
            "TurnerMountain": {"triplet": "311:MT:SNTL", "name": "Banfield Mountain", "lat": 48.57, "lon": -115.45, "elevation": 5580},
            "BearPaw":        {"triplet": "917:MT:SNTL", "name": "Rocky Boy",         "lat": 48.17, "lon": -109.65, "elevation": 4730},
            "GreatDivide":    {"triplet": "903:MT:SNTL", "name": "Nevada Ridge",      "lat": 46.84, "lon": -112.51, "elevation": 7030},
            "Schweitzer":     {"triplet": "738:ID:SNTL", "name": "Schweitzer Basin",  "lat": 48.37, "lon": -116.64, "elevation": 6090},
            "Maverick":       {"triplet": "656:MT:SNTL", "name": "Mule Creek",        "lat": 45.41, "lon": -112.96, "elevation": 8300},
            "BigSky":         {"triplet": "590:MT:SNTL", "name": "Lone Mountain",     "lat": 45.27, "lon": -111.43, "elevation": 8810},
            "BridgerBowl":    {"triplet": "365:MT:SNTL", "name": "Brackett Creek",    "lat": 45.89, "lon": -110.94, "elevation": 7370},
            "RedLodge":       {"triplet": "407:MT:SNTL", "name": "Cole Creek",        "lat": 45.19, "lon": -109.35, "elevation": 7910},
        }
    
    def __init__(self):
        self._log = logging.getLogger(__name__)
        self._api_key = os.getenv("SNOTEL_API_KEY")
        if not self._api_key:
            self._log.warning("SNOTEL_API_KEY not set — station API calls will be skipped")

        from requests import Session
        from requests.adapters import HTTPAdapter
        from requests.packages.urllib3.util.retry import Retry

        retry = Retry(
            total=4, connect=4, read=4, status=4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            backoff_factor=1.2,
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        sess = Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (SnowReportBot/2.0; +https://github.com/jriddering/Northern-Rockies-snow-reports)",
            "Accept": "application/json",
        })
        sess.mount("https://", adapter)
        self.session = sess
        self._timeout = (10, 25)

    def get_station_info(self, resort_name: str) -> Optional[Dict]:
        return self.STATION_INFO.get(resort_name)

    def _create_empty_report(self, station_name=None, latest_observation=None, error_reason=None, triplet=None, elevation=None):
        return {
            'station_name': station_name or 'SNOTEL',
            'triplet': triplet,
            'elevation': elevation,
            'snow_depth': 'N/A',
            'snotel_total_depth': 'N/A',
            'snotel_total_swe': 'N/A',
            'temp_f': 'N/A',
            'nws_7d_min_in': 'N/A',
            'nws_7d_max_in': 'N/A',
            'percent_of_median': 'N/A',
            'latest_observation': latest_observation,
            'unavailable': bool(error_reason),
            'error_reason': error_reason,
        }

    def get_snowfall_report(self, resort_name: str):
        """Single-station fallback — calls get_all and returns one entry."""
        all_reports = self.get_all_snowfall_reports()
        return all_reports.get(resort_name, self._create_empty_report(error_reason='not_found'))

    def _fetch_station_api(self, numeric_ids: list) -> dict:
        """Call the station API for all stations. Returns dict keyed by numeric id (int)."""
        if not self._api_key:
            return {}
        ids_str = ",".join(str(i) for i in numeric_ids)
        try:
            resp = self.session.get(
                self.STATION_API_URL,
                params={"ids": ids_str, "days": 1},
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            stations = data if isinstance(data, list) else data.get("stations", [])
            return {s["id"]: s for s in stations if "id" in s and "error" not in s}
        except Exception as e:
            self._log.error(f"Station API fetch failed: {e}")
            return {}

    def _fetch_awdb_medians(self, all_triplets: str, today_str: str) -> dict:
        """Fetch median SWE from AWDB. Returns dict keyed by triplet."""
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        url = (
            f"{self.AWDB_URL}"
            f"?stationTriplets={all_triplets}"
            "&elements=WTEQ"
            "&duration=DAILY"
            f"&beginDate={yesterday_str}"
            f"&endDate={today_str}"
            "&centralTendencyType=MEDIAN"
        )
        try:
            resp = self.session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            median_map = {}
            swe_map = {}
            for station in resp.json():
                triplet = station.get('stationTriplet')
                if not triplet:
                    continue
                for data_obj in station.get('data', []):
                    values = data_obj.get('values', [])
                    if values:
                        entry = values[-1]
                        cur = entry.get('value')
                        med = entry.get('median')
                        if cur is not None:
                            swe_map[triplet] = float(cur)
                        if med is not None:
                            median_map[triplet] = float(med)
                            self._log.info(f"  {triplet}: SWE={cur}\", median={med}\"")
            return median_map, swe_map
        except Exception as e:
            self._log.error(f"AWDB median fetch failed: {e}")
            return {}, {}

    def get_all_snowfall_reports(self) -> Dict[str, Dict]:
        """
        Fetches current snow data from the station API and median SWE from AWDB.
        Returns a dict keyed by resort name.
        """
        self._log.info("Fetching SNOTEL data (station API + AWDB medians)...")

        triplet_to_resort = {v['triplet']: k for k, v in self.STATION_INFO.items()}
        all_triplets = ",".join(triplet_to_resort.keys())
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Numeric IDs are the first segment of each triplet (e.g. "437" from "437:MT:SNTL")
        numeric_ids = [int(t.split(':')[0]) for t in triplet_to_resort.keys()]
        id_to_triplet = {int(t.split(':')[0]): t for t in triplet_to_resort.keys()}

        station_data = self._fetch_station_api(numeric_ids)
        median_map, swe_map = self._fetch_awdb_medians(all_triplets, today_str)

        self._log.info(f"Station API returned {len(station_data)} records; "
                       f"AWDB medians returned {len(median_map)} records")

        final_reports = {}

        for r_name, info in self.STATION_INFO.items():
            triplet = info['triplet']
            numeric_id = int(triplet.split(':')[0])
            station = station_data.get(numeric_id)

            # --- percent of median ---
            swe = (station.get('swe_in') if station else None) or swe_map.get(triplet)
            median = median_map.get(triplet)
            if swe is not None and median and median > 0:
                pct_median = int(round((float(swe) / median) * 100))
                self._log.info(f"  {r_name}: {swe}\" SWE / {median}\" median = {pct_median}%")
            elif swe and float(swe) > 0:
                pct_median = ">100"
                self._log.warning(f"  {r_name}: No median data, SWE={swe}\"")
            else:
                pct_median = "N/A"

            if station:
                final_reports[r_name] = {
                    'station_name': info['name'],
                    'triplet': triplet,
                    'elevation': info['elevation'],
                    'snow_depth': station.get('accum_in'),       # new snow last 24h (depth inches)
                    'snotel_total_depth': station.get('depth_in'),
                    'snotel_total_swe': station.get('swe_in'),
                    'temp_f': station.get('temp_f'),
                    'nws_7d_min_in': station.get('nws_7d_min_in'),
                    'nws_7d_max_in': station.get('nws_7d_max_in'),
                    'percent_of_median': pct_median,
                    'latest_observation': station.get('last_mst'),
                    'unavailable': False,
                    'error_reason': None,
                }
            else:
                report = self._create_empty_report(
                    station_name=info['name'],
                    triplet=triplet,
                    elevation=info['elevation'],
                    error_reason='station_api_no_data' if self._api_key else 'no_api_key',
                )
                report['percent_of_median'] = pct_median
                if swe is not None:
                    report['snotel_total_swe'] = swe
                final_reports[r_name] = report

        return final_reports


# =============================================================================
# MAIN EXECUTION
# =============================================================================
def main():
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning)

    from llm_fallback import llm_extract

    log_file = os.path.join(LOG_DIR, "snow_report.log")
    
    logging.basicConfig(
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ],
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    try:
        logging.info("Starting snow report script")
        
        # --- INIT FIREBASE EARLY ---
        if not firebase_admin._apps:
            cred_path = os.getenv('FIREBASE_SERVICE_ACCOUNT') 
            if cred_path:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                logging.warning("No Firebase credentials found. Change detection will fail.")
        
        snotel_fetcher = SNOTELDataFetcher()
        nws_fetcher = NWSStormOutlook()

        logging.info("Fetching all SNOTEL data (station API + AWDB medians)...")
        try:
            all_snotel_data = snotel_fetcher.get_all_snowfall_reports()
            logging.info(f"SNOTEL fetch complete. Got data for {len(all_snotel_data)} stations.")
        except Exception as e:
            logging.error(f"FATAL: SNOTEL batch fetch failed: {e}", exc_info=True)
            all_snotel_data = {}

        scrapers = [
            SnowbowlScraper(),
            DiscoveryScraper(),
            LookoutPassScraper(),
            BigMountainScraper(),
            LostTrailScraper(),
            TetonPassScraper(),
            ShowdownScraper(),
            BlacktailScraper(),
            SchweitzerScraper(),
            SilverMountainScraper(),
            BridgerBowlScraper(),
            MaverickScraper(),
            RedLodgeScraper(),
            BigSkyScraper(), 
            GreatDivideScraper(),
        ]
        
        resort_data_list = []
        for scraper in scrapers:
            logging.info(f"Fetching data for {scraper.resort_name}...")
            snow_data = scraper.get_snow_report()
            
            # Universal Scrape Validation/Quarantine
            snow_data = apply_quarantine_if_needed(scraper.resort_name, snow_data)

            # LLM fallback: if quarantined AND we have page HTML, try to recover
            if snow_data and snow_data.get('data_source') == 'quarantined':
                page_html = getattr(scraper, '_last_page_html', None)
                if page_html:
                    logging.info(f"{scraper.resort_name}: Quarantined — attempting LLM fallback extraction...")
                    llm_fields = llm_extract(scraper.resort_name, page_html)
                    if llm_fields:
                        # Merge LLM fields over the quarantined record (preserves metadata)
                        snow_data.update(llm_fields)
                        snow_data['data_source'] = 'llm_fallback'
                        # Re-run range validation on LLM output
                        problems = validate_scrape_minimums(scraper.resort_name, snow_data)
                        if problems:
                            logging.warning(f"{scraper.resort_name}: LLM output still has issues: {problems}")
                            snow_data['data_source'] = 'llm_fallback_partial'
                        else:
                            logging.info(f"{scraper.resort_name}: LLM fallback succeeded")
                    else:
                        logging.warning(f"{scraper.resort_name}: LLM fallback returned nothing — keeping quarantined record")
                else:
                    logging.warning(f"{scraper.resort_name}: Quarantined but no page HTML available for LLM fallback")

            if snow_data:
                resort_name = snow_data["resort"]
                
                # --- SNOTEL Logic & Median Mapping ---
                snotel_report = all_snotel_data.get(resort_name)
                
                if snotel_report is None:
                    logging.warning(f"No pre-fetched SNOTEL data for {resort_name}, attempting single fallback fetch...")
                    try:
                        snotel_report = snotel_fetcher.get_snowfall_report(resort_name)
                    except Exception as e:
                        logging.error(f"SNOTEL fallback fetch failed for {resort_name}: {e}")
                        snotel_report = snotel_fetcher._create_empty_report(
                            station_name=snotel_fetcher.STATION_INFO.get(resort_name, {}).get('name', 'SNOTEL'),
                            error_reason='fallback_fail',
                            triplet=snotel_fetcher.STATION_INFO.get(resort_name, {}).get('triplet')
                        )

                # Attach the full snotel object
                snow_data['snotel_data'] = snotel_report
                
                # Pull Percent of Median to the top level for CSV/Firebase columns
                if snotel_report:
                    val = snotel_report.get('percent_of_median', 'N/A')
                    snow_data['percent_of_median'] = val
                    logging.info(f"SNOTEL Median for {resort_name}: {val}%")

                # --- NWS Logic ---
                logging.info(f"Fetching NWS for: {resort_name}")
                coords = RESORT_COORDS.get(resort_name)
                if coords:
                    try:
                        snow_data['nws_forecast'] = nws_fetcher.get_outlook(coords["lat"], coords["lon"])
                        # Debug for Big Mountain
                        if resort_name == "BigMountain":
                            logging.info(f"NWS DEBUG BigMountain: {snow_data['nws_forecast']}")
                        
                        time.sleep(0.3) # Rate limit protection
                    except Exception as e:
                        logging.warning(f"NWS fetch failed for {resort_name}: {e}")
                        snow_data['nws_forecast'] = None
                else:
                    snow_data['nws_forecast'] = None

                resort_data_list = resort_data_list + [snow_data]
                
                # Save to CSV (Includes the calculated percent_of_median column)
                scraper.save_to_csv(snow_data)  
                logging.info(f"Complete data saved to CSV for {scraper.resort_name}")
            else:
                logging.warning(f"No data retrieved for {scraper.resort_name}")
        
        logging.info(f"Scraping complete. Collected data for {len(resort_data_list)} resorts.")
        
    except Exception as e:
        logging.error(f"Error in main function: {str(e)}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
