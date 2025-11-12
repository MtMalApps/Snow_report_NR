"""
Montana/Idaho Snow Report Scraper - Cleaned & Standardized VersionThis script scrapes snow reports from multiple ski resorts, stores data in CSV files,
and sends consolidated email reports to subscribers.Features:
- Standardized data schema across all resorts
- CSV backup storage
- Firebase Firestore integration (for future migration)
- Email notifications with HTML formatting
- SNOTEL backcountry data integration
- Powder alert notifications (configurable threshold)Environment Variables Required:
- GMAIL_ADDRESS: Your Gmail address for sending reports
- GMAIL_APP_PASSWORD: Gmail app-specific password
- FIREBASE_SERVICE_ACCOUNT: Path to Firebase service account JSON (for future)Author: Original code by user, cleaned and standardized by Claude
"""
import requests
import pandas as pd
from datetime import datetime, timedelta
from abc import ABC, abstractmethod
from bs4 import BeautifulSoup
import re
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional
import time
import logging
import json
from math import ceil
import base64
import sys
import warnings  # to suppress Pandas warnings in output
# Gmail API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from nws_storm_outlook_v3 import NWSStormOutlook
from googleapiclient.errors import HttpError



# =============================================================================
# CONFIGURATION
# =============================================================================

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6
# Resort coordinates for NWS forecasts
RESORT_COORDS = {
    "Snowbowl": {"lat": 46.9137, "lon": -113.9525},
    "Discovery": {"lat": 46.3861, "lon": -113.2144},
    "LookoutPass": {"lat": 47.4536, "lon": -115.7414},
    "BigMountain": {"lat": 48.4853, "lon": -114.3572},
    "LostTrail": {"lat": 45.7019, "lon": -113.9219},
    "TetonPass": {"lat": 47.9299, "lon": -112.8109},
    "Showdown": {"lat": 46.9939, "lon": -110.6647},
    "Blacktail": {"lat": 48.0067, "lon": -114.3589}
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
# =============================================================================
# BASE SCRAPER CLASS
# =============================================================================
class BaseSnowReportScraper(ABC):
    """
    Abstract base class for ski resort snow report scrapers.
    All resort-specific scrapers inherit from this class.
    """
    
    # --- MODIFIED SECTION: Added nws_forecast and snotel_data ---
    STANDARD_COLUMNS = [
        # Core metadata
        'resort',               # Resort name
        'date',                 # Date data were accessed (YYYY-MM-DD)
        'timestamp',            # Time data were accessed (YYYY-MM-DD HH:MM:SS)
        'last_updated',         # Resort's reported last update time
        
        # Snowfall measurements (NEW SNOW)
        'snow_overnight',       # Overnight snowfall (since lifts closed)
        'snow_24h_base',        # 24-hour snowfall at base
        'snow_24h_summit',      # 24-hour snowfall at summit
        'snow_48h_base',        # 48-hour snowfall at base
        'snow_48h_summit',      # 48-hour snowfall at summit
        'snow_7day',            # 7-day snowfall total (for future)
        
        # Snow depth measurements (TOTAL BASE DEPTH)
        'base_depth',           # Base depth (settled base)
        'summit_depth',         # Summit depth (settled base)
        
        # Temperature
        'temp_base',            # Base temperature
        'temp_summit',          # Summit temperature
        
        # Wind conditions
        'wind_speed',           # Wind speed measurement
        
        # Conditions
        'conditions_surface',   # Surface conditions (powder, packed, etc.)
        'weather_current',      # Current weather description
        
        # Operations
        'lifts_open',           # Number/fraction of lifts operating
        'runs_open',            # Number/fraction of runs open
        
        # Additional info
        'comments',             # Additional notes/forecast/comments
        
        # --- THESE ARE THE NEW COLUMNS FOR THE DASHBOARD ---
        'nws_forecast',         # The full NWS forecast object
        'snotel_data',          # The full SNOTEL data object

        # Old NWS fields (can be deprecated later if dashboard uses the object)
        'forecast_base_temp_high',    # Forecasted high temp at base
        'forecast_base_temp_low',     # Forecasted low temp at base
        'forecast_summit_temp_high',  # Forecasted high temp at summit
        'forecast_summit_temp_low',   # Forecasted low temp at summit
        'forecast_precipitation',     # Forecasted precipitation
        'forecast_snow',              # Forecasted snow amount
    ]
    # --- END OF MODIFIED SECTION ---

    def __init__(self, resort_name: str):
        self.resort_name = resort_name
        self.csv_filename = os.path.join(DATA_DIR, f"{resort_name}_snow_report.csv")
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        }

    @staticmethod
    def clean_numeric(value) -> float:
        """
        Clean numeric values consistently across all scrapers.
        Removes quotes, units, whitespace. Returns 0 for invalid values.
        """
        if value is None or value == '' or value == 'N/A':
            return 0.0
        
        value_str = str(value).strip()
        
        # Handle slash notation (take first number)
        if '/' in value_str:
            value_str = value_str.split('/')[0]
        
        # Remove common units and quotes
        for char in ['"', '°', 'F', 'C', 'mph', 'in', '%']:
            value_str = value_str.replace(char, '')
        
        # Extract just digits, decimal, and minus
        cleaned = ''.join(c for c in value_str if c.isdigit() or c in ['.', '-'])
        
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    @abstractmethod
    def get_url(self) -> str:
        """Return the URL to scrape for this resort."""
        pass

    @abstractmethod
    def process_data(self, response) -> Dict:
        """
        Process the HTTP response and return standardized data.
        
        Must return a dictionary with ALL keys from STANDARD_COLUMNS.
        Fields not available from the resort should be set to None.
        """
        pass    
    
    def get_snow_report(self) -> Optional[Dict]:
        """Fetch and process snow report data from the resort website."""
        try:
            # First try with standard SSL verification
            response = requests.get(self.get_url(), headers=self.headers, timeout=10)
            response.raise_for_status()
            
            snow_data = self.process_data(response)
            if snow_data:
                snow_data.update({
                    'resort': self.resort_name,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            return snow_data    
        except requests.exceptions.SSLError:
            # If SSL fails, try again without verification
            try:
                print(f"SSL verification failed for {self.resort_name}, retrying without verification...")
                response = requests.get(self.get_url(), headers=self.headers, timeout=10, verify=False)
                response.raise_for_status()
                
                snow_data = self.process_data(response)
                if snow_data:
                    snow_data.update({
                        'resort': self.resort_name,
                        'date': datetime.now().strftime('%Y-%m-%d'),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                return snow_data
                
            except Exception as e:
                print(f"Error fetching snow report for {self.resort_name} (without SSL verification): {e}")
                return None
        except requests.RequestException as e:
            print(f"Error fetching snow report for {self.resort_name}: {e}")
            return None
            
            
    def save_to_csv(self, data: Dict):
        """Save snow report data to CSV file with deduplication."""
        try:
            # Standardize last_updated parsing
            last_updated_dt = None
            if data.get('last_updated'):
                try:
                    for fmt in [
                        '%Y-%m-%d %I:%M %p',
                        '%Y-%m-%d %H:%M %p',
                        '%Y-%m-%d %I:%M%p',
                        '%Y-%m-%d %H:%M',
                        '%Y-%m-%d %I:%M  %p',
                        '%B %d, %Y %I:%M %p'
                    ]:
                        try:
                            last_updated_dt = datetime.strptime(data['last_updated'], fmt)
                            break
                        except ValueError:
                            continue
                    if last_updated_dt:
                        data['last_updated'] = last_updated_dt.strftime('%Y-%m-%d %I:%M %p')
                except Exception as e:
                    print(f"Could not parse last_updated: {data.get('last_updated')} - Error: {e}")
    
            # Standardize data with all required columns
            standardized_data = {col: None for col in self.STANDARD_COLUMNS}
            standardized_data.update({
                k: normalize_text(v) if isinstance(v, str) else v
                for k, v in data.items() if v is not None
            })
    
            # Create DataFrame
            df = pd.DataFrame([standardized_data], columns=self.STANDARD_COLUMNS)
    
            if os.path.exists(self.csv_filename):
                # Read existing CSV
                dtype_dict = {col: str for col in self.STANDARD_COLUMNS}
                existing_df = pd.read_csv(self.csv_filename, dtype=dtype_dict)
    
                # Convert date columns
                existing_df['date'] = pd.to_datetime(existing_df['date'], errors='coerce')
    
                def parse_last_updated(x):
                    formats = [
                        '%Y-%m-%d %I:%M %p',
                        '%Y-%m-%d %H:%M %p',
                        '%Y-%m-%d %I:%M%p',
                        '%Y-%m-%d %H:%M',
                        '%Y-%m-%d %I:%M  %p',
                        '%B %d, %Y %I:%M %p'
                    ]
                    for f in formats:
                        try:
                            return pd.to_datetime(x, format=f)
                        except Exception:
                            continue
                    return pd.NaT
    
                existing_df['last_updated'] = existing_df['last_updated'].apply(parse_last_updated)
    
                # Remove entries for the same date as this update (if we parsed it)
                if last_updated_dt:
                    existing_df = existing_df[
                        (existing_df['date'].dt.date != last_updated_dt.date()) &
                        (existing_df['last_updated'].dt.date != last_updated_dt.date())
                    ]
    
                # Prepare new row’s date fields
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
                df['last_updated'] = df['last_updated'].apply(parse_last_updated)
    
                # Append
                df = pd.concat([existing_df, df], ignore_index=True, sort=False)
    
            # Sort and deduplicate (keep latest per date)
            df = df.sort_values(['date', 'last_updated'], ascending=True).dropna(subset=['date'])
            df = df.drop_duplicates(subset='date', keep='last')
    
            # Convert datetime back to string for saving

            if pd.api.types.is_datetime64_any_dtype(df['date']):
                df['date'] = df['date'].dt.strftime('%Y-%m-%d')
            
            if pd.api.types.is_datetime64_any_dtype(df['last_updated']):
                df['last_updated'] = df['last_updated'].dt.strftime('%Y-%m-%d %I:%M %p')
    
            # Save to CSV
            df.to_csv(self.csv_filename, index=False, encoding='utf-8-sig')
            # print(f"Data saved to {self.csv_filename}") # This is now logged in main()
    
        except Exception as e:
            print(f"Error saving to CSV: {e}")
            import traceback
            traceback.print_exc()

            
# =============================================================================
# RESORT-SPECIFIC SCRAPERS
# =============================================================================
# =============================================================================
# UPDATED CLASS #1: SnowbowlScraper (with fixed last_updated parsing)
# =============================================================================

class SnowbowlScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Snowbowl")
    
    def get_url(self):
        return "https://www.montanasnowbowl.com/report.php"
    
    def process_data(self, response):
            data_dict = {}
            
            # --- FIX: Wrap comment-parsing in a try...except block ---
            # This part is fragile and depends on a hidden HTML comment.
            # If it fails, we want to continue and try parsing the HTML table.
            try:
                # Parse polling data from HTML comments
                comment_pattern = r'<!--polling-->(.*?)<!--/polling-->'
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
                # data_dict remains empty, and that's okay.
            # --- END FIX ---
                
            # Initialize with all standard columns
            data = {col: None for col in self.STANDARD_COLUMNS}
            
            # Map resort data to standard columns (will use data_dict if it was populated)
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
            
            # --- NEW FIX: Better table parsing for last_updated ---
            # If we didn't get last_updated from polling data, try the table
            if not data['last_updated']:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Look for "Updated:" in the table
                for row in soup.find_all('tr'):
                    row_text = row.get_text(strip=True)
                    if 'Updated:' in row_text:
                        # Extract date like "Monday Jul 14, 2025 10:19 AM"
                        match = re.search(r'Updated:(\w+\s+\w+\s+\d+,\s+\d{4}\s+\d+:\d+\s+[AP]M)', row_text)
                        if match:
                            try:
                                date_str = match.group(1).strip()
                                parsed_date = datetime.strptime(date_str, '%A %b %d, %Y %I:%M %p')
                                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                                break
                            except ValueError as e:
                                print(f"Error parsing Snowbowl table date: {e}")
            # --- END NEW FIX ---
                
            # This part will now *always* run, even if comment parsing failed
            try:
                # Parse HTML for table data
                soup = BeautifulSoup(response.text, 'html.parser')
                
                rows = soup.find_all('tr')
                for row in rows:
                    base_cell = row.find('td', {'bgcolor': '#ffffff'})
                    summit_cell = row.find('td', {'bgcolor': '#e6242b'})
                    
                    if base_cell and 'Base' in base_cell.text:
                        cells = row.find_all('td')
                        if len(cells) >= 5:
                            data['snow_24h_base'] = self.clean_numeric(cells[2].text.strip())
                            # Only update temp if not already set by polling data
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
        """Clean numeric values by removing non-numeric characters."""
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Get last updated time
        updated = soup.find('span', class_='tablinks')
        if updated:
            try:
                date_str = updated.text.replace('Updated:', '').strip()
                date_str = date_str.replace(' at ', ' ')
                parsed_date = datetime.strptime(date_str, '%B %d, %Y %I:%M %p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError:
                data['last_updated'] = updated.text.replace('Updated:', '').strip()
        
        # Find all report rows
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
        
        # Combine surface conditions
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
        """Parse 'X" Summit | Y" Base' format into (summit, base) tuple."""
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Get last updated time
        last_updated_elem = soup.find('span', class_='sr_inner_txt_sml')
        if last_updated_elem:
            try:
                date_str = last_updated_elem.text.strip()
                date_str = re.sub(r'\s+(?:PST|PDT)$', '', date_str)
                parsed_date = datetime.strptime(date_str, '%b %d %Y, %I:%M %p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError as e:
                print(f"Error parsing last_updated date: {e}")
        
        # Parse HTML data for snow measurements
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


# =============================================================================
# UPDATED CLASS #2: BigMountainScraper (with fixed last_updated parsing)
# =============================================================================

class BigMountainScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("BigMountain")

    def get_url(self):
        return "https://skiwhitefish.com/snowreport/"

    def _get_weather_data(self):
        """Get weather data from the weather page."""
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Find date and time
        date = None
        time = None
        for p_tag in soup.find_all('p'):
            strong_tag = p_tag.find('strong')
            if strong_tag:
                text = strong_tag.text
                if re.search(r'\w+,\s+\w+\.?\s+\d{1,2},\s+\d{4}', text):
                    date = text.rstrip(':')
                elif re.search(r'\d+(?::\d+)?\s*[ap]\.m\.', text, re.IGNORECASE):
                    time = re.search(r'(\d+(?::\d+)?\s*[ap]\.m\.)', text, re.IGNORECASE).group(1)
                    
        # Format last_updated
        # --- FIX: Handle case where we have date but no time ---
        if date:
            try:
                if time:
                    # We have both date and time
                    date_str = f"{date} {time}"
                    date_str = date_str.replace('.', '').replace('  ', ' ').strip()
                    if ':' not in time:
                        date_str = date_str.replace(' am', ':00 am').replace(' pm', ':00 pm')
                    parsed_date = datetime.strptime(date_str, '%A, %b %d, %Y %I:%M %p')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                else:
                    # We only have date, assume 6:00 AM (when they typically measure)
                    date_str = f"{date} 6:00 AM"
                    date_str = date_str.replace('.', '').replace('  ', ' ').strip()
                    parsed_date = datetime.strptime(date_str, '%A, %b %d, %Y %I:%M %p')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError as e:
                # If parsing fails, store whatever we have
                print(f"Error parsing BigMountain date: {e}")
                if time:
                    data['last_updated'] = f"{date} {time}"
                else:
                    data['last_updated'] = date
        # --- END FIX ---
        
        # Get weather data
        weather_data = self._get_weather_data()
        if weather_data:
            data.update(weather_data)
        
        # Process Summit data
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
        
        # Process Base Area data
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Extract "Last Updated" Section
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
        
        # Extract Snowfall Data
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
        
        # Extract Surface Conditions
        primary_condition = soup.find(string=re.compile("Surface Conditions \(Primary\)", re.I))
        secondary_condition = soup.find(string=re.compile("Surface Conditions \(Secondary\)", re.I))
        if primary_condition:
            primary_text = primary_condition.find_next("span").text.strip()
            if secondary_condition:
                secondary_text = secondary_condition.find_next("span").text.strip()
                data['conditions_surface'] = f"{primary_text}, {secondary_text}"
            else:
                data['conditions_surface'] = primary_text
        
        # Extract Lifts and Runs Open
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
        
        # Extract Comments
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Extract last updated time
        updated_elem = soup.find('p', class_='CDt4Ke', string=lambda text: 'Updated' in str(text) if text else False)
        if updated_elem:
            try:
                date_str = updated_elem.text.replace('Updated', '').strip()
                parsed_date = datetime.strptime(date_str, '%m/%d/%Y %I:%M%p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError:
                data['last_updated'] = updated_elem.text.replace('Updated', '').strip()
        
        # Find text paragraphs with snow and condition information
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


# =============================================================================
# UPDATED CLASS #3: ShowdownScraper (with fixed last_updated parsing)
# =============================================================================

class ShowdownScraper(BaseSnowReportScraper):
    def __init__(self):
        super().__init__("Showdown")

    def get_url(self):
        return "https://www.showdownmontana.com/snow-report"

    def process_data(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Find time string
        time_str = None
        large_paragraphs = soup.find_all('p', class_='sqsrte-large')
        for p in large_paragraphs:
            if 'Last Update' in p.text:
                time_str = p.text.strip()
                break
        
        # --- FIX: Parse the date-only format ---
        if time_str:
            try:
                # Extract date from "Last Update:10/12/2025"
                date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', time_str)
                if date_match:
                    date_str = date_match.group(1)
                    # Parse and assume 6:00 AM
                    parsed_date = datetime.strptime(date_str, '%m/%d/%Y')
                    # Add 6 AM time
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d') + ' 06:00 AM'
            except Exception as e:
                print(f"Error parsing Showdown date: {e}")
                data['last_updated'] = time_str
        # --- END FIX ---
        
        # Process snow blocks
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
        
        # Process text blocks
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
        
        # Initialize with all standard columns
        data = {col: None for col in self.STANDARD_COLUMNS}
        
        # Get the date from the article header
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
        
        # Look for snow report details in the sidebar
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
        
        # Look for grooming report details
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
        
        # Look for comments in the page content
        entry_content = soup.find('div', class_='entry-content')
        if entry_content:
            data['comments'] = entry_content.get_text(strip=True)
        
        return data

# =============================================================================
# SNOTEL DATA INTEGRATION (NEW API VERSION)
# =============================================================================

# =============================================================================
# SNOTEL DATA INTEGRATION (NEW API VERSION - v2)
# =============================================================================
# =============================================================================
# SNOTEL DATA INTEGRATION (NEW REST API VERSION)
# =============================================================================
class SNOTELDataFetcher:
    """
    Fetches backcountry snow data from SNOTEL stations via the AWDB REST API.
    Computes 24h depth/SWE changes, density, % of median, and snow category.
    Hardened with retries, backoff, and per-run cache.
    """
    STATION_INFO = {
        "Showdown":    {"triplet": "437:MT:SNTL",  "name": "Deadman Creek",   "lat": 46.79, "lon": -110.68, "elevation": 6440},
        "TetonPass":   {"triplet": "649:MT:SNTL",  "name": "Mount Lockhart",  "lat": 47.92, "lon": -112.82, "elevation": 6430},
        "Discovery":   {"triplet": "850:MT:SNTL",  "name": "Warm Springs",    "lat": 46.27, "lon": -113.16, "elevation": 7840},
        "LostTrail":   {"triplet": "727:MT:SNTL",  "name": "Saddle Mountain", "lat": 45.69, "lon": -113.97, "elevation": 7890},
        "Snowbowl":    {"triplet": "901:MT:SNTL",  "name": "Stuart Mountain", "lat": 47.00, "lon": -113.93, "elevation": 7270},
        "LookoutPass": {"triplet": "594:ID:SNTL",  "name": "Lookout",         "lat": 47.46, "lon": -115.70, "elevation": 5180},
        "Blacktail":   {"triplet": "1144:MT:SNTL", "name": "Blacktail Mtn",   "lat": 47.98, "lon": -114.35, "elevation": 5660},
        "BigMountain": {"triplet": "469:MT:SNTL",  "name": "Emery Creek",     "lat": 48.43, "lon": -113.94, "elevation": 4340},
    }
    
    def __init__(self):
        import os
        import logging
        from requests import Session
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util.retry import Retry
        except Exception:
            from requests.packages.urllib3.util.retry import Retry # fallback
        
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
        self._cache = {} # per-run cache by triplet

    def get_station_info(self, resort_name: str) -> Optional[Dict]:
        return self.STATION_INFO.get(resort_name)

    @staticmethod
    def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        from math import radians, sin, cos, asin, sqrt # Keep imports local
        R = 3958.7613 # miles
        phi1, lam1, phi2, lam2 = map(radians, [lat1, lon1, lat2, lon2])
        dphi, dlam = (phi2 - phi1), (lam2 - lam1)
        a = sin(dphi/2)**2 + cos(phi1) * cos(phi2) * sin(dlam/2)**2
        c = 2 * asin(sqrt(a))
        return R * c

    @staticmethod
    def _snow_category(density_pct: float) -> str:
        """Classify snow quality by density."""
        if density_pct is None: return "N/A"
        if density_pct < 6:   return "Blower Powder"
        if density_pct < 10:  return "Light Powder"
        if density_pct < 15:  return "Medium Powder"
        if density_pct < 25:  return "Heavy Powder"
        if density_pct < 40:  return "Wet Snow"
        return "Slush"

    def _create_empty_report(self, station_name=None, latest_observation=None, error_reason=None, triplet=None):
        """Return an empty-but-informative report structure."""
        return {
            'station_name': station_name or 'SNOTEL',
            'triplet': triplet,
            'snow_depth': 'N/A', # Use original key
            'swe': 'N/A',        # Use original key
            'density': 'N/A',
            'percent_of_median': 'N/A', 
            'snow_category': 'N/A',
            'latest_observation': latest_observation,
            'unavailable': bool(error_reason),
            'error_reason': error_reason 
        }

    def _fetch_from_rest_api(self, triplet: str) -> Dict:
        """
        Fetches SNOTEL data from the new REST API.
        Gets WTEQ, SNWD, and WTEQ_MEDIAN for the last 2 days.
        """
        if triplet in self._cache:
            return self._cache[triplet]
        
        # We need yesterday and today. beginDate=-1 gets yesterday and today.
        url = (
            "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
            f"?stationTriplets={triplet}"
            "&elements=WTEQ,SNWD"
            "&duration=DAILY"
            "&beginDate=-1" # Gets yesterday and today
            "&endDate=0"   # Gets up to today
            "&centralTendencyType=MEDIAN"
        )
        
        resp = self.session.get(url, timeout=self._timeout)
        resp.raise_for_status() 
        api_data = resp.json()

        results = {
            'today': {'date': None, 'snwd': None, 'wteq': None, 'wteq_median': None},
            'yesterday': {'date': None, 'snwd': None, 'wteq': None}
        }

        if not api_data or 'data' not in api_data[0]:
            raise ValueError("No 'data' field in API response.")

        # Loop through the elements (WTEQ, SNWD)
        for element_data in api_data[0].get('data', []):
            element_code = element_data['stationElement']['elementCode'].lower() # 'wteq' or 'snwd'
            values = element_data.get('values', [])
            
            if len(values) == 2:
                # We have yesterday and today
                yest_data = values[0]
                today_data = values[1]

                results['today']['date'] = today_data['date']
                results['yesterday']['date'] = yest_data['date']

                if element_code == 'wteq':
                    results['today']['wteq'] = today_data.get('value')
                    results['today']['wteq_median'] = today_data.get('median')
                    results['yesterday']['wteq'] = yest_data.get('value')
                elif element_code == 'snwd':
                    results['today']['snwd'] = today_data.get('value')
                    results['yesterday']['snwd'] = yest_data.get('value')
            
            elif len(values) == 1:
                # Only one day of data (probably start of season)
                today_data = values[0]
                results['today']['date'] = today_data['date']
                if element_code == 'wteq':
                    results['today']['wteq'] = today_data.get('value')
                    results['today']['wteq_median'] = today_data.get('median')
                elif element_code == 'snwd':
                    results['today']['snwd'] = today_data.get('value')
        
        self._cache[triplet] = results
        return results

    def get_snowfall_report(self, resort_name: str):
        """
        Safe wrapper that resolves station, fetches with REST API,
        calculates metrics, and returns a payload.
        """
        info = self.get_station_info(resort_name)
        if not info:
            return self._create_empty_report(station_name='SNOTEL', error_reason='no_station')
        
        station_name = info['name']
        triplet = info['triplet']
        
        try:
            api_results = self._fetch_from_rest_api(triplet)
        except Exception as e:
            self._log.warning("SNOTEL API fetch error for %s (%s): %s", resort_name, triplet, str(e))
            return self._create_empty_report(station_name=station_name, error_reason='timeout', triplet=triplet)
        
        # --- Perform Calculations ---
        today = api_results.get('today', {})
        yesterday = api_results.get('yesterday', {})

        # 1. 24-Hour Change
        snow_change = None
        swe_change = None
        if today.get('snwd') is not None and yesterday.get('snwd') is not None:
            snow_change = round(today['snwd'] - yesterday['snwd'], 1)
        
        if today.get('wteq') is not None and yesterday.get('wteq') is not None:
            swe_change = round(today['wteq'] - yesterday['wteq'], 3)

        # 2. Density
        density = None
        if snow_change is not None and swe_change is not None and snow_change > 0 and swe_change > 0.001:
            density = round((swe_change / snow_change) * 100, 1)
        
        # 3. Percent of Median
        percent_of_median = None
        if today.get('wteq') is not None and today.get('wteq_median') is not None:
            if today['wteq_median'] > 0:
                percent_of_median = round((today['wteq'] / today['wteq_median']) * 100)
            elif today['wteq'] > 0: # Median is 0 but we have snow
                percent_of_median = 999 # Use 999 to show it's way above median
            else: # Both are 0
                percent_of_median = 100 # 0 is 100% of 0

        # Check for new snow
        SNOW_THRESHOLD = 0.5
        SWE_THRESHOLD  = 0.01
        is_new_snow = (snow_change is not None and swe_change is not None and
                       snow_change >= SNOW_THRESHOLD and swe_change >= SWE_THRESHOLD)

        # Use the original key 'snow_depth' for the 24-hour *change*
        # Use the original key 'swe' for the 24-hour *change*
        # This maintains compatibility with your email template and uploader
        
        if not is_new_snow:
            return {
                'station_name': station_name,
                'triplet': triplet,
                'snow_depth': snow_change if snow_change is not None else 'N/A',
                'swe': swe_change if swe_change is not None else 'N/A',
                'density': 'N/A',
                'percent_of_median': percent_of_median,
                'snow_category': 'N/A',
                'latest_observation': today.get('date'),
                'unavailable': False,
                'error_reason': "No new snow"
            }
        
        return {
            'station_name': station_name,
            'triplet': triplet,
            'snow_depth': snow_change, 
            'swe': swe_change,
            'density': density,
            'percent_of_median': percent_of_median,
            'snow_category': self._snow_category(density),
            'latest_observation': today.get('date'),
            'unavailable': False,
            'error_reason': None
        }


    def get_all_snowfall_reports(self) -> Dict[str, Dict]:
            """
            Fetches SNOTEL data for ALL stations in STATION_INFO in a single batch API call.
            
            Processes all calculations and returns a dictionary mapped by RESORT NAME.
            e.g. {"Showdown": {report...}, "Discovery": {report...}}
            """
            self._log.info("Starting batch SNOTEL API fetch...")
            
            # 1. Create a map of triplet -> resort_name for easy lookup
            triplet_to_resort = {v['triplet']: k for k, v in self.STATION_INFO.items()}
            
            # 2. Get a comma-separated list of all triplets
            all_triplets = ",".join(triplet_to_resort.keys())
            
            url = (
                "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
                f"?stationTriplets={all_triplets}"
                "&elements=WTEQ,SNWD"
                "&duration=DAILY"
                "&beginDate=-1" # Gets yesterday and today
                "&endDate=0"   # Gets up to today
                "&centralTendencyType=MEDIAN"
            )
            
            # This will hold the final reports, keyed by resort_name
            final_reports = {}
    
            try:
                # 3. Make the SINGLE API call
                resp = self.session.get(url, timeout=self._timeout)
                resp.raise_for_status()
                api_data_list = resp.json()
                
                if not api_data_list:
                    raise ValueError("API response was empty.")
    
                # 4. Process the response (it's a list of station data)
                for station_data in api_data_list:
                    triplet = station_data.get('stationTriplet')
                    resort_name = triplet_to_resort.get(triplet)
                    
                    if not resort_name:
                        self._log.warning(f"Got data for unknown triplet {triplet}, skipping.")
                        continue
                    
                    station_name = self.STATION_INFO[resort_name]['name']
                    
                    # --- This is the same logic from _fetch_from_rest_api ---
                    results = {
                        'today': {'date': None, 'snwd': None, 'wteq': None, 'wteq_median': None},
                        'yesterday': {'date': None, 'snwd': None, 'wteq': None}
                    }
                    
                    if 'data' not in station_data:
                        self._log.warning(f"No 'data' field in API response for {triplet}")
                        final_reports[resort_name] = self._create_empty_report(station_name, error_reason='no_data', triplet=triplet)
                        continue
    
                    for element_data in station_data.get('data', []):
                        element_code = element_data['stationElement']['elementCode'].lower()
                        values = element_data.get('values', [])
                        
                        if len(values) == 2:
                            yest_data, today_data = values[0], values[1]
                            results['today']['date'] = today_data['date']
                            results['yesterday']['date'] = yest_data['date']
                            if element_code == 'wteq':
                                results['today']['wteq'] = today_data.get('value')
                                results['today']['wteq_median'] = today_data.get('median')
                                results['yesterday']['wteq'] = yest_data.get('value')
                            elif element_code == 'snwd':
                                results['today']['snwd'] = today_data.get('value')
                                results['yesterday']['snwd'] = yest_data.get('value')
                        elif len(values) == 1:
                            today_data = values[0]
                            results['today']['date'] = today_data['date']
                            if element_code == 'wteq':
                                results['today']['wteq'] = today_data.get('value')
                                results['today']['wteq_median'] = today_data.get('median')
                            elif element_code == 'snwd':
                                results['today']['snwd'] = today_data.get('value')
                    # --- End logic from _fetch_from_rest_api ---
    
                    # --- This is the same calculation logic from get_snowfall_report ---
                    today = results.get('today', {})
                    yesterday = results.get('yesterday', {})
    
                    snow_change = None
                    swe_change = None
                    if today.get('snwd') is not None and yesterday.get('snwd') is not None:
                        snow_change = round(today['snwd'] - yesterday['snwd'], 1)
                    
                    if today.get('wteq') is not None and yesterday.get('wteq') is not None:
                        swe_change = round(today['wteq'] - yesterday['wteq'], 3)
    
                    density = None
                    if snow_change is not None and swe_change is not None and snow_change > 0 and swe_change > 0.001:
                        density = round((swe_change / snow_change) * 100, 1)
                    
                    percent_of_median = None
                    if today.get('wteq') is not None and today.get('wteq_median') is not None:
                        if today['wteq_median'] > 0:
                            percent_of_median = round((today['wteq'] / today['wteq_median']) * 100)
                        elif today['wteq'] > 0:
                            percent_of_median = 999
                        else:
                            percent_of_median = 100
                    
                    SNOW_THRESHOLD = 0.5
                    SWE_THRESHOLD  = 0.01
                    is_new_snow = (snow_change is not None and swe_change is not None and
                                   snow_change >= SNOW_THRESHOLD and swe_change >= SWE_THRESHOLD)
    
                    if not is_new_snow:
                        final_reports[resort_name] = {
                            'station_name': station_name, 'triplet': triplet,
                            'snow_depth': snow_change if snow_change is not None else 'N/A',
                            'swe': swe_change if swe_change is not None else 'N/A',
                            'density': 'N/A', 'percent_of_median': percent_of_median,
                            'snow_category': 'N/A', 'latest_observation': today.get('date'),
                            'unavailable': False, 'error_reason': "No new snow"
                        }
                    else:
                        final_reports[resort_name] = {
                            'station_name': station_name, 'triplet': triplet,
                            'snow_depth': snow_change, 'swe': swe_change,
                            'density': density, 'percent_of_median': percent_of_median,
                            'snow_category': self._snow_category(density),
                            'latest_observation': today.get('date'),
                            'unavailable': False, 'error_reason': None
                        }
                    # --- End calculation logic ---
    
            except Exception as e:
                self._log.error(f"SNOTEL batch API fetch error: {e}", exc_info=True)
                # If the whole call fails, create empty reports for all
                for resort_name in triplet_to_resort.values():
                    if resort_name not in final_reports:
                         final_reports[resort_name] = self._create_empty_report(
                             station_name=self.STATION_INFO[resort_name]['name'], 
                             error_reason='batch_api_fail', 
                             triplet=self.STATION_INFO[resort_name]['triplet']
                         )
            
            # 5. Fill in any missing resorts (in case they weren't in the API response)
            for resort_name in self.STATION_INFO.keys():
                if resort_name not in final_reports:
                    self._log.warning(f"No SNOTEL data returned for {resort_name} in batch.")
                    final_reports[resort_name] = self._create_empty_report(
                        station_name=self.STATION_INFO[resort_name]['name'], 
                        error_reason='missing_from_batch', 
                        triplet=self.STATION_INFO[resort_name]['triplet']
                    )
                    
            self._log.info(f"Batch SNOTEL processing complete. Found data for {len(final_reports)} stations.")
            return final_reports


class EmailSnowReportMessenger:
    """
    Manages email distribution of snow reports with dark theme HTML formatting.
    Complete rewrite with modern responsive design.
    """
    def __init__(self, gmail_address: str, credentials_path: str = "credentials.json",
                 token_path: str = "token.json"):
        self.gmail_address = gmail_address
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.subscribers_file = os.path.join(DATA_DIR, "snow_report_subscribers.csv")
        self.nws_outlook = NWSStormOutlook()
    
    # -------------------------------------------------------------------------
    # UTILITY METHODS
    # -------------------------------------------------------------------------
    def _fmt_inches(self, value) -> str:
        """Format inches with single quote mark."""
        if value is None:
            return ""
        s = str(value).strip()
        if not s or s.lower() in ('', 'none', 'n/a'):
            return ""
        
        import re
        s = s.replace('"', '"').replace('"', '"').replace('″', '"')
        s = re.sub(r'"{2,}', '"', s).strip()
        
        if s.endswith('"') and re.match(r'^\s*-?\d+(\.\d+)?"\s*$', s):
            return s.strip()
        
        digits = ''.join(c for c in s if c.isdigit() or c in ".-")
        return f'{digits}"' if digits else ""
    
    def _sanitize_text(self, value: str) -> str:
        """Normalize text for HTML output."""
        if value is None:
            return ""
        s = normalize_text(str(value))
        s = re.sub(r'"+', '"', s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    
    def get_24hr_snow(self, resort_data: Dict) -> float:
        """Extract 24-hour snow amount (prefer summit, fallback base)."""
        try:
            snow = resort_data.get('snow_24h_summit') or resort_data.get('snow_24h_base', '0')
            snow = ''.join(c for c in str(snow) if c.isdigit() or c == '.')
            return float(snow or 0)
        except (ValueError, TypeError):
            return 0.0
    
    def get_master_sort_key(self, resort_data: Dict) -> tuple:
        """Sort key: fresh first, then by snow amount (desc), then recency, then name."""
        from datetime import datetime
        resort_name = resort_data.get('resort', '')
        today = datetime.now().date()
        
        last_updated_str = resort_data.get('last_updated', '')
        last_updated_date = None
        if last_updated_str and last_updated_str != 'N/A':
            for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                try:
                    last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                    break
                except ValueError:
                    continue
        
        snow_amount = self.get_24hr_snow(resort_data)
        is_fresh = 0 if (last_updated_date == today) else 1
        snow_sort = -float(snow_amount)
        date_sort = -(last_updated_date.toordinal() if last_updated_date else 0)
        return (is_fresh, snow_sort, date_sort, resort_name)

    def _get_display_name(self, resort_name: str) -> str:
        """Get display-friendly resort name."""
        name_map = {
            "LookoutPass": "Lookout Pass",
            "BigMountain": "Big Mountain",
            "LostTrail": "Lost Trail",
            "TetonPass": "Teton Pass",
            "Blacktail": "Blacktail Mountain",
            "Snowbowl": "Snowbowl",
            "Discovery": "Discovery",
            "Showdown": "Showdown"
        }
        return name_map.get(resort_name, resort_name)

    def _check_powder_alert(self, resort_data_list: List[Dict]) -> bool:
            """Check if any resort has powder alert worthy snow."""
            today = datetime.now().date()
            for resort_data in resort_data_list:
                last_updated_str = resort_data.get('last_updated', '')
                if not last_updated_str:
                    continue
                last_updated_date = None
                if last_updated_str:
                    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                        try:
                            last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                            break
                        except ValueError:
                            continue
                
                # --- THIS IS THE FIX ---
                # It now checks if the date is EXACTLY today.
                is_current = last_updated_date == today if last_updated_date else False
    
                if is_current:
                # --- END OF FIX ---
                    if self.get_24hr_snow(resort_data) >= POWDER_ALERT_THRESHOLD:
                        return True # Found a current powder day, no need to check others
            return False
        
    
    # -------------------------------------------------------------------------
    # CHART PREPARATION
    # -------------------------------------------------------------------------

    def _prepare_chart_data(self, resort_data_list: List[Dict]) -> List[Dict]:
                """Prepare 5-day snow accumulation chart data from CSV files."""
                chart_data = []
                today = datetime.now().date()
        
                for resort_data in resort_data_list:
                    resort_name = resort_data['resort']
                    csv_file = os.path.join(DATA_DIR, f"{resort_name}_snow_report.csv")
        
                    try:
                        if os.path.exists(csv_file):
                            df = pd.read_csv(csv_file)
                            
                            # Use 'date' (when script ran) for main filtering
                            df['report_date'] = pd.to_datetime(df['date'], format='%Y-%m-%d', errors='coerce')
                            
                            # --- FIX: Also parse last_updated to check for staleness ---
                            df['last_updated_dt'] = pd.to_datetime(df['last_updated'], errors='coerce').dt.date
                            # --- END FIX ---
                            
                            if df['report_date'].isna().all():
                                raise ValueError("All report_dates are NaT")
                                
                            df = df.dropna(subset=['report_date']).sort_values('report_date', ascending=False)
                            most_recent_update = df['report_date'].iloc[0].date()
                            days_since_update = (today - most_recent_update).days
        
                            values = [0] * 5
                            date_labels = [(today - timedelta(days=i)) for i in range(5)]
        
                            for i, date in enumerate(date_labels):
                                day_data = df[df['report_date'].dt.date == date]
                                
                                if not day_data.empty:
                                    # Get the first (latest) row for that day
                                    row = day_data.iloc[0]
                                    
                                    # --- FIX: Check if the data is stale ---
                                    # Check if the 'last_updated' date matches the 'report_date'
                                    is_stale = (row['last_updated_dt'] is pd.NaT) or (row['last_updated_dt'] != date)
                                    if is_stale:
                                        values[i] = 0 # Force 0 if stale
                                        continue # Skip to next day
                                    # --- END FIX ---
    
                                    # If not stale, proceed as normal
                                    snow = row['snow_24h_summit']
                                    if pd.isna(snow):
                                        snow = row['snow_24h_base']
                                    if isinstance(snow, str):
                                        snow = snow.replace('"', '').strip()
                                    try:
                                        values[i] = float(snow) if snow else 0
                                    except Exception:
                                        values[i] = 0
        
                            total = sum(values)
                            chart_data.append({
                                'resort': resort_name,
                                'values': values,
                                'date_labels': date_labels,
                                'total': total,
                                'has_current_data': days_since_update < 2,
                                'most_recent_update': most_recent_update
                            })
                        else:
                            raise FileNotFoundError("No CSV file")
                    except Exception as e:
                        # Catch any error (no CSV, parsing error, etc.) and append empty data
                        print(f"Chart prep error for {resort_name}: {e}")
                        chart_data.append({
                            'resort': resort_name,
                            'values': [0, 0, 0, 0, 0],
                            'date_labels': [today - timedelta(days=i) for i in range(5)],
                            'total': 0,
                            'has_current_data': False,
                            'most_recent_update': None
                        })
        
                return chart_data
        
        
    # -------------------------------------------------------------------------
    # HTML GENERATION - HEADER
    # -------------------------------------------------------------------------

    def _generate_email_header(self, is_powder_alert: bool = False) -> str:
                """Generate email header with title and date."""
                today = datetime.now()
                title = "POWDER ALERT - Daily Snow Report" if is_powder_alert else "Daily Snow Report"
                
                # --- THIS IS THE FIX ---
                # Create the date string for the visible badge
                date_string = today.strftime('%A, %B %d, %Y')
                
                # This is a zero-width non-joiner. It's an invisible character
                # that "fills" the preheader space, stopping Gmail from previewing the body.
                preheader_text = "&zwnj;&nbsp;"
                
                return f"""<!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Snow Report</title>
          <style>
            /* --- START MOBILE FIX --- */
            @media only screen and (max-width: 620px) {{
              .container {{ width: 100% !important; }}
              .stack {{ 
                display: block !important; 
                width: 100% !important; 
                padding: 0 !important;
                text-align: left !important; /* Force left-align when stacked */
              }}
              .p-sm {{ padding: 14px !important; }}
              .center-sm {{ text-align: center !important; }}
              .hide-sm {{ display: none !important; }}
              .stack-item {{ padding-left: 0 !important; padding-top: 12px; }}
        
              /* New rules for the summary table */
              .stack-row {{
                display: block !important;
                width: 100% !important;
              }}
              .stack-row-col {{
                display: block !important;
                width: 100% !important;
                box-sizing: border-box !important; /* Ensures padding doesn't break width */
                border-radius: 0 !important;
                border: 0 !important; /* Remove individual borders */
              }}
              /* Re-add borders for stacking */
              .stack-row-col-first {{
                border-radius: 10px 10px 0 0 !important;
                border-top: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
                border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
                border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
              }}
              .stack-row-col-last {{
                border-radius: 0 0 10px 10px !important;
                border-bottom: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
                border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
                border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
              }}
              .stack-row-col-mid {{
                border-left: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
                border-right: 1px solid {EmailTheme.BORDER_SUBTLE} !important;
              }}
            }}
            /* --- END MOBILE FIX --- */
          </style>
        </head>
        <body style="margin:0; padding:0; background:{EmailTheme.BG_OUTER}; color:{EmailTheme.BG_OUTER};">
          <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent; mso-hide:all;">
            {preheader_text}
          </div>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{EmailTheme.BG_OUTER};">
            <tr>
              <td align="center" style="padding:24px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="700" class="container" style="width:700px; max-width:100%; background:{EmailTheme.BG_CONTAINER}; border-radius:16px; overflow:hidden; box-shadow:0 8px 24px {EmailTheme.SHADOW};">
                  <tr>
                    <td style="padding:24px 24px 16px 24px; background:linear-gradient(135deg,{EmailTheme.GRADIENT_START},{EmailTheme.GRADIENT_END});">
                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                        <tr>
                          <td class="stack" style="vertical-align:middle; color:{EmailTheme.TEXT_PRIMARY}; font-family:Arial, Helvetica, sans-serif; font-size:13px;">
                            <div style="letter-spacing:.08em; text-transform:uppercase; opacity:.9;">{title}</div>
                            <div style="font-size:22px; font-weight:700; color:{EmailTheme.TEXT_WHITE}; margin-top:4px;">Northern Rockies</div>
                          </td>
                          <td class="stack center-sm" style="vertical-align:middle; text-align:right;">
                            <span style="display:inline-block; background:{EmailTheme.BG_OUTER}; color:{EmailTheme.ACCENT_BLUE}; font-family:Arial, Helvetica, sans-serif; font-size:12px; padding:8px 12px; border-radius:999px; border:1px solid {EmailTheme.DATE_BADGE_BORDER};">
                              {date_string}
                            </span>
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
        """

    # -------------------------------------------------------------------------
    # HTML GENERATION - Powder Alert
    # -------------------------------------------------------------------------

    def _generate_powder_alert_banner(self, sorted_resorts: List[Dict]) -> str:
            """Generate powder alert banner with resort badges."""
            powder_resorts = []
            today = datetime.now().date()
            
            for resort_data in sorted_resorts:
                last_updated_str = resort_data.get('last_updated', '')
                if not last_updated_str:
                    continue
                
                last_updated_date = None
                for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                    try:
                        last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                        break
                    except ValueError:
                        continue
                
                # --- THIS IS THE FIX ---
                # It now checks if the date is EXACTLY today.
                is_current = last_updated_date == today if last_updated_date else False
                
                if is_current:
                # --- END OF FIX ---
                    snow_amount = self.get_24hr_snow(resort_data)
                    if snow_amount >= POWDER_ALERT_THRESHOLD:
                        powder_resorts.append({
                            'name': self._get_display_name(resort_data['resort']),
                            'snow': int(round(snow_amount))
                        })
    
            if not powder_resorts:
                return ""
    
            # Sort by snow amount, descending
            powder_resorts.sort(key=lambda x: x['snow'], reverse=True)
            
            badges_html = []
            for resort in powder_resorts:
                # Note: These colors are from the v7 HTML, but they are not in the EmailTheme
                # class because they are unique to this one banner. This is fine.
                badges_html.append(
                    f'<span style="display:inline-block; background:#113b63; border:1px solid rgba(148,163,184,.25); color:#cfe7ff; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:700; margin: 4px;">'
                    f"{resort['name']} &middot; {resort['snow']}\""
                    '</span>'
                )
    
            return f"""
              <tr>
                <td style="padding:16px 24px; background:#051d33; border-bottom:1px solid {EmailTheme.BORDER_SUBTLE};">
                  <div style="font-family:Arial, Helvetica, sans-serif; font-size:14px; color:{EmailTheme.TEXT_PRIMARY}; line-height:1.5;">
                    <div style="font-weight:800; color:{EmailTheme.TEXT_WHITE}; margin-bottom:6px;">Powder Alert (&ge; {POWDER_ALERT_THRESHOLD}" in last 24 hrs)</div>
                    <div style="display:flex; flex-wrap:wrap; margin: -4px;">
                      {''.join(badges_html)}
                    </div>
                  </div>
                </td>
              </tr>
            """
        
    # -------------------------------------------------------------------------
    # HTML GENERATION - SUMMARY TABLE WITH CHARTS
    # -------------------------------------------------------------------------
    
    def _generate_summary_table(self, sorted_resorts: List[Dict], chart_data: List[Dict]) -> str:
            """Generate summary table with 5-day charts."""
            html = [f"""
              <tr>
                <td style="padding:20px 24px 6px 24px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate; border-spacing:0 8px;">
                    
                    <tr class="hide-sm">
                      <td style="width:38%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">Resort</td>
                      <td style="width:22%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">24-hr</td>
                      <td style="width:40%; font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:{EmailTheme.TEXT_SECONDARY};">5-Day Trend</td>
                    </tr>
                    """]
            
            today = datetime.now().date()
            
            for resort_data in sorted_resorts:
                resort_name = resort_data['resort']
                display_name = self._get_display_name(resort_name)
                snow_24hr = int(round(self.get_24hr_snow(resort_data)))
                
                # Get chart data for this resort
                resort_chart = next((c for c in chart_data if c['resort'] == resort_name), None)
                
                # Determine badge
                last_updated_str = resort_data.get('last_updated', '')
                last_updated_date = None
                if last_updated_str:
                    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                        try:
                            last_updated_date = datetime.strptime(last_updated_str, fmt).date()
                            break
                        except ValueError:
                            continue
                
                is_current = last_updated_date == today if last_updated_date else False
                is_powder = is_current and snow_24hr >= POWDER_ALERT_THRESHOLD
                
                # --- FIX: If data is historic, force 24hr snow to 0 ---
                if not is_current:
                    snow_24hr = 0
                # --- END FIX ---
    
                badge_html = ""
                if is_powder:
                    badge_html = f'<span style="display:inline-block; background:{EmailTheme.POWDER_BADGE_BG}; color:{EmailTheme.POWDER_BADGE_TEXT}; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; margin-left:8px;">Powder</span>'
                elif not is_current:
                    badge_html = f'<span style="display:inline-block; background:{EmailTheme.HISTORIC_BADGE_BG}; color:{EmailTheme.HISTORIC_BADGE_TEXT}; font-size:11px; padding:2px 8px; border-radius:999px; margin-left:8px;">Historic</span>'
                
                # Format last updated
                last_updated_display = last_updated_str.replace(' ', ' ') if last_updated_str else "N/A"
                
                # Generate mini chart
                chart_html = self._generate_mini_chart(resort_chart['values'] if resort_chart else [0,0,0,0,0])
                
                html.append(f"""
                      <tr class="stack-row">
                        <td class="stack-row-col stack-row-col-first" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-radius:10px 0 0 10px; border:1px solid {EmailTheme.BORDER_SUBTLE};">
                          <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:14px; font-weight:700;">
                            {display_name}{badge_html}
                          </div>
                          <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:11px; margin-top:2px;">Last updated: {last_updated_display}</div>
                        </td>
                        <td class="stack-row-col stack-row-col-mid" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-top:1px solid {EmailTheme.BORDER_SUBTLE}; border-bottom:1px solid {EmailTheme.BORDER_SUBTLE}; border-right:1px solid {EmailTheme.BORDER_SUBTLE};">
                          <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:14px; font-weight:700; text-align:right;">{snow_24hr}"</div>
                          <div class="hide-lg" style="display:none; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; text-align:left;">24-hr Snow</div>
                        </td>
                        <td class="stack-row-col stack-row-col-last" style="padding:10px 12px; background:{EmailTheme.BG_CARD}; border-radius:0 10px 10px 0; border:1px solid {EmailTheme.BORDER_SUBTLE};">
                          {chart_html}
                          <div class="hide-lg" style="display:none; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; text-align:left; margin-top:4px;">5-Day Trend</div>
                        </td>
                      </tr>
                """)
            
            # --- MODIFIED SECTION ---
            # Add legend with day names
            legend_items = []
            # Generate date labels (e.g., "Sat", "Sun", "Mon", "Tue")
            date_objects = [today - timedelta(days=i) for i in range(5)]
            # Reverse them to go from oldest to newest
            date_objects.reverse()
            labels = [d.strftime('%a') for d in date_objects] 
    
            for i, color in enumerate(EmailTheme.CHART_COLORS):
                legend_items.append(
                    f'<span style="padding:6px 8px;">'
                    f'<span style="display:inline-block; width:14px; height:14px; background:{color}; border-radius:3px; vertical-align:middle;"></span>'
                    f'<span style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:12px; margin-left:6px; vertical-align:middle;">{labels[i]}</span>'
                    '</span>'
                )
            # --- END OF MODIFIED SECTION ---
    
            html.append(f"""
                          </table>
                          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 8px;">
                            <tr>
                              <td style="padding:8px 0 2px 0;">
                                {''.join(legend_items)}
                              </td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding:10px 24px 2px 24px;">
                          <div style="height:1px; background:{EmailTheme.DIVIDER_GRADIENT};"></div>
                        </td>
                      </tr>
                """)
            
            return ''.join(html)

    def _generate_mini_chart(self, values: List[float]) -> str:
            """Generate mini bar chart HTML."""
            # Reverse values (oldest to newest: Mon -> Today)
            values_reversed = list(reversed(values))
            max_val = max(max(values_reversed), 10) # At least 10 for scaling
            
            if all(v == 0 for v in values_reversed):
                return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;">
          <tr><td style="height:14px; background:{EmailTheme.BG_OUTER};"></td></tr>
        </table>
    """
            
            html = ['<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-radius:6px; overflow:hidden;"><tr>']
            
            for i, (val, color) in enumerate(zip(values_reversed, EmailTheme.CHART_COLORS)):
                if val > 0:
                    width_pct = (val / max_val) * 100
                    # Logic for text color based on background
                    text_color = EmailTheme.BG_CONTAINER if color == EmailTheme.CHART_COLORS[0] else EmailTheme.TEXT_WHITE
                    html.append(f'<td style="width:{width_pct:.1f}%; height:14px; background:{color}; color:{text_color}; font-size:11px; text-align:center;">{int(round(val))}"</td>')
                else:
                    # Empty space
                    if i == 0 or values_reversed[i-1] == 0:
                        html.append(f'<td style="height:14px; background:{EmailTheme.BG_CARD};"></td>')
            
            html.append('</tr></table>')
            return ''.join(html)
        
        
    # -------------------------------------------------------------------------
    # HTML GENERATION - NWS STORM BANNER (if applicable)
    # -------------------------------------------------------------------------
 
    # Use this simpler version if you prefer the old layout
    def _generate_nws_storm_banner(self, nws_outlooks: List[Dict]) -> str:
        """
        This is the OLD function, updated with the NEW v7 colors.
        It generates the simple "STORM WATCH" banner for significant weather.
        """
        if not nws_outlooks:
            return ""
        
        STORM_SNOW_THRESHOLD = 4.0
        STORM_RAIN_THRESHOLD = 0.5
        
        heavy_snow = []
        heavy_rain = []
        
        for outlook in nws_outlooks:
            if not outlook.get('outlook'):
                continue
            
            ol = outlook['outlook']
            resort_name = self._get_display_name(outlook['resort_name'])
            
            snow_amount = ol.get('total_snow_inches', 0)
            precip_amount = ol.get('total_precip_inches', 0)
            
            if snow_amount >= STORM_SNOW_THRESHOLD:
                heavy_snow.append(f"{resort_name}: {snow_amount:.1f}\" snow")
            
            if precip_amount >= STORM_RAIN_THRESHOLD and snow_amount < 1.0:
                prob = ol.get('precip_probability_max', 0)
                heavy_rain.append(f"{resort_name}: {precip_amount:.2f}\" ({prob}% chance)")
    
        if not heavy_snow and not heavy_rain:
            return ""
        
        snow_text = ""
        if heavy_snow:
            snow_text = f"""
                <b style="color: {EmailTheme.ACCENT_BLUE};">❄️ Snow Forecast (48hr):</b><br>
                {' · '.join(heavy_snow[:4])}
            """
        
        rain_text = ""
        if heavy_rain:
            rain_text = f"""
                <b style="color: {EmailTheme.ACCENT_BLUE};">🌧️ Heavy Rain Forecast (48hr):</b><br>
                {' · '.join(heavy_rain[:2])}
            """
            
        return f"""
          <tr>
            <td style="padding: 20px 24px 10px 24px;">
              <div style="border-top: 1px solid {EmailTheme.BORDER_SUBTLE}; padding-top: 20px; border-bottom: 1px solid {EmailTheme.BORDER_SUBTLE}; padding-bottom: 20px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="width: 40px; vertical-align: top; padding-top: 4px;">
                      <span style="font-size: 28px; line-height: 1;">⚠️</span>
                    </td>
                    <td style="font-family: Arial, Helvetica, sans-serif; color: {EmailTheme.TEXT_PRIMARY}; padding-left: 12px;">
                      <div style="font-size: 16px; font-weight: 700; color: {EmailTheme.STORM_TEXT};">STORM WATCH</div>
                      <div style="font-size: 13px; color: {EmailTheme.TEXT_SECONDARY}; line-height: 1.5; margin-top: 8px;">
                        Heavy precipitation forecast - see detailed reports below.
                        <div style="margin-top: 8px;">{snow_text}</div>
                        <div style="margin-top: 8px;">{rain_text}</div>
                      </div>
                    </td>
                  </tr>
                </table>
              </div>
            </td>
          </tr>
        """
    
    # -------------------------------------------------------------------------
    # HTML GENERATION - DETAILED RESORT SECTIONS
    # -------------------------------------------------------------------------

    def _generate_detailed_section(self, resort_data: Dict, snotel_data: Optional[Dict], nws_outlook: Optional[Dict], station_info: Optional[Dict]) -> str:
            """Generate detailed resort section with hero stats, data table, NWS, and SNOTEL."""
            resort_name = resort_data['resort']
            display_name = self._get_display_name(resort_name)
            last_updated = resort_data.get('last_updated', 'N/A')
            
            snow_24hr = int(round(self.get_24hr_snow(resort_data)))
            
            # --- FIX: Check if data is current before assigning snow/base ---
            today = datetime.now().date()
            last_updated_date = None
            if last_updated:
                for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M %p'):
                    try:
                        last_updated_date = datetime.strptime(last_updated, fmt).date()
                        break
                    except ValueError:
                        continue
            
            is_current = last_updated_date == today if last_updated_date else False
    
            # If data is historic, force 24h snow to 0
            if not is_current:
                snow_24hr = 0
                
            # Use base_depth, fallback to summit_depth if base is missing
            base_depth_val = resort_data.get('base_depth') or resort_data.get('summit_depth')
            base_depth = self._fmt_inches(base_depth_val)
            
            # Generate data rows
            data_rows = self._generate_resort_data_rows(resort_data, is_current)
            
            # Generate NWS outlook box
            nws_box = self._generate_nws_outlook_box(nws_outlook, display_name) if nws_outlook else ""
            
            # Generate SNOTEL box
            snotel_box = self._generate_snotel_box(snotel_data, station_info, resort_name) if snotel_data else ""
            
            return f"""
          <tr>
            <td style="padding:18px 24px 6px 24px;">
              <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:18px; font-weight:800;">{display_name}</div>
              <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:4px;">Last updated {last_updated}</div>
              <div style="margin-top:8px;">
                <div style="display:inline-block; background:{EmailTheme.HERO_BADGE_24H_BG}; color:{EmailTheme.HERO_BADGE_24H_TEXT}; padding:9px 14px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:14px; font-weight:700; border:1px solid {EmailTheme.BORDER_MEDIUM}; margin-right:8px;">
                  24h: <span style="color:{EmailTheme.HERO_BADGE_24H_SPAN};">{snow_24hr}"</span>
                </div>
                <div style="display:inline-block; background:{EmailTheme.HERO_BADGE_BASE_BG}; color:{EmailTheme.HERO_BADGE_BASE_TEXT}; padding:9px 14px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:14px; font-weight:700; border:1px solid {EmailTheme.BORDER_MEDIUM};">
                  Base: <span style="color:{EmailTheme.HERO_BADGE_BASE_SPAN};">{base_depth if base_depth else 'N/A'}</span>
                </div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 24px 20px 24px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td class="stack" style="width:56%; vertical-align:top; padding-right:12px;">
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{EmailTheme.BG_CARD}; border-radius:12px;">
                      {data_rows}
                    </table>
                  </td>
                  <td class="stack stack-item" style="width:44%; vertical-align:top; padding-left:12px;">
                    {snotel_box}
                    <div style="height:10px;"></div>
                    {nws_box}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 8px 24px;">
              <div style="height:1px; background:{EmailTheme.DIVIDER_GRADIENT};"></div>
            </td>
          </tr>
    """

    def _generate_resort_data_rows(self, resort_data: Dict, is_current: bool) -> str:
            """Generate data rows for resort table."""
            rows = []
            
            # Add summit fields to the map
            field_map = [
                ('snow_24h_base', '24‑hr Snow – Base', True),
                ('snow_24h_summit', '24‑hr Snow – Summit', True),
                ('snow_overnight', 'Overnight Snow', False),
                ('base_depth', 'Base Depth', False),
                ('summit_depth', 'Summit Depth', False),
                ('temp_base', 'Base Temp', False),
                ('temp_summit', 'Summit Temp', False),
                ('wind_speed', 'Wind Speed', False),
                ('lifts_open', 'Lifts Open', False),
                ('runs_open', 'Runs Open', False),
                ('conditions_surface', 'Surface', False),
                ('weather_current', 'Weather', False),
            ]
            
            # --- FIX: Define fields to show even if historic ---
            historic_safe_fields = ['base_depth', 'summit_depth']
    
            for field_key, label, is_highlight in field_map:
                # --- FIX: If not current, skip all fields that are not "safe" ---
                if not is_current and field_key not in historic_safe_fields:
                    continue
    
                value = resort_data.get(field_key)
                if value is None or str(value).strip().lower() in ('', 'none', 'n/a', '0', '0.0'):
                    # Special case: allow '0"' for snow if it's current
                    if is_current and field_key in ['snow_24h_base', 'snow_24h_summit', 'snow_overnight'] and str(value) == '0':
                         pass # Allow '0"' to be shown
                    else:
                         continue # Skip all other empty/zero values
                
                # Format value
                if field_key in ['snow_24h_base', 'snow_24h_summit', 'snow_overnight', 'base_depth', 'summit_depth']:
                    formatted_value = self._fmt_inches(value)
                elif field_key in ['temp_base', 'temp_summit']:
                    formatted_value = f"{self._sanitize_text(value)}°F" if value else ""
                elif field_key == 'wind_speed':
                    formatted_value = f"{self._sanitize_text(value)} mph" if value else ""
                else:
                    formatted_value = self._sanitize_text(value)
                
                if not formatted_value:
                    continue
                
                font_weight = '700' if is_highlight else '400'
                
                rows.append(f"""
        <tr>
          <td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">{label}</td>
          <td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:{font_weight}; text-align:right;">{formatted_value}</td>
        </tr>""")
            
            # Remove border from last row
            if rows:
                rows[-1] = rows[-1].replace(f'border-bottom:1px solid {EmailTheme.BORDER_MICRO}', 'border-bottom:0')
    
            return ''.join(rows)

    def _generate_nws_outlook_box(self, nws_outlook: Optional[Dict], display_name: str) -> str:
                """Generate NWS 48-hour outlook box."""
                if not nws_outlook or not nws_outlook.get('outlook'):
                    return ""
                
                ol = nws_outlook['outlook']
                
                # --- START FIX ---
                # Use the correct keys from nws_storm_outlook_v3.py
                
                prob = ol.get('precip_probability_max', 0)
                precip = f"{ol.get('total_precip_inches', 0):.2f}\" liquid ({prob}% chance)"
                
                snow_val = ol.get('total_snow_inches', 0)
                snow = f"{snow_val:.1f}\" expected"
                
                temp_high = ol.get('temp_high_f', 'N/A')
                temp_low = ol.get('temp_low_f', 'N/A')
                temps = f"High {temp_high}°F, Low {temp_low}°F"
                
                snow_level = ol.get('snow_level_feet', 'N/A')
                snow_level_str = f"{snow_level} ft" if snow_level != 'N/A' and snow_level is not None else "N/A"
                
                # The 'conditions' key in the NWS script returns a list, so join it.
                conditions_list = ol.get('conditions')
                if isinstance(conditions_list, list):
                    conditions = ", ".join(conditions_list)
                else:
                    conditions = ol.get('conditions', 'N/A') # Fallback if it's a string
                
                # --- END FIX ---
                
                return f"""
                      <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                        <tr>
                          <td style="padding:12px;">
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_GREEN}; font-weight:800; font-size:12px;">48-Hour Weather Outlook - {display_name}</div>
                            <div style="height:8px;"></div>
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                              <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Precipitation</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{precip}</td></tr>
                              <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Snow</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{snow}</td></tr>
                              <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Temps</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{temps}</td></tr>
                              <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Snow Level</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{snow_level_str}</td></tr>
                              <tr><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%; vertical-align: top;">Conditions</td><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{conditions}</td></tr>
                            </table>
                          </td>
                        </tr>
                      </table>
        """

    def _generate_snotel_box(self, snotel_data: Dict, station_info: Optional[Dict], resort_name: str) -> str:
            """Generate SNOTEL backcountry data box with graceful fallback text."""
            if not snotel_data:
                return ""
    
            station_name = snotel_data.get('station_name') or (station_info.get('name') if station_info else 'SNOTEL')
    
            # Distance + elevation
            distance_str = "nearby"
            elevation_str = "—"
            if station_info and {'lat','lon'} <= station_info.keys():
                try:
                    rc = RESORT_COORDS.get(resort_name)
                    if rc:
                        distance_str = f"{self.haversine_miles(rc['lat'], rc['lon'], station_info['lat'], station_info['lon']):.1f} mi"
                except Exception:
                    pass
            elev = station_info.get('elevation') if station_info else None
            elevation_str = f"{int(elev)} ft" if isinstance(elev,(int,float)) else "—"
    
            # Check for unavailable status
            unavailable = snotel_data.get('unavailable', False)
            if unavailable:
                error_reason = snotel_data.get('error_reason')
                msg = "SNOTEL data unavailable"
                if error_reason:
                    msg += f" ({error_reason})"
                status_info = f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; color:{EmailTheme.TEXT_SECONDARY}; margin-top:4px;">{msg}.</div>'
                
                # --- Return a simplified box for errors ---
                return f"""
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                        <tr>
                            <td style="padding:12px 12px 6px 12px;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_BLUE}; font-weight:700; font-size:12px;">SNOTEL</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; margin-top:2px;">{station_name}</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:2px;">{distance_str} • {elevation_str}</div>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding:6px 12px 12px 12px;">
                                {status_info}
                            </td>
                        </tr>
                    </table>
                """
    
            # If we reach here, data is available.
            def inches(value, fallback='0"'):
                if value in (None, '', 'N/A'): return fallback
                try:
                    if isinstance(value, (int, float)): return f'{value:.2f}"'.rstrip('0').rstrip('.')
                    if isinstance(value, str) and value.endswith('"'): return value
                    digits = ''.join(c for c in str(value) if c.isdigit() or c in ".-")
                    return f'{digits}"' if digits else fallback
                except Exception: return fallback
    
            snow_depth = snotel_data.get('snow_depth', 0)
            snow_depth_display = inches(snow_depth, fallback='0"') if snow_depth != 'melting/settling' else '0"'
            swe_display = inches(snotel_data.get('swe', 0), fallback='0"')
            density = snotel_data.get('density', 'N/A')
            density_display = f"{density}%" if density not in ('N/A', None) else "—"
            snow_quality = snotel_data.get('snow_category', 'N/A')
            
            # --- NEWLY ADDED ---
            percent_of_median = snotel_data.get('percent_of_median')
            median_display = f"{int(percent_of_median)}%" if isinstance(percent_of_median, (int, float)) else "—"
            # --- END NEW ---
    
            # --- Safely check if snow_depth is a positive number ---
            snow_depth_numeric = 0.0
            if isinstance(snow_depth, (int, float)):
                snow_depth_numeric = snow_depth
            
            # SNOTEL Badge Logic
            if 'Powder' in str(snow_quality):
                badge_html = f'<div style="margin-top:8px; display:inline-block; background:{EmailTheme.SNOTEL_POWDER_BG}; color:{EmailTheme.SNOTEL_POWDER_TEXT}; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">{snow_quality}</div>'
            elif snow_depth != 'melting/settling' and snow_depth_numeric > 0:
                    badge_html = f'<div style="margin-top:8px; display:inline-block; background:#eab308; color:#0b1220; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">{snow_quality}</div>'
            else:
                badge_html = f'<div style="margin-top:8px; display:inline-block; background:{EmailTheme.SNOTEL_HISTORIC_BG}; color:{EmailTheme.SNOTEL_HISTORIC_TEXT}; padding:4px 8px; border-radius:999px; font-family:Arial, Helvetica, sans-serif; font-size:11px; font-weight:700;">—</div>'
    
            return f"""
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:{EmailTheme.BG_CARD}; border:1px solid {EmailTheme.BORDER_MEDIUM}; border-radius:12px;">
                    <tr>
                        <td style="padding:12px 12px 6px 12px;">
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.ACCENT_BLUE}; font-weight:700; font-size:12px;">SNOTEL</div>
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; margin-top:2px;">{station_name}</div>
                            <div style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px; margin-top:2px;">{distance_str} • {elevation_str}</div>
                            {badge_html}
                        </td>
                    </tr>
                    <tr>
                        <td style="padding:6px 12px 12px 12px;">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">24-hr Snow Δ</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{snow_depth_display}</td></tr>
                                <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">24-hr SWE Δ</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{swe_display}</td></tr>
                                <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Density</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{density_display}</td></tr>
                                
                                <tr><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">% of Median</td><td style="padding:8px 10px; border-bottom:1px solid {EmailTheme.BORDER_MICRO}; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:700; text-align:right;">{median_display}</td></tr>
                                
                                <tr><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_MUTED}; font-size:13px; width:52%;">Latest Obs</td><td style="padding:8px 10px; border-bottom:0; font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_PRIMARY}; font-size:13px; font-weight:400; text-align:right;">{snotel_data.get('latest_observation', 'N/A')}</td></tr>
                            </table>
                        </td>
                    </tr>
                </table>
            """


    # -------------------------------------------------------------------------
    # HTML GENERATION - FOOTER
    # -------------------------------------------------------------------------
    def _generate_footer(self) -> str:
            """Generate email footer (v7, web button removed)."""
            return f"""
              <tr>
                <td style="padding:22px 24px 28px 24px; background:{EmailTheme.BG_CONTAINER}; border-top:1px solid {EmailTheme.BORDER_SUBTLE};">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td class="stack center-sm" style="font-family:Arial, Helvetica, sans-serif; color:{EmailTheme.TEXT_SECONDARY}; font-size:12px;">
                        Data sources: resort public snow reports, NWS API, &amp; nearby SNOTEL stations. Values are best‑effort scrapes and may lag resort updates.
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """
    
    
    # -------------------------------------------------------------------------
    # MAIN EMAIL ASSEMBLY
    # -------------------------------------------------------------------------

    def _build_email_html(self, sorted_resorts: List[Dict], snotel: SNOTELDataFetcher, is_powder_alert: bool = False, nws_outlooks: List[Dict] = None) -> str:
            """Build complete HTML email with dark theme."""
            html_parts = []
            
            # Header
            html_parts.append(self._generate_email_header(is_powder_alert))
            
            # Powder alert banner
            if is_powder_alert:
                html_parts.append(self._generate_powder_alert_banner(sorted_resorts))
            
            # Summary table with charts
            chart_data = self._prepare_chart_data(sorted_resorts)
            html_parts.append(self._generate_summary_table(sorted_resorts, chart_data))
            
            # NWS storm banner (if applicable)
            if nws_outlooks:
                html_parts.append(self._generate_nws_storm_banner(nws_outlooks))
            
            # Detailed resort sections
            for resort_data in sorted_resorts:
                resort_name = resort_data['resort']
                
                # --- MODIFIED SECTION ---
                # Get SNOTEL data *from the object we already fetched*
                snotel_data = resort_data.get('snotel_data') 
                # --- END MODIFICATION ---
    
                station_info = snotel.get_station_info(resort_name)
                # Get NWS outlook for this resort
                nws_outlook = next((item for item in nws_outlooks if item['resort_name'] == resort_name), None) if nws_outlooks else None
                
                html_parts.append(self._generate_detailed_section(resort_data, snotel_data, nws_outlook, station_info))
            
            # Footer
            html_parts.append(self._generate_footer())
            
            return ''.join(html_parts)
    
    # -------------------------------------------------------------------------
    # EMAIL SENDING
    # -------------------------------------------------------------------------

    def send_consolidated_report(self, resort_data_list: List[Dict]):
            """
            Send consolidated snow report email via Gmail API OAuth2.
            (PRODUCTION-SAFE: Use this in your GitHub script)
            Data is now pre-compiled with NWS and SNOTEL info.
            """
            if not os.path.exists(self.subscribers_file):
                print(f"No subscribers file found at {self.subscribers_file}")
                return
            try:
                subscribers_df = pd.read_csv(self.subscribers_file)
                recipients = subscribers_df['email'].tolist()
            except Exception as e:
                print(f"Error reading subscribers: {e}")
                return
            if not recipients:
                print("No subscribers found")
                return
            
            # Sort resorts (resort_data_list already contains full NWS/SNOTEL data)
            sorted_resorts = sorted(resort_data_list, key=self.get_master_sort_key)
            
            # Check powder alert
            is_powder_alert = self._check_powder_alert(sorted_resorts)
            
            # --- MODIFIED SECTION ---
            # We just need to create the objects the email builder expects.
            # The SNOTEL object is needed for the .get_station_info() method.
            snotel_fetcher_for_email = SNOTELDataFetcher() 
            
            nws_outlooks_for_email = []
            for resort_data in sorted_resorts:
                # Re-create the simple list format that _build_email_html expects
                nws_outlooks_for_email.append({
                    "resort_name": resort_data["resort"],
                    "outlook": resort_data.get("nws_forecast") # Get the data we saved
                })
            # --- END OF MODIFIED SECTION ---
    
            # Build email HTML
            email_html = self._build_email_html(sorted_resorts, snotel_fetcher_for_email, is_powder_alert, nws_outlooks_for_email)
            
            # === DEBUG: write out the EXACT html we are about to send ===
            debug_path = os.path.expanduser("~/email_last_sent.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(email_html)
            print("EMAIL DEBUG | Theme CHART_COLORS:", EmailTheme.CHART_COLORS)
            print("EMAIL DEBUG | Wrote exact outgoing HTML to:", debug_path)
            
            # --- Create the date string here ---
            date_string = datetime.now().strftime('%A, %B %d, %Y')
    
            # Subject
            if is_powder_alert:
                max_snow = max(self.get_24hr_snow(r) for r in sorted_resorts)
                best_resort = max(sorted_resorts, key=lambda x: self.get_24hr_snow(x))['resort']
                # Add the date string to the subject
                subject = f"POWDER ALERT: {int(max_snow)}\" at {self._get_display_name(best_resort)} - {date_string}"
            else:
                # Add the date string to the subject
                subject = f"Daily Snow Report - {date_string}"
            
            # Gmail API send
            try:
                creds = None
                SCOPES = ['https://www.googleapis.com/auth/gmail.send'] # Define scope
                
                if os.path.exists(self.token_path):
                    creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
                
                # --- PRODUCTION-SAFE AUTH LOGIC ---
                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        print("Refreshing expired token...")
                        creds.refresh(Request())
                        # Save the refreshed token back to the file
                        with open(self.token_path, 'w') as token:
                            token.write(creds.to_json())
                    else:
                        print(f"FATAL: No valid token.json found at {self.token_path}")
                        print("This script must be run with a valid token.json file.")
                        print("Please generate one locally and add it to GitHub Secrets.")
                        return 
                # --- END OF PRODUCTION-SAFE LOGIC ---
    
                service = build('gmail', 'v1', credentials=creds)
                
                message = MIMEMultipart('alternative')
                message['Subject'] = subject
                message['From'] = self.gmail_address
                message['To'] = ', '.join(recipients)
                
                html_part = MIMEText(email_html, 'html')
                message.attach(html_part)
                raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
                
                service.users().messages().send(userId='me', body={'raw': raw_message}).execute()
                
                print(f"Report sent successfully to {len(recipients)} recipients")
                
            except HttpError as error:
                print(f"Gmail API error: {error}")
                if 'invalid_grant' in str(error):
                    print("FATAL: invalid_grant. The token in GitHub Secrets is expired or revoked.")
                    print("Please generate a new token.json locally and update the GMAIL_TOKEN_JSON secret.")
            except Exception as e:
                print(f"Error sending email: {e}")

# =============================================================================
# Email color and theme constants for snow/weather HTML reports (v7)
# =============================================================================
class EmailTheme:
    # === Backgrounds ===
    BG_OUTER = "#0b1220"      # Main page background
    BG_CONTAINER = "#0f172a"  # Main email body container
    BG_CARD = "#0b1220"       # Background for summary rows & detail tables (now matches outer)

    # === Text Colors ===
    TEXT_PRIMARY = "#e2e8f0"     # Main text (was #e5e7eb)
    TEXT_SECONDARY = "#94a3b8"   # Muted text (unchanged)
    TEXT_MUTED = "#cbd5e1"       # Muter text (was #9ca3af), used in SNOTEL tables
    TEXT_WHITE = "#ffffff"     # Unchanged

    # === Accent Colors ===
    ACCENT_BLUE = "#93c5fd"      # Main accent (date badge, SNOTEL title)
    ACCENT_GREEN = "#a7f3d0"     # NWS Outlook title
    
    # === Borders ===
    BORDER_SUBTLE = "rgba(148,163,184,.15)" # Summary table rows
    BORDER_MEDIUM = "rgba(148,163,184,.25)" # SNOTEL/NWS box border
    BORDER_MICRO = "rgba(148,163,184,.12)"  # Per-resort data table rows
    DATE_BADGE_BORDER = "rgba(255,255,255,0.12)" # Header date badge border
    SHADOW = "rgba(0,0,0,0.35)" # Main container shadow

    # === Header ===
    GRADIENT_START = "#1e293b"  # Was #1f1b3a
    GRADIENT_END   = "#0ea5e9"  # Was #3b2a7a

    # === Powder/Historic Badges (Summary Table) ===
    POWDER_BADGE_BG = "#22c55e"
    POWDER_BADGE_TEXT = "#052e16"
    HISTORIC_BADGE_BG = "#334155"
    HISTORIC_BADGE_TEXT = "#e2e8f0"
    
    # === SNOTEL Badges (Detail View) ===
    SNOTEL_POWDER_BG = "#22c55e"
    SNOTEL_POWDER_TEXT = "#04120a"
    SNOTEL_HISTORIC_BG = "#64748b" # Used for "—" or "N/A"
    SNOTEL_HISTORIC_TEXT = "#e2e8f0"

    # === Storm Watch Banner (Regional Outlook) ===
    STORM_BANNER_BG = "#190b0b"
    STORM_BANNER_BORDER = "rgba(248,113,113,.25)"
    STORM_TEXT = "#fecaca"
    STORM_BADGE_BG = "#7f1d1d"
    STORM_BADGE_BORDER = "rgba(248,113,113,.35)"
    ALERT_RED_LIGHT = "#fca5a5" # For heavy rain text
    TOTAL_BADGE_BG = "#172554"
    TOTAL_BADGE_TEXT = "#bfdbfe"
    TOTAL_BADGE_BORDER = "rgba(148,163,184,.35)"
    
    # === Hero Badges (Detail View) ===
    HERO_BADGE_24H_BG = "#172554"
    HERO_BADGE_24H_TEXT = "#93c5fd"
    HERO_BADGE_24H_SPAN = "#ffffff"
    HERO_BADGE_BASE_BG = "#042f2e"
    HERO_BADGE_BASE_TEXT = "#5eead4"
    HERO_BADGE_BASE_SPAN = "#e2e8f0"
    
    # === Dividers ===
    DIVIDER_GRADIENT = "linear-gradient(90deg, rgba(148,163,184,.2), rgba(148,163,184,.06))"

    # === Mini-Chart Colors (5-Day) ===
    CHART_COLORS = [
        "#cbd5e1", # D-4
        "#38bdf8", # D-3
        "#a78bfa", # D-2
        "#14b8a6", # D-1
        "#1e40af", # Today
    ]


# =============================================================================
# MAIN EXECUTION
# =============================================================================

# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    
    # Suppress Pandas warnings in output...
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning)

    
    
    """Main execution function."""
    log_file = os.path.join(LOG_DIR, "snow_report.log")
    
    # --- THIS IS THE UPDATED LOGGING CONFIG ---
    # We import sys at the top of the script for this
    logging.basicConfig(
        handlers=[
            logging.FileHandler(log_file),     # Saves to file
            logging.StreamHandler(sys.stdout)  # Prints to console
        ],
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    # --- END OF UPDATE ---
    
    try:
        logging.info("Starting snow report script")
        
        # Get Gmail address from environment variable
        gmail_address = os.environ.get('GMAIL_ADDRESS')
        
        # Get OAuth credential paths from environment (or use defaults)
        credentials_path = os.environ.get('GMAIL_CREDENTIALS_PATH', 'credentials.json')
        token_path = os.environ.get('GMAIL_TOKEN_PATH', 'token.json')
        
        if not gmail_address:
            raise ValueError("GMAIL_ADDRESS environment variable must be set")
        
        # Initialize messenger with OAuth
        messenger = EmailSnowReportMessenger(
            gmail_address=gmail_address,
            credentials_path=credentials_path,
            token_path=token_path
        )
        logging.info(f"Checking subscribers file at: {messenger.subscribers_file}")
        
        if os.path.exists(messenger.subscribers_file):
            logging.info("Subscribers file found")
        else:
            logging.warning("Subscribers file not found - will create new one")
        
        # Initialize SNOTEL/NWS fetchers
        snotel_fetcher = SNOTELDataFetcher()
        nws_fetcher = NWSStormOutlook()

        # === EFFICIENCY CHANGE ===
        # Call SNOTEL *once* before the loop
        logging.info("Fetching all SNOTEL data in one batch...")
        try:
            all_snotel_data = snotel_fetcher.get_all_snowfall_reports()
            logging.info(f"Batch SNOTEL fetch complete. Got data for {len(all_snotel_data)} stations.")
        except Exception as e:
            logging.error(f"FATAL: SNOTEL batch fetch failed: {e}", exc_info=True)
            # Create an empty dict so the script can continue
            all_snotel_data = {}
        # === END EFFICIENCY CHANGE ===

        # Initialize scrapers
        scrapers = [
            SnowbowlScraper(),
            DiscoveryScraper(),
            LookoutPassScraper(),
            BigMountainScraper(),
            LostTrailScraper(),
            TetonPassScraper(),
            ShowdownScraper(),
            BlacktailScraper(),
        ]
        
        # Run scrapers and collect data
        resort_data_list = []
        for scraper in scrapers:
            logging.info(f"Fetching data for {scraper.resort_name}...")
            snow_data = scraper.get_snow_report()
            
            if snow_data:
                resort_name = snow_data["resort"]
                
                # === EFFICIENCY CHANGE ===
                # 1. Get SNOTEL data from the pre-fetched dictionary
                logging.info(f"Looking up pre-fetched SNOTEL for: {resort_name}")
                snow_data['snotel_data'] = all_snotel_data.get(resort_name)
                
                if snow_data['snotel_data'] is None:
                    # This is a fallback in case the batch call failed
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
                # === END EFFICIENCY CHANGE ===

                # 2. Get NWS data (still must be one by one)
                logging.info(f"Fetching NWS for: {resort_name}")
                coords = RESORT_COORDS.get(resort_name)
                if coords:
                    try:
                        snow_data['nws_forecast'] = nws_fetcher.get_outlook(coords["lat"], coords["lon"])
                        time.sleep(0.3) # Respect NWS API rate limits
                    except Exception as e:
                        logging.warning(f"NWS fetch failed for {resort_name}: {e}")
                        snow_data['nws_forecast'] = None
                else:
                    snow_data['nws_forecast'] = None

                resort_data_list.append(snow_data)
                
                # Now save the *complete* data to the CSV
                scraper.save_to_csv(snow_data)  
                logging.info(f"Complete data saved to CSV for {scraper.resort_name}")
            else:
                logging.warning(f"No data retrieved for {scraper.resort_name}")
        
        # Send consolidated report
        if resort_data_list:
            logging.info("Sending consolidated report...")
            try:
                # Pass the *complete* list to the messenger
                messenger.send_consolidated_report(resort_data_list)
                logging.info("Consolidated report sent successfully")
            except Exception as e:
                logging.error(f"Error sending report: {str(e)}", exc_info=True)
        else:
            logging.warning("No resort data to send")
    except Exception as e:
        logging.error(f"Error in main function: {str(e)}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
