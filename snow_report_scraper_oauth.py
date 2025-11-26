"""
Montana/Idaho Snow Report Scraper - Cleaned & Standardized Version
This script scrapes snow reports from multiple ski resorts and stores data in CSV files.
It calculates SNOTEL density/SWE but DOES NOT upload to Firebase directly.

Features:
- Standardized data schema across all resorts
- CSV backup storage
- SNOTEL backcountry data integration (Density & Category)
- Powder alert notifications (configurable threshold)

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
#from googleapiclient.errors import HttpError
#from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from zoneinfo import ZoneInfo




# =============================================================================
# CONFIGURATION
# =============================================================================

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6
# Resort coordinates for NWS forecasts
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



# =============================================================================
# BASE SCRAPER CLASS
# =============================================================================
class BaseSnowReportScraper(ABC):
    """
    Abstract base class for ski resort snow report scrapers.
    All resort-specific scrapers inherit from this class.
    """

    STANDARD_COLUMNS = [
        # Core metadata
        'resort',               # Resort name
        'date',                 # Date data were accessed (YYYY-MM-DD)
        'timestamp',            # Time data were accessed (YYYY-MM-DD HH:MM:SS)
        'last_updated',         # Resort's reported last update time

        # Snowfall measurements
        'snow_overnight',
        'snow_24h_base',
        'snow_24h_summit',
        'snow_48h_base',
        'snow_48h_summit',
        'snow_7day',

        # Snow depth
        'base_depth',
        'summit_depth',

        # Temperatures
        'temp_base',
        'temp_summit',

        # Wind
        'wind_speed',

        # Conditions
        'conditions_surface',
        'weather_current',

        # Operations
        'lifts_open',
        'runs_open',

        # Notes
        'comments',

        # --- DATA QUALITY FLAG ---
        # "api_ok", "api_offseason", "html_fallback"
        'data_source',

        # --- DASHBOARD EXTRA FIELDS ---
        'nws_forecast',
        'snotel_data',

        # Old NWS legacy fields (kept for backward compatibility)
        'forecast_base_temp_high',
        'forecast_base_temp_low',
        'forecast_summit_temp_high',
        'forecast_summit_temp_low',
        'forecast_precipitation',
        'forecast_snow',
    ]

    # ----------------------------------------------------------------------
    #  INITIALIZER
    # ----------------------------------------------------------------------
    def __init__(self, resort_name: str):
        self.resort_name = resort_name
        self.csv_filename = os.path.join(DATA_DIR, f"{resort_name}_snow_report.csv")

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        }

    # ----------------------------------------------------------------------
    #  NUMERIC CLEANER
    # ----------------------------------------------------------------------
    @staticmethod
    def clean_numeric(value):
        """
        Clean numeric values consistently across all scrapers.
        Removes quotes, units, whitespace. Returns 0 for invalid values.
        """
        if value is None or value == '' or value == 'N/A':
            return 0.0

        value_str = str(value).strip()

        # Handle slash notation (like 0/5")
        if '/' in value_str:
            value_str = value_str.split('/')[0]

        # Strip common units
        for bad in ['"', '°', 'F', 'C', 'mph', 'in', '%']:
            value_str = value_str.replace(bad, '')

        cleaned = ''.join(c for c in value_str if c.isdigit() or c in ['.', '-'])

        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    # ----------------------------------------------------------------------
    #  REQUIRED ABSTRACT METHODS
    # ----------------------------------------------------------------------
    @abstractmethod
    def get_url(self) -> str:
        """Return the URL to scrape for this resort."""
        pass

    @abstractmethod
    def process_data(self, response) -> Dict:
        """
        Process the HTTP response and return standardized data.

        Must return a dictionary with ALL keys from STANDARD_COLUMNS.
        Missing data should be None.
        """
        pass

    # ----------------------------------------------------------------------
    #  BLANK RECORD HELPER  ← REQUIRED BY SILVER SCRAPER
    # ----------------------------------------------------------------------
    def blank_record(self) -> Dict:
        """
        Return a fully-populated "empty" record with all STANDARD_COLUMNS = None.
        Subclasses (like SilverMountainScraper) can safely start from this.
        """
        return {col: None for col in self.STANDARD_COLUMNS}

    # ----------------------------------------------------------------------
    #  TIMESTAMP HELPER  ← REQUIRED BY SILVER SCRAPER
    # ----------------------------------------------------------------------
    @staticmethod
    def now_string() -> str:
        """
        Consistent timestamp helper used by scrapers.
        """
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ----------------------------------------------------------------------
    #  BASE FETCH METHOD (SilverMountain overrides this)
    # ----------------------------------------------------------------------
    def get_snow_report(self) -> Optional[Dict]:
        """
        Fetch and process snow report data from the resort website.
        Silver Mountain *overrides* this method completely.
        """
        try:
            response = requests.get(self.get_url(), headers=self.headers, timeout=10)
            response.raise_for_status()
            data = self.process_data(response)

            if data:
                data.update({
                    'resort': self.resort_name,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
            return data

        except requests.exceptions.SSLError:
            # Retry without SSL verification
            try:
                response = requests.get(self.get_url(), headers=self.headers, timeout=10, verify=False)
                response.raise_for_status()
                data = self.process_data(response)

                if data:
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

    # ----------------------------------------------------------------------
    #  SAFE CSV SAVE METHOD (SILENTLY SKIPS None DATA)
    # ----------------------------------------------------------------------
    def save_to_csv(self, data: Dict):
        """
        Save snow report data to CSV with dedupe (keep latest timestamp per date).
        Skips None instead of crashing.
        """
        try:
            if data is None:
                print(f"save_to_csv: No data for {self.resort_name}, skipping CSV write.")
                return

            # Standardize full row
            row = {col: None for col in self.STANDARD_COLUMNS}
            row.update({
                k: normalize_text(v) if isinstance(v, str) else v
                for k, v in data.items()
                if v is not None
            })

            df_new = pd.DataFrame([row], columns=self.STANDARD_COLUMNS)

            # Load existing CSV
            if os.path.exists(self.csv_filename):
                df_old = pd.read_csv(self.csv_filename, dtype=str, keep_default_na=False)

                # Ensure schema consistency
                for col in self.STANDARD_COLUMNS:
                    if col not in df_old.columns:
                        df_old[col] = None

                df_all = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_all = df_new

            # Sort + dedupe latest by date
            df_all = df_all.sort_values(['date', 'timestamp'], ascending=True)
            df_all = df_all.drop_duplicates(subset='date', keep='last')

            df_all.to_csv(self.csv_filename, index=False, encoding='utf-8-sig')

        except Exception as e:
            print(f"Error saving to CSV: {e}")
            import traceback
            traceback.print_exc()
      
# =============================================================================
# RESORT-SPECIFIC SCRAPERS
# =============================================================================
class SnowbowlScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Snowbowl")
    
    def get_url(self):
        return "https://www.montanasnowbowl.com/report.php"
    
    def process_data(self, response):
            data_dict = {}
            
            try:
                # Parse polling data from HTML comments
                comment_pattern = r'(.*?)'
                polling_data = re.search(comment_pattern, response.text, re.DOTALL)
                

                if polling_data:
                    data_string = polling_data.group(1)
                    pairs = data_string.split('|~')
                    
                    for pair in pairs:
                        if '|' in pair:
                            key, value = pair.split('|')
                            data_dict[key.strip()] = value.strip()
            except Exception as e:
                print(f"Warning: Could not parse polling data for Snowbowl: {e}")
                
            # Initialize with all standard columns
            data = {col: None for col in self.STANDARD_COLUMNS}
            
            # Map resort data to standard columns
            data['summit_depth'] = data_dict.get('surface_depth_summit', '')
            data['base_depth'] = data_dict.get('surface_depth_base', '')
            data['temp_base'] = data_dict.get('current_temp_base', '')
            data['lifts_open'] = data_dict.get('operations_lifts', '')
            data['runs_open'] = data_dict.get('operations_trails', '')
            data['comments'] = data_dict.get('comments', '')
            
            # Format last_updated
            if data_dict.get('lastupdated'):
                try:
                    date_str = data_dict['lastupdated']
                    parsed_date = datetime.strptime(date_str, '%A %b %d, %Y %I:%M %p')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                except ValueError:
                    data['last_updated'] = data_dict.get('lastupdated', '')
            
            if not data['last_updated']:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                for row in soup.find_all('tr'):
                    row_text = row.get_text(strip=True)
                    if 'Updated:' in row_text:
                        match = re.search(r'Updated:(\w+\s+\w+\s+\d+,\s+\d{4}\s+\d+:\d+\s+[AP]M)', row_text)
                        if match:
                            try:
                                date_str = match.group(1).strip()
                                parsed_date = datetime.strptime(date_str, '%A %b %d, %Y %I:%M %p')
                                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                                break
                            except ValueError as e:
                                print(f"Error parsing Snowbowl table date: {e}")
                
            # HTML table parsing
            try:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                rows = soup.find_all('tr')
                for row in rows:
                    base_cell = row.find('td', {'bgcolor': '#ffffff'})
                    summit_cell = row.find('td', {'bgcolor': '#e6242b'})
                    
                    if base_cell and 'Base' in base_cell.text:
                        cells = row.find_all('td')
                        if len(cells) >= 5:
                            data['snow_24h_base'] = self.clean_numeric(cells[2].text.strip())
                            if not data['temp_base']:
                                data['temp_base'] = cells[3].text.strip()
                    
                    if summit_cell and 'Summit' in summit_cell.text:
                        cells = row.find_all('td')
                        if len(cells) >= 5:
                            data['snow_24h_summit'] = self.clean_numeric(cells[2].text.strip())
                            data['temp_summit'] = cells[3].text.strip()
                            data['wind_speed'] = self.clean_numeric(cells[4].text.strip())
                
                # Get surface conditions
                conditions = []
                condition_rows = soup.find_all('tr')
                for row in condition_rows:
                    condition_cell = row.find('td', {'bgcolor': '#e6242b'}) or row.find('td', {'bgcolor': '#ffffff'})
                    if condition_cell and any(cond in condition_cell.text for cond in ['Powder:', 'Packed Powder:', 'Packed:', 'Machine Groomed:', 'Firm Packed:', 'Spring Snow:']):
                        check_cell = row.find('img', {'src': re.compile(r'check\.gif$')})
                        if check_cell:
                            condition_text = condition_cell.text.strip().rstrip(':')
                            conditions.append(condition_text)
                
                if conditions:
                    data['conditions_surface'] = ', '.join(conditions)
            except Exception as e:
                print(f"Error parsing Snowbowl HTML table data: {e}")
            
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

    def _parse_snow_value(self, value_text):
        if not value_text or '|' not in value_text:
            return None, None
        try:
            summit_part, base_part = value_text.split('|')
            summit = re.search(r'(\d+\.?\d*)', summit_part)
            base = re.search(r'(\d+\.?\d*)', base_part)
            return (summit.group(1) if summit else None,
                    base.group(1) if base else None)
        except:
            return None, None

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        last_updated_elem = soup.find('span', class_='sr_inner_txt_sml')
        if last_updated_elem:
            try:
                date_str = last_updated_elem.text.strip()
                date_str = re.sub(r'\s+(?:PST|PDT)$', '', date_str)
                parsed_date = datetime.strptime(date_str, '%b %d %Y, %I:%M %p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError as e:
                print(f"Error parsing last_updated date: {e}")
        
        snowfall_rows = soup.find_all('div', class_=['light_row', 'dark_row'])
        for row in snowfall_rows:
            left = row.find('div', class_='left')
            right = row.find('div', class_='right')
            
            if left and right:
                label = left.find('p')
                value = right.find('p')
                
                if label and value:
                    label_text = label.text.strip()
                    value_text = value.text.strip()
                    
                    if '24-HR:' in label_text:
                        summit, base = self._parse_snow_value(value_text)
                        data['snow_24h_summit'] = self.clean_numeric(summit)
                        data['snow_24h_base'] = self.clean_numeric(base)
                    elif '48-HR:' in label_text:
                        summit, base = self._parse_snow_value(value_text)
                        data['snow_48h_summit'] = summit
                        data['snow_48h_base'] = base
                    elif 'Base:' in label_text:
                        data['base_depth'] = value_text.replace('"', '')
                    elif 'Summit:' in label_text:
                        data['summit_depth'] = value_text.replace('"', '')
                    elif 'Base Temp:' in label_text:
                        data['temp_base'] = value_text.replace('°', '')
                    elif 'Summit Temp:' in label_text:
                        data['temp_summit'] = value_text.replace('°', '')
                    elif 'Open:' in label_text:
                        data['runs_open'] = value_text
                    elif 'Conditions:' in label_text:
                        data['conditions_surface'] = value_text
        
        return data

class BigMountainScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("BigMountain")

    def get_url(self):
        return "https://skiwhitefish.com/snowreport/"

    def _get_weather_data(self):
        try:
            weather_url = "https://skiwhitefish.com/weather/"
            response = requests.get(weather_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            weather_data = {
                'temp_summit': None,
                'wind_speed': None
            }
            
            temp_cols = soup.find_all('div', class_='wp-block-column')
            for col in temp_cols:
                em = col.find('em')
                if em and 'Temperature' in em.text:
                    if br := col.find('br'):
                        temp_text = br.next_sibling.strip() if br.next_sibling else None
                        if temp_text:
                            temp_match = re.search(r'(-?\d+)', temp_text)
                            if temp_match:
                                weather_data['temp_summit'] = temp_match.group(1)
                
                elif em and 'Winds' in em.text:
                    if br := col.find('br'):
                        wind_text = br.next_sibling.strip() if br.next_sibling else None
                        if wind_text:
                            speed_match = re.search(r'@\s*(\d+(?:\.\d+)?)', wind_text)
                            if speed_match:
                                weather_data['wind_speed'] = self.clean_numeric(speed_match.group(1))
            
            return weather_data
        except Exception as e:
            print(f"Error getting weather data: {e}")
            return None

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # --- DATE PARSING FIX ---
        # Try to find the date, but default to NOW if we find data but miss the date
        date_found = False
        for p_tag in soup.find_all('p'):
            strong_tag = p_tag.find('strong')
            if strong_tag:
                text = strong_tag.text
                if re.search(r'\w+,\s+\w+\.?\s+\d{1,2},\s+\d{4}', text):
                    date_found = True
                    try:
                        clean_date = text.replace('.', '').replace('  ', ' ').strip().rstrip(':')
                        # Add default time if missing
                        if 'm' not in clean_date.lower(): 
                            clean_date += " 6:00 AM"
                        
                        # Normalize format
                        clean_date = re.sub(r'(\d)(am|pm)', r'\1 \2', clean_date, flags=re.I)
                        
                        parsed_date = datetime.strptime(clean_date, '%A, %b %d, %Y %I:%M %p')
                        data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                        break
                    except:
                        data['last_updated'] = text
        
        # --- FALLBACK: If we didn't find a date text, use current time ---
        if not data['last_updated']:
            data['last_updated'] = datetime.now().strftime('%Y-%m-%d %I:%M %p')
        
        # --- WEATHER ---
        weather_data = self._get_weather_data()
        if weather_data:
            data.update(weather_data)
        
        # --- SNOW TOTALS ---
        summit_cols = soup.find_all('div', class_='wp-block-column')
        for col in summit_cols:
            h5 = col.find('h5')
            p = col.find('p')
            if h5 and p:
                value = h5.text.strip().replace('"', '')
                label = p.text.strip()
                
                if 'Overnight Fresh' in label:
                    data['snow_overnight'] = value
                elif 'Past 24 Hours' in label and not data['snow_24h_summit']:
                    data['snow_24h_summit'] = self.clean_numeric(value)
                elif 'Settled Base' in label and not data['summit_depth']:
                    data['summit_depth'] = value
        
        base_area = False
        for col in summit_cols:
            h4 = col.find('h4')
            if h4 and 'Base Area' in h4.text:
                base_area = True
                continue
            
            if base_area:
                h5 = col.find('h5')
                p = col.find('p')
                if h5 and p:
                    value = h5.text.strip().replace('"', '')
                    label = p.text.strip()
                    
                    if 'Past 24 Hours' in label:
                        data['snow_24h_base'] = self.clean_numeric(value)
                    elif 'Settled Base' in label:
                        data['base_depth'] = value
        
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


class MaverickScraper(BaseSnowReportScraper):
    """
    Maverick scraper - extracts raw snow report text content directly from the 
    __BOOTSTRAP_STATE__ JSON object embedded in the HTML.
    """
    
    def __init__(self):
        super().__init__("Maverick")

    def get_url(self) -> str:
        """Returns the URL for the static snow report page."""
        return "https://www.skimaverick.com/weather"

    def _find_text_content(self, data: Any) -> Optional[str]:
        """
        Recursively searches for the raw text content inside the nested JSON.
        The raw text is always under the key 'insert' and contains 'Overnight'.
        """
        if isinstance(data, dict):
            if 'insert' in data and isinstance(data['insert'], str) and 'Overnight' in data['insert']:
                return data['insert']
            for value in data.values():
                result = self._find_text_content(value)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = self._find_text_content(item)
                if result:
                    return result
        return None

    def process_data(self, response) -> Dict:
        """
        1. Find the __BOOTSTRAP_STATE__ JSON string.
        2. Clean the JSON string of control characters.
        3. Use recursive search to extract and parse the raw snow report content.
        """
        data = self.blank_record()
        
        try:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find the script tag containing the JSON object
            script_tag = soup.find('script', string=re.compile(r'window\.__BOOTSTRAP_STATE__'))
            
            if not script_tag:
                 data['comments'] = "Could not find __BOOTSTRAP_STATE__ JSON script tag."
                 data['data_source'] = 'error_no_json'
                 return data
            
            # 1. Extract the raw JSON string
            json_text = script_tag.string.split('=', 1)[1].strip().rstrip(';')
            
            # 2. AGGRESSIVE CLEANING FIX: Remove all invalid control characters
            json_text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_text)
            
            # 3. Load the JSON
            bootstrap_data = json.loads(json_text)

            # 4. Use the recursive search to find the content block
            raw_data_content = self._find_text_content(bootstrap_data)
            
            if not raw_data_content:
                 data['comments'] = "Found bootstrap JSON but could not find the snow report data using recursive search."
                 data['data_source'] = 'error_no_content'
                 return data
            
            raw_text = normalize_text(raw_data_content)
            
            # --- Regex Parsing of the Raw Text Content ---
            
            # Extract and store explicit surface conditions if they exist
            surface_condition_match = re.search(r'Primary Surface Conditions:\s*(.*?)\n', raw_text, re.IGNORECASE | re.DOTALL)
            
            condition_value = ""
            if surface_condition_match:
                condition_value = surface_condition_match.group(1).strip()
            
            # FINAL FIX: If value is blank OR if it contains only the SECONDARY label (which happens if Primary is blank)
            if not condition_value or condition_value == "Secondary Surface Conditions:":
                 data['conditions_surface'] = None
            else:
                 data['conditions_surface'] = normalize_text(condition_value)

            patterns = {
                'snow_overnight': r'Overnight:\s*(\d+)',
                'snow_24h_base': r'Last 24 hours:\s*(\d+)',
                'snow_48h_base': r'Last 48 hours:\s*(\d+)',
                'snow_7day': r'Last 7 days:\s*(\d+)',
                'base_depth': r'Snow Depth \(base\):\s*(\d+)',
                'summit_depth': r'Snow Depth \(summit\):\s*(\d+)',
                'seasonal_snowfall': r'Annual Snowfall:\s*(\d+)',
            }
            
            for key, pattern in patterns.items():
                match = re.search(pattern, raw_text, re.IGNORECASE | re.DOTALL) 
                if match:
                    data[key] = self.clean_numeric(match.group(1))

            # Date extraction from the first line of raw text
            date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', raw_text)
            last_updated_date = None
            if date_match:
                try:
                    parsed_date = datetime.strptime(date_match.group(1), '%m/%d/%Y')
                    last_updated_date = parsed_date.strftime('%Y-%m-%d 06:00 AM')
                except Exception:
                    last_updated_date = date_match.group(1).strip()
            
            data['last_updated'] = last_updated_date
            
            # NEW COMMENT LOGIC: Clean, concise comment reflecting status.
            if last_updated_date and '10/14' in last_updated_date:
                 data['comments'] = "Resort status is static/pre-season. Data last updated on 10/14/2025."
            else:
                 data['comments'] = "Data extracted from static content on the website."
            
            data['data_source'] = 'json_extraction_api'

        except Exception as e:
            logging.error(f"Error parsing Maverick data from JSON: {e}", exc_info=True)
            data['comments'] = f"Fatal error processing JSON data: {str(e)}"
        
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
            # GreatDivideScraper(),
        ]
        
        resort_data_list = []
        for scraper in scrapers:
            logging.info(f"Fetching data for {scraper.resort_name}...")
            snow_data = scraper.get_snow_report()
            
            
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
