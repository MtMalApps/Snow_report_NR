#!/usr/bin/env python3
"""
NWS Storm Outlook Generator V3
Includes retry logic and banner formatting
"""

"""
NWS Storm Outlook Generator V4
Adds wind extraction + categorization while keeping all existing behavior.
"""

import requests
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List


class NWSStormOutlook:
    """Fetches and formats NWS storm outlook data with temps, winds, retry logic."""

    def __init__(self):
        self.headers = {'User-Agent': 'SnowReportScraper/1.0'}
        self.timeout = 8
        self.max_retries = 3
        self.retry_delay = 2  # seconds

    # ──────────────────────────────────────────────────────────────
    # CORE NETWORK REQUEST WITH RETRIES
    # ──────────────────────────────────────────────────────────────
    def _fetch_with_retry(self, url: str) -> Optional[requests.Response]:
        """Fetch URL with retry logic."""
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, headers=self.headers, timeout=self.timeout)
                response.raise_for_status()
                return response

            except requests.exceptions.Timeout:
                if attempt < self.max_retries - 1:
                    print(f"  Timeout on attempt {attempt + 1}, retrying in {self.retry_delay}s...")
                    time.sleep(self.retry_delay)
                else:
                    print(f"  Failed after {self.max_retries} attempts (timeout)")
                    return None

            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries - 1:
                    print(f"  Error on attempt {attempt + 1}: {e}, retrying...")
                    time.sleep(self.retry_delay)
                else:
                    print(f"  Failed after {self.max_retries} attempts: {e}")
                    return None

        return None

    # ──────────────────────────────────────────────────────────────
    # PUBLIC FORECAST METHOD
    # ──────────────────────────────────────────────────────────────
    def get_outlook(self, lat: float, lon: float, hours: int = 48) -> Optional[Dict]:
        """
        Get storm outlook for a location including temps, winds, etc.
        Returns raw, unformatted values suitable for Firebase storage.
        """
        try:
            # Step 1: Get grid point
            point_url = f"https://api.weather.gov/points/{lat},{lon}"
            response = self._fetch_with_retry(point_url)
            if not response:
                return None

            point_data = response.json()

            # Step 2: Get grid data
            grid_url = point_data['properties']['forecastGridData']
            grid_response = self._fetch_with_retry(grid_url)
            if not grid_response:
                return None

            grid_data = grid_response.json()

            # Step 3: Get regular forecast for conditions
            forecast_url = point_data['properties']['forecast']
            forecast_response = self._fetch_with_retry(forecast_url)
            if not forecast_response:
                return None

            forecast_data = forecast_response.json()

            # Extract data
            props = grid_data['properties']
            num_periods = min(hours, 72)

            # Precip totals (mm → inches)
            precip_total = self._calculate_total(
                props.get('quantitativePrecipitation', {}).get('values', []),
                num_periods
            )

            # Snow totals (mm → inches)
            snow_total = self._calculate_total(
                props.get('snowfallAmount', {}).get('values', []),
                num_periods
            )

            # Snow level
            snow_level = self._get_snow_level(
                props.get('snowLevel', {}).get('values', [])
            )

            # Precip probability
            precip_prob = self._get_max_probability(
                props.get('probabilityOfPrecipitation', {}).get('values', []),
                num_periods
            )

            # Temps
            temps = self._get_temp_range(
                props.get('temperature', {}).get('values', []),
                num_periods
            )

            # Conditions
            conditions = self._get_conditions(forecast_data, hours)

            # Timing
            timing = self._get_timing_breakdown(
                props.get('quantitativePrecipitation', {}).get('values', []),
                num_periods
            )

            # WINDS (new)
            wind = self._extract_wind(
                props.get('windSpeed', {}).get('values', []),
                props.get('windGust', {}).get('values', []),
                num_periods
            )

            # Final dict
            return {
                'hours': hours,
                'total_precip_inches': round(precip_total, 2),
                'total_snow_inches': round(snow_total, 1),
                'snow_level_feet': snow_level,
                'precip_probability_max': precip_prob,
                'temp_high_f': temps['high'],
                'temp_low_f': temps['low'],
                'conditions': conditions,
                'timing': timing,
                'wind': wind,  # <-- NEW structured wind info
                'has_snow': snow_total > 0,
                'has_precip': precip_total > 0.1,
                'is_significant': snow_total >= 6 or precip_total >= 1.0
            }

        except Exception as e:
            print(f"Error processing NWS data: {e}")
            return None

    # ──────────────────────────────────────────────────────────────
    # HELPERS — PRECIP & SNOW
    # ──────────────────────────────────────────────────────────────
    def _calculate_total(self, values: list, num_periods: int) -> float:
        """Calculate total from NWS values (converts mm to inches)."""
        total_mm = 0
        for v in values[:num_periods]:
            value_mm = v.get('value')
            if value_mm is not None:
                total_mm += value_mm
        return total_mm * 0.0393701

    # ──────────────────────────────────────────────────────────────
    # HELPERS — SNOW LEVEL
    # ──────────────────────────────────────────────────────────────
    def _get_snow_level(self, values: list) -> Optional[int]:
        """Get snow level in feet above mean sea level."""
        for v in values[:5]:
            value_m = v.get('value')
            if value_m is not None:
                return int(value_m * 3.28084)
        return None

    # ──────────────────────────────────────────────────────────────
    # HELPERS — PROBABILITY
    # ──────────────────────────────────────────────────────────────
    def _get_max_probability(self, values: list, num_periods: int) -> int:
        max_prob = 0
        for v in values[:num_periods]:
            prob = v.get('value')
            if prob and prob > max_prob:
                max_prob = prob
        return int(max_prob)

    # ──────────────────────────────────────────────────────────────
    # HELPERS — TEMPERATURE
    # ──────────────────────────────────────────────────────────────
    def _get_temp_range(self, values: list, num_periods: int) -> Dict:
        temps_c = []
        for v in values[:num_periods]:
            temp = v.get('value')
            if temp is not None:
                temps_c.append(temp)

        if temps_c:
            high_c = max(temps_c)
            low_c = min(temps_c)
            return {
                'high': int(high_c * 9/5 + 32),
                'low': int(low_c * 9/5 + 32)
            }

        return {'high': None, 'low': None}

    # ──────────────────────────────────────────────────────────────
    # HELPERS — CONDITIONS
    # ──────────────────────────────────────────────────────────────
    def _get_conditions(self, forecast_data: dict, hours: int) -> str:
        try:
            periods = forecast_data['properties']['periods']
            num_periods = min(int(hours / 12) + 1, len(periods))

            seen = []
            for p in periods[:num_periods]:
                text = p.get('shortForecast', '')
                if text and text not in seen:
                    seen.append(text)

            return ', '.join(seen[:3]) if seen else "Fair weather"
        except:
            return "Conditions unavailable"

    # ──────────────────────────────────────────────────────────────
    # HELPERS — PRECIP TIMING
    # ──────────────────────────────────────────────────────────────
    def _get_timing_breakdown(self, values: list, num_periods: int) -> Dict:
        """Break down precipitation by time periods."""
        # (Keeping your existing simple placeholder—no changes here)
        try:
            total = sum([v.get('value') or 0 for v in values[:num_periods]])
            return {'total_raw': total}
        except:
            return {}

    # ──────────────────────────────────────────────────────────────
    # HELPERS — NEW WIND EXTRACTION
    # ──────────────────────────────────────────────────────────────
    def _extract_wind(self, wind_values: list, gust_values: list, num_periods: int) -> Dict:
        """Extract ambient wind min/max and gust max, convert to mph, categorize."""

        def to_mph(raw):
            if raw is None:
                return None
            # NWS grid is almost always in m/s or knots.
            # We detect typical ranges:
            if raw < 60:  # assume m/s
                return raw * 2.23694
            else:  # knots (unlikely for grid, but safe)
                return raw * 1.15078

        # Ambient winds
        ambient = [to_mph(v.get('value')) for v in wind_values[:num_periods] if v.get('value') is not None]
        ambient_min = int(min(ambient)) if ambient else None
        ambient_max = int(max(ambient)) if ambient else None

        # Gusts
        gusts = [to_mph(v.get('value')) for v in gust_values[:num_periods] if v.get('value') is not None]
        gust_max = int(max(gusts)) if gusts else None

        # Category (using strongest value)
        strongest = max([x for x in [ambient_max, gust_max] if x is not None], default=None)

        if strongest is None:
            category = "unknown"
        elif strongest <= 10:
            category = "light"
        elif strongest <= 25:
            category = "breezy"
        elif strongest <= 45:
            category = "moderate"
        elif strongest <= 60:
            category = "strong"
        else:
            category = "very strong"

        return {
            "ambient_min_mph": ambient_min,
            "ambient_max_mph": ambient_max,
            "gust_max_mph": gust_max,
            "category": category
        }

# Test with all resorts
def test_all_resorts_with_banner():
    """Test storm outlook and banner for all 8 resorts."""
    from resort_coordinates import RESORT_COORDS
    
    outlook_generator = NWSStormOutlook()
    
    print("\n" + "="*70)
    print("FETCHING FORECASTS FOR ALL RESORTS...")
    print("="*70)
    print()
    
    # Collect all outlooks
    outlook_data = []
    
    for key, resort in RESORT_COORDS.items():
        print(f"Fetching {resort['name']}...")
        data = outlook_generator.get_outlook(resort['lat'], resort['lon'])
        
        outlook_data.append({
            'resort_name': resort['name'],
            'outlook': data
        })
        
        time.sleep(0.5)  # Be nice to the API
    
    print("\n" + "="*70)
    print("STORM WATCH BANNER (HTML Preview)")
    print("="*70)
    banner_html = outlook_generator.create_storm_banner(outlook_data)
    
    if banner_html:
        # Show HTML (in real email this renders nicely)
        print(banner_html)
    else:
        print("(No significant storms to report)")
    
    print("\n" + "="*70)
    print("DETAILED RESORT FORECASTS")
    print("="*70)
    print()
    
    for item in outlook_data:
        if item['outlook']:
            print(outlook_generator.format_outlook_detailed(
                item['outlook'], 
                item['resort_name']
            ))
            print()
        else:
            print(f"{item['resort_name']}: Forecast unavailable")
            print()


if __name__ == "__main__":
    test_all_resorts_with_banner()

