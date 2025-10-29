"""
Montana/Idaho Snow Report Scraper - Cleaned & Standardized Version

This script scrapes snow reports from multiple ski resorts, stores data in CSV files,
and sends consolidated email reports to subscribers.

Features:
- Standardized data schema across all resorts
- CSV backup storage
- Firebase Firestore integration (for future migration)
- Email notifications with HTML formatting
- SNOTEL backcountry data integration
- Powder alert notifications (configurable threshold)

Environment Variables Required:
- GMAIL_ADDRESS: Your Gmail address for sending reports
- GMAIL_APP_PASSWORD: Gmail app-specific password
- FIREBASE_SERVICE_ACCOUNT: Path to Firebase service account JSON (for future)

Author: Original code by user, cleaned and standardized by Claude
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

# Gmail API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# =============================================================================
# CONFIGURATION
# =============================================================================

# Powder Alert Threshold (inches of snow in 24 hours)
POWDER_ALERT_THRESHOLD = 6

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
    
    # Standardized schema - all scrapers must return these fields
    STANDARD_COLUMNS = [
        # Core metadata
        'resort',                    # Resort name
        'date',                      # Date data were accessed (YYYY-MM-DD)
        'timestamp',                 # Time data were accessed (YYYY-MM-DD HH:MM:SS)
        'last_updated',              # Resort's reported last update time
        
        # Snowfall measurements (NEW SNOW)
        'snow_overnight',            # Overnight snowfall (since lifts closed)
        'snow_24h_base',             # 24-hour snowfall at base
        'snow_24h_summit',           # 24-hour snowfall at summit
        'snow_48h_base',             # 48-hour snowfall at base
        'snow_48h_summit',           # 48-hour snowfall at summit
        'snow_7day',                 # 7-day snowfall total (for future)
        
        # Snow depth measurements (TOTAL BASE DEPTH)
        'base_depth',                # Base depth (settled base)
        'summit_depth',              # Summit depth (settled base)
        
        # Temperature
        'temp_base',                 # Base temperature
        'temp_summit',               # Summit temperature
        
        # Wind conditions
        'wind_speed',                # Wind speed measurement
        
        # Conditions
        'conditions_surface',        # Surface conditions (powder, packed, etc.)
        'weather_current',           # Current weather description
        
        # Operations
        'lifts_open',                # Number/fraction of lifts operating
        'runs_open',                 # Number/fraction of runs open
        
        # Additional info
        'comments',                  # Additional notes/forecast/comments
        
        # Future NWS forecast fields (populated later)
        'forecast_base_temp_high',   # Forecasted high temp at base
        'forecast_base_temp_low',    # Forecasted low temp at base
        'forecast_summit_temp_high', # Forecasted high temp at summit
        'forecast_summit_temp_low',  # Forecasted low temp at summit
        'forecast_precipitation',    # Forecasted precipitation
        'forecast_snow',             # Forecasted snow amount
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
                    print(f"Could not parse last_updated: {data['last_updated']} - Error: {e}")

            # Standardize data with all required columns
            standardized_data = {col: None for col in self.STANDARD_COLUMNS}
            standardized_data.update({k: normalize_text(v) if isinstance(v, str) else v
                                    for k, v in data.items() if v is not None})

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
                    for fmt in formats:
                        try:
                            return pd.to_datetime(x, format=fmt)
                        except:
                            continue
                    return pd.NaT

                existing_df['last_updated'] = existing_df['last_updated'].apply(parse_last_updated)

                # Remove entries for the same date
                if last_updated_dt:
                    existing_df = existing_df[
                        (existing_df['date'].dt.date != last_updated_dt.date()) &
                        (existing_df['last_updated'].dt.date != last_updated_dt.date())
                    ]

                # Add new data
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
                df['last_updated'] = df['last_updated'].apply(parse_last_updated)
                
                df = pd.concat([existing_df, df], ignore_index=True, sort=False)

            # Sort and deduplicate
            df = df.sort_values(['date', 'last_updated'], ascending=True).dropna(subset=['date'])
            df = df.drop_duplicates(subset='date', keep='first')

            # Convert datetime back to string for saving
            df['date'] = df['date'].dt.strftime('%Y-%m-%d')
            df['last_updated'] = df['last_updated'].dt.strftime('%Y-%m-%d %I:%M %p')

            # Save to CSV
            df.to_csv(self.csv_filename, index=False, encoding='utf-8-sig')
            print(f"Data saved to {self.csv_filename}")

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
        # Parse polling data from HTML comments
        comment_pattern = r'<!-- BEGIN POLLING --(.*?)-- END POLLING -->'
        polling_data = re.search(comment_pattern, response.text, re.DOTALL)
        data_dict = {}
        
        if polling_data:
            data_string = polling_data.group(1)
            pairs = data_string.split('|~')
            
            for pair in pairs:
                if '|' in pair:
                    key, value = pair.split('|')
                    data_dict[key.strip()] = value.strip()

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
                        data['snow_24h_base'] = cells[2].text.strip()
                        data['temp_base'] = cells[3].text.strip()
                
                if summit_cell and 'Summit' in summit_cell.text:
                    cells = row.find_all('td')
                    if len(cells) >= 5:
                        data['snow_24h_summit'] = cells[2].text.strip()
                        data['temp_summit'] = cells[3].text.strip()
                        data['wind_speed'] = cells[4].text.strip()

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
            print(f"Error parsing HTML data: {e}")

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
                    data['snow_24h_base'] = self._clean_numeric(value)
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
                        data['wind_speed'] = value
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
                        data['snow_24h_summit'] = summit
                        data['snow_24h_base'] = base
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
                                weather_data['wind_speed'] = speed_match.group(1)
            
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
        if date and time:
            try:
                date_str = f"{date} {time}"
                date_str = date_str.replace('.', '').replace('  ', ' ').strip()
                if ':' not in time:
                    date_str = date_str.replace(' am', ':00 am').replace(' pm', ':00 pm')
                parsed_date = datetime.strptime(date_str, '%A, %b %d, %Y %I:%M %p')
                data['last_updated'] = parsed_date.strftime('%Y-%m-%d %I:%M %p')
            except ValueError:
                if date:
                    data['last_updated'] = f"{date} {time}"

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
                    data['snow_24h_summit'] = value
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
                        data['snow_24h_base'] = value
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
                    data['snow_24h_base'] = snow_match.group(1)
            
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
                data['wind_speed'] = text.replace('Wind:', '').strip()

        return data


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

        # Find date string
        all_h3s = soup.find_all('h3')
        for h3 in all_h3s:
            if re.match(r'\d{1,2}/\d{1,2}/\d{4}', h3.text.strip()):
                date_str = h3.text.strip()
                try:
                    parsed_date = datetime.strptime(date_str, '%m/%d/%Y')
                    formatted_date = parsed_date.strftime('%Y-%m-%d')
                    if time_str:
                        clean_time = time_str.replace('Last Update:', '').strip()
                        data['last_updated'] = f"{formatted_date} {clean_time}"
                except Exception as e:
                    print(f"Error parsing date: {e}")

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
                    data['snow_24h_base'] = value.replace('"', '')
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
                        data['snow_24h_base'] = value.replace('"', '').strip()
                    elif label == 'Summit Depth':
                        data['summit_depth'] = value.replace('"', '').strip()
                    elif label == 'Wind':
                        data['wind_speed'] = value.strip()

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
# SNOTEL DATA INTEGRATION
# =============================================================================

class SNOTELDataFetcher:
    """
    Fetches backcountry snow data from SNOTEL stations.
    Provides snow depth, SWE, temperature, and snow quality analysis.
    """
    
    # SNOTEL station mappings for each resort
    STATION_INFO = {
        'LookoutPass': {
            'name': 'Saddle Mountain',
            'id': '47.22,-115.66',
            'triplet': '737:ID:SNTL',
            'elevation': '6240',
            'distance': '2.8'
        },
        'BigMountain': {
            'name': 'Stahl Peak',
            'id': '48.53,-114.27',
            'triplet': '778:MT:SNTL',
            'elevation': '6520',
            'distance': '7.5'
        },
        'LostTrail': {
            'name': 'Copper Creek',
            'id': '45.66,-113.97',
            'triplet': '395:MT:SNTL',
            'elevation': '7320',
            'distance': '14.1'
        }
    }

    def get_station_info(self, resort_name: str) -> Optional[Dict]:
        """Get SNOTEL station information for a resort."""
        return self.STATION_INFO.get(resort_name)

    def _get_snow_category(self, density: float) -> str:
        """Categorize snow quality based on density percentage."""
        if density < 6:
            return "Champagne Powder"
        elif density < 8:
            return "Powder"
        elif density < 10:
            return "Regular"
        elif density < 12:
            return "Average"
        elif density < 15:
            return "Dense"
        else:
            return "Very Dense/Wet"

    def get_snowfall_report(self, resort_name: str, days: int = 1) -> Optional[Dict]:
        """
        Fetch SNOTEL data and calculate snow metrics.
        
        Args:
            resort_name: Name of the resort
            days: Number of days to analyze (1 or 2)
            
        Returns:
            Dictionary with snow depth, SWE, density, quality category, and temperatures
        """
        station_info = self.STATION_INFO.get(resort_name)
        if not station_info:
            return None

        try:
            # Fetch data from SNOTEL API
            url = f"https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
            params = {
                'stationTriplets': station_info['triplet'],
                'elementCd': 'SNWD,WTEQ,TOBS',  # Snow depth, SWE, Temperature
                'ordinal': '1',
                'duration': 'DAILY',
                'getFlags': 'false',
                'alwaysReturnDailyFeb29': 'false',
                'format': 'json',
                'beginDate': (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d'),
                'endDate': datetime.now().strftime('%Y-%m-%d')
            }

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            if not data:
                return None

            # Process measurements
            snow_measurements = []
            swe_measurements = []
            temp_measurements = []

            for item in data:
                try:
                    date_str = item['date']
                    value = item['value']
                    element = item['elementCd']
                    
                    if value is None:
                        continue

                    # Parse date
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    
                    if element == 'SNWD':
                        snow_measurements.append({'datetime': dt, 'value': value})
                    elif element == 'WTEQ':
                        swe_measurements.append({'datetime': dt, 'value': value})
                    elif element == 'TOBS':
                        temp_measurements.append({'datetime': dt, 'value': value})
                        
                except (KeyError, ValueError) as e:
                    continue

            if not snow_measurements or not swe_measurements:
                return None

            # Sort by date
            snow_measurements.sort(key=lambda x: x['datetime'], reverse=True)
            swe_measurements.sort(key=lambda x: x['datetime'], reverse=True)
            temp_measurements.sort(key=lambda x: x['datetime'], reverse=True)
            
            latest_time = snow_measurements[0]['datetime']
            latest_temp = temp_measurements[0]['value'] if temp_measurements else None
            
            # Calculate period boundaries
            reference_time = latest_time
            period_end = reference_time
            period_start = period_end - timedelta(hours=24*days)
            
            # Get measurements within the period
            period_snow = [m for m in snow_measurements
                         if period_start <= m['datetime'] <= period_end]
            period_swe = [m for m in swe_measurements
                         if period_start <= m['datetime'] <= period_end]
            period_temp = [m for m in temp_measurements
                         if period_start <= m['datetime'] <= period_end]
            
            if not period_snow or not period_swe or not period_temp:
                return self._create_empty_report(latest_time, latest_temp)
            
            # Calculate snow depth change
            start_snow = min(period_snow, key=lambda x: x['datetime'])
            end_snow = max(period_snow, key=lambda x: x['datetime'])
            snow_change = round(end_snow['value'] - start_snow['value'], 1)
            
            # Calculate SWE change
            start_swe = min(period_swe, key=lambda x: x['datetime'])
            end_swe = max(period_swe, key=lambda x: x['datetime'])
            swe_change = round(end_swe['value'] - start_swe['value'], 3)
            
            # Calculate temperature range
            temps = [t['value'] for t in period_temp]
            min_temp = round(min(temps), 1) if temps else None
            max_temp = round(max(temps), 1) if temps else None
            
            # Define thresholds for significant changes
            SNOW_THRESHOLD = 0.5  # inches
            SWE_THRESHOLD = 0.01  # inches
            
            period_snow_threshold = SNOW_THRESHOLD * (days if days <= 2 else 2)
            period_swe_threshold = SWE_THRESHOLD * (days if days <= 2 else 2)
            
            # Determine if there's real new snow
            is_new_snow = (snow_change > period_snow_threshold and swe_change > period_swe_threshold)
            
            if not is_new_snow:
                return {
                    'station_name': station_info['name'],
                    'snow_depth': 'melting/settling',
                    'swe': 0,
                    'density': 'N/A',
                    'snow_category': 'N/A',
                    'temp_min': min_temp,
                    'temp_max': max_temp,
                    'latest_observation': latest_time,
                    'latest_temp': latest_temp
                }
            
            # Calculate density and category
            density = round((swe_change / snow_change) * 100, 1)
            snow_category = self._get_snow_category(density)
            
            return {
                'station_name': station_info['name'],
                'snow_depth': snow_change,
                'swe': swe_change,
                'density': density,
                'snow_category': snow_category,
                'temp_min': min_temp,
                'temp_max': max_temp,
                'latest_observation': latest_time,
                'latest_temp': latest_temp
            }
            
        except Exception as e:
            print(f"Error fetching SNOTEL data for {resort_name}: {e}")
            return None

    def _create_empty_report(self, latest_observation=None, latest_temp=None):
        """Create an empty report when data is missing or invalid."""
        return {
            'snow_depth': 'melting/settling',
            'swe': 0,
            'density': 'N/A',
            'snow_category': 'N/A',
            'temp_min': None,
            'temp_max': None,
            'latest_observation': latest_observation,
            'latest_temp': latest_temp
        }


# =============================================================================
# EMAIL REPORTING SYSTEM
# =============================================================================

class EmailSnowReportMessenger:
    """
    Manages email distribution of snow reports.
    Sends HTML-formatted consolidated reports with charts and SNOTEL data.
    """
    
    def __init__(self, gmail_address: str, credentials_path: str = "credentials.json", 
                 token_path: str = "token.json"):
        self.gmail_address = gmail_address
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.subscribers_file = os.path.join(DATA_DIR, "snow_report_subscribers.csv")
        
        # Color sequence for resort sections
        self.color_sequence = [
            "#fff8e1",  # Warm Yellow
            "#e0f7fa",  # Cool Blue
            "#fff1f2",  # Soft Pink
            "#ecfdf5",  # Light Green
            "#f5f3ff",  # Pale Purple
            "#fff7ed",  # Light Orange
            "#f0f9ff"   # Sky Blue
        ]

    def _check_powder_alert(self, resort_data_list: List[Dict]) -> bool:
        """
        Check if any resort has snowfall exceeding the powder alert threshold.
        
        Returns:
            True if powder alert should be triggered, False otherwise
        """
        for resort_data in resort_data_list:
            snow_24h = self.get_24hr_snow(resort_data)
            if snow_24h >= POWDER_ALERT_THRESHOLD:
                return True
        return False

    def get_24hr_snow(self, resort_data: Dict) -> float:
        """Extract 24-hour snow amount from resort data."""
        try:
            resort = resort_data['resort']
            
            # Try summit first, fall back to base
            snow = resort_data.get('snow_24h_summit') or resort_data.get('snow_24h_base', '0')
            
            # Clean and convert
            snow = ''.join(c for c in str(snow) if c.isdigit() or c == '.')
            return float(snow or 0)
        except (ValueError, TypeError) as e:
            print(f"Error processing snow for {resort}: {str(e)}")
            return 0

    def _prepare_chart_data(self, resort_data_list: List[Dict]) -> List[Dict]:
        """Prepare 5-day snow accumulation chart data from Firestore."""
        chart_data = []
        today = datetime.now().date()
        
        # Try to use Firestore for historical data
        try:
            import firebase_uploader
            if firebase_uploader.db is not None:
                use_firestore = True
            else:
                firebase_uploader.initialize_firebase()
                use_firestore = firebase_uploader.db is not None
        except Exception as e:
            print(f"Firebase not available for charts: {e}")
            use_firestore = False
        
        for resort_data in resort_data_list:
            resort_name = resort_data["resort"]
            
            if use_firestore:
                # Get historical data from Firestore
                try:
                    import firebase_uploader
                    chart_info = firebase_uploader.get_chart_data_from_firestore(resort_name, days=5)
                    chart_data.append(chart_info)
                    continue
                except Exception as e:
                    print(f"Error getting Firestore chart data for {resort_name}: {e}")
            
            # Fallback: use current data only
            snow_24h = resort_data.get("snow_24h_summit") or resort_data.get("snow_24h_base") or "0"
            if isinstance(snow_24h, str):
                snow_24h = snow_24h.replace('"', "").strip()
            try:
                snow_value = float(snow_24h)
            except:
                snow_value = 0
            
            chart_data.append({
                "resort": resort_name,
                "values": [snow_value, 0, 0, 0, 0],
                "date_labels": [(today - timedelta(days=i)) for i in range(5)],
                "total": snow_value,
                "has_current_data": True,
                "most_recent_update": today
            })
        
        return chart_data
    def _generate_snow_chart_html(self, chart_data: List[Dict]) -> str:
        """Generate HTML for the 5-day snow accumulation chart."""
        resort_name_map = {
            "LookoutPass": "Lookout Pass",
            "BigMountain": "Big Mountain",
            "LostTrail": "Lost Trail",
            "TetonPass": "Teton Pass",
            "Blacktail": "Blacktail Mountain"
        }

        colors = [
            '#CBD5E1',  # Slate gray (oldest)
            '#0EA5E9',  # Sky blue
            '#7C3AED',  # Purple
            '#0D9488',  # Teal
            '#312E81'   # Dark navy (today)
        ]

        max_total = max(max(resort['total'] for resort in chart_data), 10)
        max_total = ceil(max_total / 5) * 5

        html = ["""
            <div style="font-family: Arial, sans-serif; max-width: 800px; margin: 20px 0;">
                <h2 style="font-size: 20px; margin-bottom: 20px;">5-Day Snow Accumulation</h2>
                <table cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: collapse;">
        """]

        for resort_data in chart_data:
            resort_name = resort_name_map.get(resort_data['resort'], resort_data['resort'])
            has_current_data = resort_data.get('has_current_data', True)
            display_name = resort_name if has_current_data else f"{resort_name}*"
            values = [round(float(v)) for v in resort_data['values']]
            total = round(resort_data['total'])

            html.append(f"""
                <tr style="height: 40px;">
                    <td style="width: 180px; padding: 8px; border-bottom: 1px solid #e5e7eb; font-weight: bold; text-align: right; {'' if has_current_data else 'color: #666;'}">
                        {display_name}
                    </td>
                    <td style="width: 500px; padding: 8px; border-bottom: 1px solid #e5e7eb;">
                        <table cellpadding="0" cellspacing="0" style="width: 100%; height: 25px;">
                            <tr>
            """)

            for value, color in zip(reversed(values), colors):
                if value > 0:
                    width_percent = (value / max_total) * 100
                    html.append(f"""
                        <td style="width: {width_percent}%; background-color: {color}; text-align: center; color: {'white' if color != colors[0] else '#1f2937'}; font-size: 12px; border-right: 1px solid white;">
                            {value}"
                        </td>
                    """)

            remaining_percent = 100 - sum((v / max_total) * 100 for v in reversed(values) if v > 0)
            if remaining_percent > 0:
                html.append(f"""
                    <td style="width: {remaining_percent}%; background-color: #f9fafb;"></td>
                """)

            html.append(f"""
                            </tr>
                        </table>
                    </td>
                    <td style="width: 100px; padding: 8px; border-bottom: 1px solid #e5e7eb; text-align: left; color: {'' if has_current_data else '#666;'}">
                        Total: {total}"
                    </td>
                </tr>
            """)

        # Add legend
        html.append("""
                <tr>
                    <td colspan="3" style="padding: 15px 8px;">
                        <div style="display: flex; justify-content: center; gap: 20px; font-size: 12px;">
        """)

        days = []
        for date in reversed(chart_data[0]['date_labels']):
            if date == datetime.now().date():
                days.append('Today')
            else:
                days.append(date.strftime('%A'))

        for day, color in zip(days, colors):
            html.append(f"""
                <div style="display: flex; align-items: center; gap: 5px;">
                    <div style="width: 15px; height: 15px; background-color: {color};"></div>
                    <span>{day}</span>
                </div>
            """)

        if any(not resort.get('has_current_data', True) for resort in chart_data):
            html.append("""
                </div>
                </td>
                </tr>
                <tr>
                    <td colspan="3" style="padding: 8px; font-size: 12px; color: #666;">
                        * Indicates historical data (current data unavailable)
                    </td>
            """)

        html.append("""
                        </div>
                    </td>
                </tr>
            </table>
        </div>
        """)

        return ''.join(html)

    def _format_table_row(self, label: str, value: str, is_snow_total: bool = False, bg_color: str = None) -> str:
        """Format a table row with styling."""
        if value and value.strip().lower() not in ['', 'none']:
            label = label.replace('\n', '<br>')
            
            highlight_style = ''
            
            # Highlight old dates
            if label in ['Last Updated', 'Last Update']:
                today = datetime.now().date()
                try:
                    if isinstance(value, str):
                        matches = re.findall(r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}-\d{2}-\d{2}', value)
                        if matches:
                            found_date = matches[0]
                            for fmt in ['%m/%d/%y', '%m/%d/%Y', '%Y-%m-%d']:
                                try:
                                    date_parts = re.findall(r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}-\d{2}-\d{2}', found_date)
                                    if date_parts:
                                        parsed_date = datetime.strptime(date_parts[0], fmt).date()
                                        if parsed_date != today:
                                            highlight_style = 'color: red; font-weight: bold;'
                                        break
                                except ValueError:
                                    continue
                except Exception as e:
                    print(f"Error parsing date from '{value}': {e}")

            # Highlight big snow totals
            if is_snow_total and not highlight_style:
                try:
                    snow_amount = float(''.join(c for c in str(value) if c.isdigit() or c == '.') or '0')
                    if snow_amount >= POWDER_ALERT_THRESHOLD:
                        highlight_style = 'font-weight: bold;'
                except ValueError:
                    pass

            base_style = f'background-color: {bg_color};' if bg_color else ''

            return f"""
                <tr style='border: 1px solid #ddd;'>
                    <td style='padding: 8px; border: 1px solid #ddd; width: 200px; font-weight: bold; {base_style}'>{label}</td>
                    <td style='padding: 8px; border: 1px solid #ddd; width: 150px; {highlight_style} {base_style}'>{value}</td>
                </tr>
            """
        return ""

    def _create_snow_summary(self, resort_data_list: List[Dict]) -> str:
        """Create the summary table showing 24hr snow totals."""
        today = datetime.now().date()

        def get_sort_key(resort_data):
            last_updated_str = resort_data.get('last_updated', '')
            last_updated_date = None
            try:
                if last_updated_str:
                    last_updated_date = datetime.strptime(last_updated_str, '%Y-%m-%d %I:%M %p').date()
            except ValueError:
                try:
                    last_updated_date = datetime.strptime(last_updated_str, '%Y-%m-%d %H:%M %p').date()
                except ValueError:
                    last_updated_date = None
                
            snow_amount = float(str(self.get_24hr_snow(resort_data)).replace('"', '') or 0)
            is_current = last_updated_date == today if last_updated_date else False
            
            return (
                0 if is_current else 1,
                -snow_amount,
                0 if is_current else -(last_updated_date.toordinal() if last_updated_date else 0),
                resort_data['resort']
            )

        sorted_resorts = sorted(resort_data_list, key=get_sort_key, reverse=False)

        summary_parts = ["""
            <h2 style='font-size: 20px; font-weight: bold; margin: 16px 0;'>Summary</h2>
            <table style='border-collapse: collapse; margin-bottom: 32px; width: 100%; max-width: 500px;'>
                <tr style='border-bottom: 2px solid #e5e7eb;'>
                    <td colspan='2' style='font-weight: 600; padding: 8px;'>24hr Snow Totals:</td>
                </tr>
        """]

        for resort_data in sorted_resorts:
            resort_name = resort_data['resort']
            snow_amount = self.get_24hr_snow(resort_data)
            
            display_name = {
                "LookoutPass": "Lookout Pass",
                "BigMountain": "Big Mountain",
                "LostTrail": "Lost Trail",
                "TetonPass": "Teton Pass",
                "Blacktail": "Blacktail Mountain"
            }.get(resort_name, resort_name)

            last_updated_str = resort_data.get('last_updated', '')
            last_updated_date = None
            old_data = False
            date_span = ""

            if last_updated_str:
                try:
                    last_updated_date = datetime.strptime(last_updated_str, '%Y-%m-%d %I:%M %p').date()
                    if last_updated_date < today:
                        old_data = True
                        formatted_date = last_updated_date.strftime('%Y-%m-%d')
                        date_span = f"<span style='color: red; font-size: 12px;'>(Last updated {formatted_date})</span>"
                except ValueError:
                    pass

            text_color = 'red' if old_data else 'black'
            snow_display = int(round(snow_amount))
            
            summary_parts.append(f"""
                <tr style='border: 1px solid #e5e7eb;'>
                    <td style='padding: 8px; border-right: 1px solid #e5e7eb; color: {text_color};'>{display_name} {date_span}</td>
                    <td style='padding: 8px; width: 50px; text-align: right; color: {text_color};'>{snow_display}"</td>
                </tr>
            """)

        summary_parts.append("</table>")
        return ''.join(summary_parts)

    def _format_snotel_header(self, snotel, station_info, resort_name):
        """Format the SNOTEL station header with distance and elevation info."""
        if not station_info:
            return f"{snotel['station_name']} SNOTEL Report"
            
        display_name = {
            "LookoutPass": "Lookout Pass",
            "BigMountain": "Big Mountain",
            "LostTrail": "Lost Trail"
        }.get(resort_name, resort_name)
            
        distance = station_info['distance']
        return f"{station_info['name']} SNOTEL Report (for {display_name}, {distance}mi away - {station_info['elevation']}ft)"

    def _add_snotel_section(self, message_parts: List[str], snotel_data: Dict, bg_color: str):
        """Add SNOTEL backcountry data section with snow quality analysis."""
        fields = {
            'snow_depth': ('24hr Snow Depth Change', 
                          f"{snotel_data.get('snow_depth', '')}\"" if snotel_data.get('snow_depth') != 'melting/settling' else 'melting/settling', 
                          True),
            'swe': ('24hr SWE Change', 
                   f"{snotel_data.get('swe', '')}\"" if snotel_data.get('swe') else '0"', 
                   False),
            'density': ('Snow Density', 
                       f"{snotel_data.get('density', '')}%" if snotel_data.get('density') != 'N/A' else 'N/A', 
                       False),
            'snow_category': ('Snow Quality', 
                            snotel_data.get('snow_category', ''), 
                            False),
            'temp_min': ('Min Temp (24hr)', 
                        f"{snotel_data.get('temp_min', '')}°F" if snotel_data.get('temp_min') else '', 
                        False),
            'temp_max': ('Max Temp (24hr)', 
                        f"{snotel_data.get('temp_max', '')}°F" if snotel_data.get('temp_max') else '', 
                        False),
            'latest_temp': ('Current Temp', 
                          f"{snotel_data.get('latest_temp', '')}°F" if snotel_data.get('latest_temp') else '', 
                          False)
        }
        
        for field, (label, value, is_snow_total) in fields.items():
            if value:
                message_parts.append(self._format_table_row(label, value, is_snow_total, bg_color))

    def _add_resort_section(self, message_parts: List[str], data: Dict, bg_color: str):
        """Add a resort-specific section to the email. (Simplified - maps common fields)"""
        
        # Common fields that most resorts have
        common_fields = {
            'last_updated': ('Last Updated', data.get('last_updated', ''), False),
            'snow_24h_base': ('24hr Snow - Base', f"{data.get('snow_24h_base', '')}\"" if data.get('snow_24h_base') else '', True),
            'snow_24h_summit': ('24hr Snow - Summit', f"{data.get('snow_24h_summit', '')}\"" if data.get('snow_24h_summit') else '', True),
            'snow_overnight': ('Overnight Snow', f"{data.get('snow_overnight', '')}\"" if data.get('snow_overnight') else '', True),
            'snow_48h_base': ('48hr Snow - Base', f"{data.get('snow_48h_base', '')}\"" if data.get('snow_48h_base') else '', False),
            'snow_48h_summit': ('48hr Snow - Summit', f"{data.get('snow_48h_summit', '')}\"" if data.get('snow_48h_summit') else '', False),
            'base_depth': ('Base Depth', f"{data.get('base_depth', '')}\"" if data.get('base_depth') else '', False),
            'summit_depth': ('Summit Depth', f"{data.get('summit_depth', '')}\"" if data.get('summit_depth') else '', False),
            'temp_base': ('Base Temp', f"{data.get('temp_base', '')}°F" if data.get('temp_base') else '', False),
            'temp_summit': ('Summit Temp', f"{data.get('temp_summit', '')}°F" if data.get('temp_summit') else '', False),
            'wind_speed': ('Wind Speed', f"{data.get('wind_speed', '')} mph" if data.get('wind_speed') else '', False),
            'conditions_surface': ('Surface Conditions', data.get('conditions_surface', ''), False),
            'weather_current': ('Current Weather', data.get('weather_current', ''), False),
            'lifts_open': ('Lifts Open', data.get('lifts_open', ''), False),
            'runs_open': ('Runs Open', data.get('runs_open', ''), False),
            'comments': ('Comments', data.get('comments', ''), False)
        }
        
        for field, (label, value, is_snow_total) in common_fields.items():
            if value and value.strip():
                message_parts.append(self._format_table_row(label, value, is_snow_total, bg_color))

    def _build_email_html(self, sorted_resorts: List[Dict], snotel: SNOTELDataFetcher, is_powder_alert: bool = False) -> str:
        """Build the complete HTML email content."""
        message_parts = ["""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    body { font-family: Arial, sans-serif; }
                    .resort-section { 
                        margin: 20px 0; 
                        padding: 15px; 
                        border-radius: 5px; 
                    }
                    table { 
                        border-collapse: collapse; 
                        width: 100%; 
                        max-width: 600px; 
                    }
                    .timestamp { 
                        color: #666; 
                        font-size: 12px; 
                    }
                </style>
            </head>
            <body>
            <div style="max-width: 800px; margin: 0 auto;">
        """]

        # Add title with powder alert indicator if applicable
        if is_powder_alert:
            message_parts.append("<h1>⚠️ POWDER ALERT - Daily Snow Report</h1>")
        else:
            message_parts.append("<h1>Daily Snow Report</h1>")
            
        message_parts.append(f'<p class="timestamp">Report generated on: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')
        
        # Add snow summary
        message_parts.append(self._create_snow_summary(sorted_resorts))

        # Add chart
        chart_data = self._prepare_chart_data(sorted_resorts)
        chart_html = self._generate_snow_chart_html(chart_data)
        message_parts.append(chart_html)

        # Add individual resort sections
        message_parts.append("<h2>Detailed Resort Reports</h2>")
        
        for idx, resort_data in enumerate(sorted_resorts):
            resort_name = resort_data['resort']
            bg_color = self.color_sequence[idx % len(self.color_sequence)]
            
            message_parts.append(f"""
                <div class="resort-section" style="background-color: {bg_color};">
                <h3 style="margin-top: 0;">{resort_name}</h3>
                <table>
            """)

            # Add resort data
            self._add_resort_section(message_parts, resort_data, bg_color)
            message_parts.append("</table>")

            # Add SNOTEL data if available
            snotel_data = snotel.get_snowfall_report(resort_name)
            if snotel_data:
                station_info = snotel.get_station_info(resort_name)
                snotel_header = self._format_snotel_header(snotel_data, station_info, resort_name)
                message_parts.append(f"<h4 style='margin-bottom: 10px;'>{snotel_header}</h4>")
                message_parts.append("<table>")
                self._add_snotel_section(message_parts, snotel_data, bg_color)
                message_parts.append("</table>")

            message_parts.append("</div>")

        message_parts.append("""
            </div>
            </body>
            </html>
        """)

        return "\n".join(message_parts)

    def send_consolidated_report(self, resort_data_list: List[Dict]):
        """Send consolidated snow report email to all subscribers."""
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

        # Sort resorts by snow amount
        sorted_resorts = sorted(
            resort_data_list,
            key=lambda x: self.get_24hr_snow(x),
            reverse=True
        )

        # Check for powder alert
        is_powder_alert = self._check_powder_alert(sorted_resorts)

        # Initialize SNOTEL fetcher
        snotel = SNOTELDataFetcher()

        # Build email
        email_html = self._build_email_html(sorted_resorts, snotel, is_powder_alert)

        # Create subject line
        if is_powder_alert:
            max_snow = max(self.get_24hr_snow(r) for r in sorted_resorts)
            best_resort = max(sorted_resorts, key=lambda x: self.get_24hr_snow(x))['resort']
            subject = f"⚠️ POWDER ALERT: {int(max_snow)}\" at {best_resort}! - Daily Snow Report {datetime.now().strftime('%Y-%m-%d')}"
        else:
            subject = f"Daily Snow Report - {datetime.now().strftime('%Y-%m-%d')}"

        # Send email using Gmail API
        try:
            # Load or refresh OAuth2 credentials
            creds = None
            if os.path.exists(self.token_path):
                creds = Credentials.from_authorized_user_file(self.token_path)
            
            # If credentials don't exist or are invalid, we need to refresh
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    # Save the refreshed credentials
                    with open(self.token_path, 'w') as token:
                        token.write(creds.to_json())
                else:
                    raise ValueError("OAuth credentials are invalid and cannot be refreshed. Please re-authenticate.")
            
            # Build Gmail API service
            service = build('gmail', 'v1', credentials=creds)
            
            # Create message
            message = MIMEMultipart('alternative')
            message['Subject'] = subject
            message['From'] = self.gmail_address
            message['To'] = ', '.join(recipients)
            
            html_part = MIMEText(email_html, 'html')
            message.attach(html_part)
            
            # Encode message
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
            
            # Send via Gmail API
            send_message = service.users().messages().send(
                userId='me',
                body={'raw': raw_message}
            ).execute()
            
            print(f"Report sent successfully to {len(recipients)} recipients")
            if is_powder_alert:
                print(f"🎉 POWDER ALERT triggered!")

        except HttpError as error:
            print(f"Gmail API error: {error}")
            import traceback
            traceback.print_exc()
        except Exception as e:
            print(f"Error sending email: {e}")
            import traceback
            traceback.print_exc()


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """Main execution function."""
    log_file = os.path.join(LOG_DIR, "snow_report.log")
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
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
                resort_data_list.append(snow_data)
                scraper.save_to_csv(snow_data)
                logging.info(f"Data saved for {scraper.resort_name}")
            else:
                logging.warning(f"No data retrieved for {scraper.resort_name}")

        # Send consolidated report
        if resort_data_list:
            logging.info("Sending consolidated report...")
            try:
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
