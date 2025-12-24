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
    Returns a list of problems (empty list means OK).
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

    if "enable javascript" in blob:
        problems.append("javascript_gate_page")

    # Fragile resort-specific sanity checks
    if resort_name == "Maverick":
        core = [
            data.get("snow_overnight"),
            data.get("snow_24h_base"),
            data.get("base_depth"),
            data.get("summit_depth"),
        ]
        if all(v is None for v in core):
            problems.append("missing_core_fields")

    if resort_name == "GreatDivide":
        if data.get("runs_open") is None:
            problems.append("missing_runs_open")
        if data.get("lifts_open") is None:
            problems.append("missing_lifts_open")
        if data.get("base_depth") is None:
            problems.append("missing_base_depth")

    return problems



def quarantine_bad_scrape(resort_name: str, data: Dict, problems: List[str]) -> Dict:
    """
    Lenient mode: keep pipeline running, but prevent bad parses from poisoning output.
    - wipe key numeric + ops fields to None (NOT 0.0)
    - last_updated=None so your existing staleness logic can handle it
    - add a clean warning to comments
    - mark data_source='quarantined'
    """
    wipe_keys = [
        "snow_overnight", "snow_24h_base", "snow_24h_summit",
        "snow_48h_base", "snow_48h_summit", "snow_7day",
        "base_depth", "summit_depth",
        "temp_base", "temp_summit", "wind_speed",
        "lifts_open", "runs_open",
        "conditions_surface", "weather_current",
        "forecast_base_temp_high", "forecast_base_temp_low",
        "forecast_summit_temp_high", "forecast_summit_temp_low",
        "forecast_precipitation", "forecast_snow",
    ]
    for k in wipe_keys:
        data[k] = None

    data["last_updated"] = None

    prev = (data.get("comments") or "").strip()
    warn = f"SCRAPE_WARNING: {resort_name} quarantined ({', '.join(problems)})."
    data["comments"] = warn if not prev else f"{prev} | {warn}"

    data["data_source"] = "quarantined"
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
        try:
            response = requests.get(self.get_url(), headers=self.headers, timeout=20)
            response.raise_for_status()
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
                            # Snowbowl’s "New Snow" here is not necessarily 24h, but it matches the polling totals when present
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

            val_clean = val_candidate.replace('"', '').replace('”', '').strip()
            
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

class LostTrailScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("LostTrail")

    def get_url(self):
        return "https://losttrail.com/snow-report/"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        last_updated_element = soup.find('div', class_='elementor-heading-title elementor-size-default',
                                         string=re.compile(r'Snow Report for', re.I))
        
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
        
        labels = {
            "New Snow (since lifts closed)": "snow_overnight",
            "New Snow, 24 hours": "snow_24h_base",
            "New Snow, 48 hours": "snow_48h_base",
            "Snow Depth - Bottom": "base_depth",
            "Snow Depth - Top": "summit_depth"
        }
        
        for label, key in labels.items():
            element = soup.find(string=re.compile(label, re.I))
            if element:
                next_sibling = element.find_next("span", class_="elementor-heading-title")
                if next_sibling:
                    data[key] = next_sibling.text.strip().replace('"', '')
        
        primary_condition = soup.find(string=re.compile("Surface Conditions \(Primary\)", re.I))
        secondary_condition = soup.find(string=re.compile("Surface Conditions \(Secondary\)", re.I))
        if primary_condition:
            primary_text = primary_condition.find_next("span").text.strip()
            if secondary_condition:
                secondary_text = secondary_condition.find_next("span").text.strip()
                data['conditions_surface'] = f"{primary_text}, {secondary_text}"
            else:
                data['conditions_surface'] = primary_text
        
        lifts_section = soup.find(string=re.compile("Number of Lifts Open", re.I))
        if lifts_section:
            lifts_value = lifts_section.find_next("span", class_="elementor-heading-title")
            if lifts_value:
                data['lifts_open'] = lifts_value.text.strip()
        
        trails_section = soup.find(string=re.compile("Trails Open", re.I))
        if trails_section:
            trails_value = trails_section.find_next("span", class_="elementor-heading-title")
            if trails_value:
                data['runs_open'] = trails_value.text.strip()
        
        comments_section = soup.find('div', class_='elementor-widget-container')
        if comments_section:
            data['comments'] = comments_section.get_text(strip=True)
        
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




class GreatDivideScraper(BaseSnowReportScraper):
    """
    Great Divide scraper - parses the mostly-static HTML text on:
    https://www.skigd.com/weather-and-conditions

    Notes:
      - The page includes a report date like "12-14-25" (no explicit time).
        We normalize to "YYYY-MM-DD 06:00 AM" in America/Denver.
      - "Season Total 26”" is *season snowfall*, not 7-day snowfall.
        Your schema does not currently include seasonal snowfall (except in a couple scrapers),
        so we store it in comments and (optionally) also in snow_7day ONLY if you want that behavior.
    """

    def __init__(self):
        super().__init__("GreatDivide")

    def get_url(self) -> str:
        return "https://www.skigd.com/weather-and-conditions"

    @staticmethod
    def _first_match(pattern: str, text: str, flags=re.IGNORECASE):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    def process_data(self, response) -> Dict:
        soup = BeautifulSoup(response.text, "html.parser")
        data = self.blank_record()

        # Work with normalized full-page text (very resilient for this site)
        full_text = normalize_text(soup.get_text("\n", strip=True))
        full_text = re.sub(r"\n{2,}", "\n", full_text).strip()

        # -------------------------
        # 1) last_updated (report date like "12-14-25")
        # -------------------------
        # The page includes a standalone MM-DD-YY line near the conditions block.
        raw_date = self._first_match(r"\b(\d{1,2}-\d{1,2}-\d{2})\b", full_text)
        if raw_date:
            try:
                dt = datetime.strptime(raw_date, "%m-%d-%y")
                # Great Divide doesn’t publish a time in that block; choose a stable morning time.
                data["last_updated"] = dt.strftime("%Y-%m-%d") + " 06:00 AM"
            except Exception:
                data["last_updated"] = raw_date

        # -------------------------
        # 2) Surface / weather summary lines
        # -------------------------
        # Examples on the page:
        #   "Forecast Weather: Mostly Sunny"
        #   "Forecast: Mostly Sunny"
        #   "Snow Surface: Groomed"
        forecast_weather = (
            self._first_match(r"Forecast Weather:\s*([^\n]+)", full_text)
            or self._first_match(r"Forecast:\s*([^\n]+)", full_text)
        )
        if forecast_weather:
            data["weather_current"] = forecast_weather

        snow_surface = self._first_match(r"Snow Surface:\s*([^\n]+)", full_text)
        if snow_surface:
            data["conditions_surface"] = snow_surface

        # Forecast temps (store in forecast_* fields; leave temp_base/temp_summit empty unless you prefer otherwise)
        base_temp = self._first_match(r"Forecast Base Temp:\s*([-\d\.]+)", full_text)
        top_temp  = self._first_match(r"Forecast Top Temp:\s*([-\d\.]+)", full_text)
        if base_temp is not None:
            data["forecast_base_temp_high"] = self.clean_numeric(base_temp)
        if top_temp is not None:
            data["forecast_summit_temp_high"] = self.clean_numeric(top_temp)

        # -------------------------
        # 3) Depth / lifts / trails
        # -------------------------
        # "Avg Base Depth min/max 10 / 10"
        depth_pair = re.search(r"Avg Base Depth.*?(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", full_text, re.IGNORECASE)
        if depth_pair:
            # Use max as the "base_depth" (more intuitive)
            data["base_depth"] = self.clean_numeric(depth_pair.group(2))
        else:
            # Fallback: any single "Avg Base Depth" number
            one_depth = self._first_match(r"Avg Base Depth.*?(\d+(?:\.\d+)?)", full_text)
            if one_depth is not None:
                data["base_depth"] = self.clean_numeric(one_depth)

        # "Open Downhill Trails 3 / 127 max"
        trails_pair = re.search(r"Open Downhill Trails\s+(\d+)\s*/\s*(\d+)", full_text, re.IGNORECASE)
        if trails_pair:
            data["runs_open"] = f"{trails_pair.group(1)} / {trails_pair.group(2)}"

        # "Open Lifts 2 / 6 max"
        lifts_pair = re.search(r"Open Lifts\s+(\d+)\s*/\s*(\d+)", full_text, re.IGNORECASE)
        if lifts_pair:
            data["lifts_open"] = f"{lifts_pair.group(1)} / {lifts_pair.group(2)}"

        # -------------------------
        # 4) Snowfall block
        # -------------------------
        # The page shows:
        #   "Season Total 26”"
        # It often does NOT include Last 12hrs / Last 72hrs numeric values.
        season_total = self._first_match(r"Season Total\s+(\d+(?:\.\d+)?)", full_text)
        if season_total is not None:
            # Keep this true meaning in comments so you don't confuse it with 7-day.
            # If you *want* to also fill snow_7day, uncomment the next line.
            # data["snow_7day"] = self.clean_numeric(season_total)
            season_total_num = self.clean_numeric(season_total)
        else:
            season_total_num = None

        # -------------------------
        # 5) Comments / bulletin
        # -------------------------
        # Try to extract the "Good morning from Great Divide!" section through the next major header-ish block.
        bulletin = None
        bullet_start = full_text.lower().find("good morning from great divide")
        if bullet_start != -1:
            snippet = full_text[bullet_start:]
            # Stop at "Weather & Snow Update:" if present (keeps bulletin readable)
            stop_idx = snippet.lower().find("weather & snow update")
            if stop_idx != -1:
                bulletin = snippet[:stop_idx].strip()
            else:
                bulletin = snippet[:1200].strip()  # hard cap to avoid huge comments

        comment_parts = []
        if bulletin:
            comment_parts.append(bulletin)
        if season_total_num is not None:
            comment_parts.append(f"Season total snowfall (site): {season_total_num}\"")

        if comment_parts:
            data["comments"] = normalize_text("\n\n".join(comment_parts))

        data["data_source"] = "html_static"
        return data



class MaverickScraper(BaseSnowReportScraper):
    """
    Maverick scraper - parse the visible text content from the /weather page.

    Why this is better:
      - Avoids brittle __BOOTSTRAP_STATE__ JSON parsing and control-character issues.
      - Uses the rendered page text, which currently includes:
        Overnight, Last 24/48/7 days, Snow Depth (base/summit), Annual Snowfall.
    """

    def __init__(self):
        super().__init__("Maverick")

    def get_url(self) -> str:
        return "https://www.skimaverick.com/weather"

    @staticmethod
    def _first_match(pattern: str, text: str, flags=re.IGNORECASE) -> Optional[str]:
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    def process_data(self, response) -> Dict:
        data = self.blank_record()

        soup = BeautifulSoup(response.text, "html.parser")
        raw_text = normalize_text(soup.get_text("\n", strip=True))
        raw_text = re.sub(r"\n{2,}", "\n", raw_text).strip()

        # -----------------------------
        # Core snowfall/depth fields
        # Examples currently present on page (as visible text):
        # Overnight: 0"
        # Last 24 hours: 0"
        # Last 48 hours: 4"
        # Last 7 days: 14"
        # Snow Depth (base): 5"
        # Snow Depth (summit): 20"
        # Annual Snowfall: 26"
        # -----------------------------
        patterns = {
            "snow_overnight":  r"\bOvernight:\s*([0-9]+(?:\.[0-9]+)?)",
            "snow_24h_base":   r"\bLast\s*24\s*hours:\s*([0-9]+(?:\.[0-9]+)?)",
            "snow_48h_base":   r"\bLast\s*48\s*hours:\s*([0-9]+(?:\.[0-9]+)?)",
            "snow_7day":       r"\bLast\s*7\s*days:\s*([0-9]+(?:\.[0-9]+)?)",
            "base_depth":      r"\bSnow\s*Depth\s*\(base\):\s*([0-9]+(?:\.[0-9]+)?)",
            "summit_depth":    r"\bSnow\s*Depth\s*\(summit\):\s*([0-9]+(?:\.[0-9]+)?)",
        }

        for key, pat in patterns.items():
            v = self._first_match(pat, raw_text)
            if v is not None:
                data[key] = self.clean_numeric(v)

        # Annual snowfall doesn't have a dedicated STANDARD_COLUMNS field.
        # Keep it in comments so you still capture it reliably.
        annual = self._first_match(r"\bAnnual\s*Snowfall:\s*([0-9]+(?:\.[0-9]+)?)", raw_text)
        annual_num = self.clean_numeric(annual) if annual is not None else None

        # -----------------------------
        # Optional: surface conditions, if present
        # -----------------------------
        primary = self._first_match(r"\bPrimary\s*Surface\s*Conditions:\s*([^\n]+)", raw_text)
        secondary = self._first_match(r"\bSecondary\s*Surface\s*Conditions:\s*([^\n]+)", raw_text)

        if primary and secondary:
            # Guard against the weird case you saw where primary is blank and it "captures" the secondary label
            if primary.strip().lower() != "secondary surface conditions:":
                data["conditions_surface"] = normalize_text(f"{primary}, {secondary}")
        elif primary:
            if primary.strip().lower() != "secondary surface conditions:":
                data["conditions_surface"] = normalize_text(primary)

        # -----------------------------
        # last_updated
        # -----------------------------
        # If the page includes an explicit date anywhere (e.g., 12/14/2025), capture it.
        # Otherwise leave None so your universal staleness check can fingerprint.
        date_str = self._first_match(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", raw_text)
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%m/%d/%Y")
                data["last_updated"] = dt.strftime("%Y-%m-%d") + " 06:00 AM"
            except Exception:
                data["last_updated"] = date_str
        else:
            data["last_updated"] = None

        # -----------------------------
        # comments
        # -----------------------------
        comment_parts = []

        # If you want to preserve a small snippet of the page context, keep it short:
        # Pull the first line that contains "Overnight" as proof-of-source.
        if "Overnight:" in raw_text:
            # find the line that contains Overnight and keep a limited run-on
            lines = raw_text.splitlines()
            for ln in lines:
                if "Overnight:" in ln:
                    comment_parts.append(f"Parsed page metrics line: {ln[:180]}")
                    break

        if annual_num is not None:
            comment_parts.append(f"Annual snowfall (site): {annual_num}\"")

        if comment_parts:
            data["comments"] = normalize_text(" | ".join(comment_parts))
        else:
            data["comments"] = "Data extracted from visible page text."

        data["data_source"] = "html_text_parse"
        return data






# =============================================================================
# SNOTEL DATA INTEGRATION (HOURLY)
# =============================================================================
class SNOTELDataFetcher:
    """
    Fetches backcountry snow data from SNOTEL stations via the AWDB REST API.
    Uses HOURLY data to build a history of Snow Depth, SWE, and Temperature.
    """
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
        import os
        import logging
        from requests import Session
        from requests.adapters import HTTPAdapter
        from requests.packages.urllib3.util.retry import Retry 
        
        self._log = logging.getLogger(__name__)
        
        connect_timeout = float(os.getenv("SNOTEL_TIMEOUT_CONNECT", "10"))
        read_timeout    = float(os.getenv("SNOTEL_TIMEOUT_READ",    "25"))
        total_retries   = int(os.getenv("SNOTEL_RETRIES",         "4"))
        backoff_factor  = float(os.getenv("SNOTEL_BACKOFF",         "1.2"))
        self._timeout = (connect_timeout, read_timeout)
        
        retry = Retry(
            total=total_retries,
            connect=total_retries,
            read=total_retries,
            status=total_retries,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            backoff_factor=backoff_factor,
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        sess = Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (SnowReportBot/2.0; +https://github.com/jriddering/Northern-Rockies-snow-reports)",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        sess.mount("https://", adapter)
        self.session = sess

    def get_station_info(self, resort_name: str) -> Optional[Dict]:
        return self.STATION_INFO.get(resort_name)

    @staticmethod
    def _snow_category(density_pct: float) -> str:
        if density_pct is None: return "N/A"
        if density_pct < 6:   return "Blower Powder"
        if density_pct < 10:  return "Light Powder"
        if density_pct < 15:  return "Medium Powder"
        if density_pct < 25:  return "Heavy Powder"
        if density_pct < 40:  return "Wet Snow"
        return "Slush"

    def _create_empty_report(self, station_name=None, latest_observation=None, error_reason=None, triplet=None, elevation=None):
        return {
            'station_name': station_name or 'SNOTEL',
            'triplet': triplet,
            'elevation': elevation,
            'snow_depth': 'N/A',
            'swe': 'N/A',
            'snotel_total_depth': 'N/A',
            'snotel_total_swe': 'N/A', 
            'density': 'N/A',
            'percent_of_median': 'N/A', 
            'snow_category': 'N/A',
            'latest_observation': latest_observation,
            'history': [], # Empty history list
            'unavailable': bool(error_reason),
            'error_reason': error_reason 
        }

    def get_snowfall_report(self, resort_name: str):
        # Fallback method, rarely used. Returns empty struct to be safe.
        return self._create_empty_report(error_reason="Use batch fetch")

    def get_all_snowfall_reports(self) -> Dict[str, Dict]:
            """
            Fetches HOURLY SNOTEL data for all stations.
            Collects history (SNWD, WTEQ, TOBS) and calculates 24h deltas.
            """
            self._log.info("Starting batch SNOTEL API fetch (HOURLY)...")
            triplet_to_resort = {v['triplet']: k for k, v in self.STATION_INFO.items()}
            all_triplets = ",".join(triplet_to_resort.keys())
            
            # --- FIX: Calculate explicit date instead of using relative "-2" ---
            # This forces the API to respect our window regardless of server time
            from datetime import datetime, timedelta
            start_dt = datetime.now() - timedelta(days=2)
            begin_date_str = start_dt.strftime("%Y-%m-%d")
            
            url = (
                "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
                f"?stationTriplets={all_triplets}"
                "&elements=WTEQ,SNWD,TOBS"
                "&duration=HOURLY"
                f"&beginDate={begin_date_str}"  # Explicit start date
                "&endDate=9999-12-31"           # Explicit end date (force to current)
            )
            
            final_reports = {}
            try:
                resp = self.session.get(url, timeout=self._timeout)
                resp.raise_for_status()
                api_data_list = resp.json()
                
                if not api_data_list:
                    raise ValueError("API response was empty.")
                
                for station_data in api_data_list:
                    triplet = station_data.get('stationTriplet')
                    resort_name = triplet_to_resort.get(triplet)
                    
                    if not resort_name:
                        continue
                    
                    station_name = self.STATION_INFO[resort_name]['name']
                    station_elev = self.STATION_INFO[resort_name]['elevation']
                    
                    # Store series by timestamp to merge them
                    history_map = {}
                    
                    def get_or_create(ts):
                        if ts not in history_map:
                            history_map[ts] = {"timestamp": ts, "snow_depth": None, "swe": None, "temp": None}
                        return history_map[ts]

                    for element_data in station_data.get('data', []):
                        element_code = element_data['stationElement']['elementCode'].lower()
                        values = element_data.get('values', [])
                        
                        for v in values:
                            date_str = v.get('date')
                            val = v.get('value')
                            if not date_str: continue
                            
                            entry = get_or_create(date_str)
                            
                            if element_code == 'snwd':
                                entry['snow_depth'] = val
                            elif element_code == 'wteq':
                                entry['swe'] = val
                            elif element_code == 'tobs':
                                entry['temp'] = val

                    # Convert map to sorted list
                    full_history = sorted(history_map.values(), key=lambda x: x['timestamp'])
                    
                    # Ensure we don't upload too much if the query returns tons of data
                    # (Last 48 entries is roughly 2 days of hourly data)
                    recent_history = full_history[-48:] if len(full_history) > 48 else full_history

                    # --- CALCULATE 24H SUMMARY METRICS ---
                    obs_time = "N/A"
                    total_depth = None
                    total_swe = None
                    snow_change = None
                    swe_change = None
                    
                    if recent_history:
                        latest = recent_history[-1]
                        obs_time = latest['timestamp']
                        total_depth = latest['snow_depth']
                        total_swe = latest['swe']
                        
                        # Find 24h ago
                        try:
                            # Parse formatting
                            fmt_opts = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M']
                            latest_dt = None
                            for f in fmt_opts:
                                try:
                                    latest_dt = datetime.strptime(latest['timestamp'], f)
                                    break
                                except: pass
                            
                            if latest_dt:
                                target_dt = latest_dt - timedelta(hours=24)
                                prev_depth = None
                                prev_swe = None
                                min_diff = 5400 # 1.5 hours in seconds
                                
                                for h in recent_history:
                                    try:
                                        h_dt = None
                                        for f in fmt_opts:
                                            try:
                                                h_dt = datetime.strptime(h['timestamp'], f)
                                                break
                                            except: pass
                                        
                                        if h_dt:
                                            diff = abs((h_dt - target_dt).total_seconds())
                                            if diff < min_diff:
                                                min_diff = diff
                                                prev_depth = h['snow_depth']
                                                prev_swe = h['swe']
                                    except: continue
                                
                                if total_depth is not None and prev_depth is not None:
                                    snow_change = round(total_depth - prev_depth, 1)
                                
                                if total_swe is not None and prev_swe is not None:
                                    swe_change = round(total_swe - prev_swe, 2)
                        except Exception as e:
                            self._log.warning(f"Error calculating summary delta for {resort_name}: {e}")

                    # Calculate Density
                    density = None
                    if snow_change is not None and swe_change is not None and snow_change > 0.5 and swe_change > 0.01:
                        density = round((swe_change / snow_change) * 100, 1)
                    
                    percent_of_median = "N/A" 
                    is_new_snow = (density is not None)
    
                    report = {
                        'station_name': station_name,
                        'triplet': triplet,
                        'elevation': station_elev,
                        'snow_depth': snow_change if snow_change is not None else 0,
                        'swe': swe_change if swe_change is not None else 0,
                        'snotel_total_depth': total_depth if total_depth is not None else 0, 
                        'snotel_total_swe': total_swe if total_swe is not None else 0,
                        'density': density if is_new_snow else 'N/A',
                        'percent_of_median': percent_of_median,
                        'snow_category': self._snow_category(density) if is_new_snow else 'N/A',
                        'latest_observation': obs_time,
                        'history': recent_history,
                        'unavailable': False,
                        'error_reason': None if is_new_snow else "No new snow"
                    }
                    
                    final_reports[resort_name] = report
    
            except Exception as e:
                self._log.error(f"SNOTEL batch API fetch error: {e}", exc_info=True)
            
            # Fill missing resorts
            for resort_name in self.STATION_INFO.keys():
                if resort_name not in final_reports:
                    final_reports[resort_name] = self._create_empty_report(
                        station_name=self.STATION_INFO[resort_name]['name'], 
                        error_reason='batch_api_fail', 
                        triplet=self.STATION_INFO[resort_name]['triplet'],
                        elevation=self.STATION_INFO[resort_name]['elevation']
                    )
                    
            return final_reports

# =============================================================================
# MAIN EXECUTION
# =============================================================================
def main():
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning)

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

        logging.info("Fetching all SNOTEL data in one batch...")
        try:
            all_snotel_data = snotel_fetcher.get_all_snowfall_reports()
            logging.info(f"Batch SNOTEL fetch complete. Got data for {len(all_snotel_data)} stations.")
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
            
            snow_data = apply_quarantine_if_needed(scraper.resort_name, snow_data)

            
            
            if snow_data:
                resort_name = snow_data["resort"]
                
                # SNOTEL Logic
                snow_data['snotel_data'] = all_snotel_data.get(resort_name)
                if snow_data['snotel_data'] is None:
                    logging.warning(f"No pre-fetched SNOTEL data for {resort_name}, attempting single fallback fetch...")
                    try:
                        snow_data['snotel_data'] = snotel_fetcher.get_snowfall_report(resort_name)
                    except Exception as e:
                        logging.error(f"SNOTEL fallback fetch failed for {resort_name}: {e}")
                        snow_data['snotel_data'] = snotel_fetcher._create_empty_report(
                            station_name=snotel_fetcher.STATION_INFO.get(resort_name, {}).get('name', 'SNOTEL'),
                            error_reason='fallback_fail',
                            triplet=snotel_fetcher.STATION_INFO.get(resort_name, {}).get('triplet')
                        )

                # NWS Logic
                logging.info(f"Fetching NWS for: {resort_name}")
                coords = RESORT_COORDS.get(resort_name)
                if coords:
                    try:
                        snow_data['nws_forecast'] = nws_fetcher.get_outlook(coords["lat"], coords["lon"])
                        #debug
                        if resort_name == "BigMountain":
                            logging.info(f"NWS DEBUG BigMountain: {snow_data['nws_forecast']}")

                        
                        #debug
                        time.sleep(0.3) 
                    except Exception as e:
                        logging.warning(f"NWS fetch failed for {resort_name}: {e}")
                        snow_data['nws_forecast'] = None
                else:
                    snow_data['nws_forecast'] = None

                resort_data_list.append(snow_data)
                
                # Save to CSV - This includes the calculated snotel_data with density
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
