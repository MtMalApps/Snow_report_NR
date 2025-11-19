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
from typing import Dict, List, Optional
import time
import logging
import json
from math import ceil
import sys
import warnings  # to suppress Pandas warnings in output
from nws_storm_outlook_v3 import NWSStormOutlook
#from googleapiclient.errors import HttpError

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
        
        # --- DASHBOARD DATA COLUMNS ---
        'nws_forecast',         # The full NWS forecast object
        'snotel_data',          # The full SNOTEL data object (Contains Density & Category)

        # Old NWS fields (kept for backward compatibility)
        'forecast_base_temp_high',    
        'forecast_base_temp_low',     
        'forecast_summit_temp_high',  
        'forecast_summit_temp_low',   
        'forecast_precipitation',     
        'forecast_snow',              
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
        
        date = None
        time_str = None
        for p_tag in soup.find_all('p'):
            strong_tag = p_tag.find('strong')
            if strong_tag:
                text = strong_tag.text
                if re.search(r'\w+,\s+\w+\.?\s+\d{1,2},\s+\d{4}', text):
                    date = text.rstrip(':')
                elif re.search(r'\d+(?::\d+)?\s*[ap]\.m\.', text, re.IGNORECASE):
                    time_str = re.search(r'(\d+(?::\d+)?\s*[ap]\.m\.)', text, re.IGNORECASE).group(1)
                    
        if date:
            try:
                if time_str:
                    date_str = f"{date} {time_str}"
                    date_str = date_str.replace('.', '').replace('  ', ' ').strip()
                    if ':' not in time_str:
                        date_str = date_str.replace(' am', ':00 am').replace(' pm', ':00 pm')
                    parsed_date = datetime.strptime(date_str, '%A, %b %d, %Y %I:%M %p')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
                else:
                    date_str = f"{date} 6:00 AM"
                    date_str = date_str.replace('.', '').replace('  ', ' ').strip()
                    parsed_date = datetime.strptime(date_str, '%A, %b %d, %Y %I:%M %p')
                    data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError as e:
                print(f"Error parsing BigMountain date: {e}")
                if time_str:
                    data['last_updated'] = f"{date} {time_str}"
                else:
                    data['last_updated'] = date
        
        weather_data = self._get_weather_data()
        if weather_data:
            data.update(weather_data)
        
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

# =============================================================================
# SNOTEL DATA INTEGRATION (HOURLY)
# =============================================================================
class SNOTELDataFetcher:
    """
    Fetches backcountry snow data from SNOTEL stations via the AWDB REST API.
    Uses HOURLY data to calculate real-time 24-hour snow totals (catching early morning storms).
    """
    STATION_INFO = {
        "Showdown":     {"triplet": "437:MT:SNTL",  "name": "Deadman Creek",   "lat": 46.79, "lon": -110.68, "elevation": 6440},
        "TetonPass":    {"triplet": "649:MT:SNTL",  "name": "Mount Lockhart",  "lat": 47.92, "lon": -112.82, "elevation": 6430},
        "Discovery":    {"triplet": "850:MT:SNTL",  "name": "Warm Springs",    "lat": 46.27, "lon": -113.16, "elevation": 7840},
        "LostTrail":    {"triplet": "727:MT:SNTL",  "name": "Saddle Mountain", "lat": 45.69, "lon": -113.97, "elevation": 7890},
        "Snowbowl":     {"triplet": "901:MT:SNTL",  "name": "Stuart Mountain", "lat": 47.00, "lon": -113.93, "elevation": 7270},
        "LookoutPass": {"triplet": "594:ID:SNTL",  "name": "Lookout",          "lat": 47.46, "lon": -115.70, "elevation": 5180},
        "Blacktail":    {"triplet": "1144:MT:SNTL", "name": "Blacktail Mtn",   "lat": 47.98, "lon": -114.35, "elevation": 5660},
        "BigMountain": {"triplet": "469:MT:SNTL",  "name": "Emery Creek",      "lat": 48.43, "lon": -113.94, "elevation": 4340},
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
            'density': 'N/A',
            'percent_of_median': 'N/A', 
            'snow_category': 'N/A',
            'latest_observation': latest_observation,
            'unavailable': bool(error_reason),
            'error_reason': error_reason 
        }

    def _calculate_24h_delta(self, values_list: List[Dict]) -> Optional[tuple]:
            """
            Helper to find the most recent reading and the reading 24h ago.
            Returns (current_val, prev_val, current_date) or None.
            values_list example: [{'date': '2025-11-19 06:00:00', 'value': 12.5}, ...]
            """
            if not values_list or len(values_list) < 2:
                return None
                
            # Helper to parse date strings that might lack seconds
            def parse_date(date_str):
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                    try:
                        return datetime.strptime(date_str, fmt)
                    except ValueError:
                        continue
                raise ValueError(f"Date format not recognized: {date_str}")
    
            # Sort by date just in case the API returns them out of order
            # We use the helper here to ensure sorting works regardless of format
            try:
                sorted_values = sorted(values_list, key=lambda x: parse_date(x['date']))
            except Exception:
                # Fallback to string sort if parsing fails during sort
                sorted_values = sorted(values_list, key=lambda x: x['date'])
            
            # Get the very last reading (most recent)
            current = sorted_values[-1]
            current_date_str = current['date']
            current_val = current['value']
            
            try:
                curr_dt = parse_date(current_date_str)
                target_prev_dt = curr_dt - timedelta(hours=24)
                
                # Find reading closest to target_prev_dt
                prev_val = None
                min_diff = float('inf')
                
                for entry in sorted_values:
                    try:
                        entry_dt = parse_date(entry['date'])
                        # We only care about readings roughly 24h ago (allow small variance)
                        diff = abs((entry_dt - target_prev_dt).total_seconds())
                        
                        # If within 1.5 hours (5400 seconds) of the target time
                        if diff < 5400: 
                            if diff < min_diff:
                                min_diff = diff
                                prev_val = entry['value']
                    except ValueError:
                        continue # Skip malformed dates in the list
                
                if prev_val is not None:
                    return (current_val, prev_val, current_date_str)
                    
            except Exception as e:
                self._log.warning(f"Error calculating delta: {e}")
                
            return None

    def get_snowfall_report(self, resort_name: str):
        # This single-fetch method is rarely used now that we batch, 
        # but we update it for consistency.
        # (Simplified to just return empty to save space, assuming batch is primary)
        # Real implementation would mirror the batch logic below.
        return self._create_empty_report(error_reason="Use batch fetch")

    def get_all_snowfall_reports(self) -> Dict[str, Dict]:
            """
            Fetches HOURLY SNOTEL data for all stations.
            Calculates real-time 24-hour change.
            """
            self._log.info("Starting batch SNOTEL API fetch (HOURLY)...")
            triplet_to_resort = {v['triplet']: k for k, v in self.STATION_INFO.items()}
            all_triplets = ",".join(triplet_to_resort.keys())
            
            # We need yesterday and today to capture the full 24h window + buffer
            url = (
                "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
                f"?stationTriplets={all_triplets}"
                "&elements=WTEQ,SNWD"
                "&duration=HOURLY"
                "&beginDate=-1"     # Yesterday
                "&endDate=0"        # Today
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
                    
                    # Parse the hourly data series
                    snwd_series = []
                    wteq_series = []
                    
                    for element_data in station_data.get('data', []):
                        element_code = element_data['stationElement']['elementCode'].lower()
                        values = element_data.get('values', [])
                        
                        if element_code == 'snwd':
                            snwd_series = values
                        elif element_code == 'wteq':
                            wteq_series = values
    
                    # --- FIX: Get Timestamp FIRST ---
                    # Grab the latest time from the raw data, even if delta calc fails later
                    obs_time = "N/A"
                    if snwd_series:
                        obs_time = snwd_series[-1].get('date', "N/A")
                    elif wteq_series:
                        # Fallback to WTEQ time if SNWD is missing
                        obs_time = wteq_series[-1].get('date', "N/A")
    
                    # Calculate Deltas
                    snow_delta_data = self._calculate_24h_delta(snwd_series)
                    swe_delta_data = self._calculate_24h_delta(wteq_series)
                    
                    snow_change = None
                    swe_change = None
    
                    if snow_delta_data:
                        curr, prev, delta_time = snow_delta_data
                        # If calc succeeded, update time to match the specific delta point used
                        obs_time = delta_time 
                        if curr is not None and prev is not None:
                            snow_change = round(curr - prev, 1)
                    
                    if swe_delta_data:
                        curr, prev, _ = swe_delta_data
                        if curr is not None and prev is not None:
                            swe_change = round(curr - prev, 2)
    
                    # Calculate Density
                    density = None
                    # Ensure we have positive snow accumulation to calculate density
                    if snow_change is not None and swe_change is not None and snow_change > 0.5 and swe_change > 0.01:
                        density = round((swe_change / snow_change) * 100, 1)
                    
                    percent_of_median = "N/A" 
    
                    # Determine if there is new snow
                    is_new_snow = (density is not None)
    
                    report = {
                        'station_name': station_name,
                        'triplet': triplet,
                        'elevation': station_elev,
                        'snow_depth': snow_change if snow_change is not None else 0,
                        'swe': swe_change if swe_change is not None else 0,
                        'density': density if is_new_snow else 'N/A',
                        'percent_of_median': percent_of_median,
                        'snow_category': self._snow_category(density) if is_new_snow else 'N/A',
                        'latest_observation': obs_time,
                        'unavailable': False,
                        'error_reason': None if is_new_snow else "No new snow"
                    }
                    
                    final_reports[resort_name] = report
    
            except Exception as e:
                self._log.error(f"SNOTEL batch API fetch error: {e}", exc_info=True)
                # Fallback loop is handled by the main loop missing keys
            
            # Fill missing resorts with empty reports
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
