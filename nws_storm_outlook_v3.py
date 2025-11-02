#!/usr/bin/env python3
"""
NWS Storm Outlook Generator V3
Includes retry logic and banner formatting
"""

import requests
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

class NWSStormOutlook:
    """Fetches and formats NWS storm outlook data with temps and retry logic."""
    
    def __init__(self):
        self.headers = {'User-Agent': 'SnowReportScraper/1.0'}
        self.timeout = 8  # Increased from 10
        self.max_retries = 3
        self.retry_delay = 2  # seconds
    
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
    
    def get_outlook(self, lat: float, lon: float, hours: int = 48) -> Optional[Dict]:
        """
        Get storm outlook for a location including temps.
        Uses retry logic for reliability.
        
        Args:
            lat: Latitude
            lon: Longitude  
            hours: Forecast period in hours (default 48)
            
        Returns:
            Dictionary with outlook data or None if error
        """
        try:
            # Step 1: Get grid point
            point_url = f"https://api.weather.gov/points/{lat},{lon}"
            response = self._fetch_with_retry(point_url)
            if not response:
                return None
            
            point_data = response.json()
            
            # Step 2: Get grid data (quantitative forecasts)
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
            
            # Get all metrics
            precip_total = self._calculate_total(
                props.get('quantitativePrecipitation', {}).get('values', []),
                num_periods
            )
            
            snow_total = self._calculate_total(
                props.get('snowfallAmount', {}).get('values', []),
                num_periods
            )
            
            snow_level = self._get_snow_level(
                props.get('snowLevel', {}).get('values', [])
            )
            
            precip_prob = self._get_max_probability(
                props.get('probabilityOfPrecipitation', {}).get('values', []),
                num_periods
            )
            
            temps = self._get_temp_range(
                props.get('temperature', {}).get('values', []),
                num_periods
            )
            
            conditions = self._get_conditions(forecast_data, hours)
            
            timing = self._get_timing_breakdown(
                props.get('quantitativePrecipitation', {}).get('values', []),
                num_periods
            )
            
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
                'has_snow': snow_total > 0,
                'has_precip': precip_total > 0.1,
                'is_significant': snow_total >= 6 or precip_total >= 1.0
            }
            
        except Exception as e:
            print(f"Error processing NWS data: {e}")
            return None
    
    def _calculate_total(self, values: list, num_periods: int) -> float:
        """Calculate total from NWS values (converts mm to inches)."""
        total_mm = 0
        for i, v in enumerate(values[:num_periods]):
            value_mm = v.get('value')
            if value_mm:
                total_mm += value_mm
        return total_mm * 0.0393701
    
    def _get_snow_level(self, values: list) -> Optional[int]:
        """Get snow level in feet."""
        if not values:
            return None
        for v in values[:5]:
            value_m = v.get('value')
            if value_m:
                return int(value_m * 3.28084)
        return None
    
    def _get_max_probability(self, values: list, num_periods: int) -> int:
        """Get maximum precipitation probability."""
        max_prob = 0
        for v in values[:num_periods]:
            prob = v.get('value')
            if prob and prob > max_prob:
                max_prob = prob
        return int(max_prob)
    
    def _get_temp_range(self, values: list, num_periods: int) -> Dict:
        """Get temperature high and low."""
        temps_c = []
        for v in values[:num_periods]:
            temp = v.get('value')
            if temp:
                temps_c.append(temp)
        
        if temps_c:
            high_c = max(temps_c)
            low_c = min(temps_c)
            high_f = int(high_c * 9/5 + 32)
            low_f = int(low_c * 9/5 + 32)
            return {'high': high_f, 'low': low_f}
        
        return {'high': None, 'low': None}
    
    def _get_conditions(self, forecast_data: dict, hours: int) -> str:
        """Get general conditions description."""
        try:
            periods = forecast_data['properties']['periods']
            num_periods = min(int(hours / 12) + 1, len(periods))
            
            conditions = []
            for p in periods[:num_periods]:
                forecast = p.get('shortForecast', '')
                if forecast and forecast not in conditions:
                    conditions.append(forecast)
            
            return ', '.join(conditions[:3]) if conditions else "Fair weather"
        except:
            return "Conditions unavailable"
    
    def _get_timing_breakdown(self, values: list, num_periods: int) -> Dict:
        """Break down precipitation by time periods."""
        periods = {
            'next_12h': 0,
            'hours_12_24': 0,
            'hours_24_36': 0,
            'hours_36_48': 0
        }
        
        for i, v in enumerate(values[:num_periods]):
            value_mm = v.get('value', 0)
            if value_mm:
                value_in = value_mm * 0.0393701
                if i < 12:
                    periods['next_12h'] += value_in
                elif i < 24:
                    periods['hours_12_24'] += value_in
                elif i < 36:
                    periods['hours_24_36'] += value_in
                else:
                    periods['hours_36_48'] += value_in
        
        for key in periods:
            periods[key] = round(periods[key], 2)
        
        return periods
    
    def format_outlook_detailed(self, outlook: Dict, resort_name: str = "") -> str:
        """Format outlook for detailed resort report."""
        if not outlook:
            return "Storm outlook unavailable"
        
        lines = []
        if resort_name:
            lines.append(f"{resort_name}:")
        
        if outlook['has_precip']:
            lines.append(f"  Precipitation: {outlook['total_precip_inches']}\" liquid ({outlook['precip_probability_max']}% chance)")
        else:
            lines.append(f"  Precipitation: None expected")
        
        if outlook['has_snow']:
            snow_msg = f"  Snow: {outlook['total_snow_inches']}\" expected"
            if outlook['total_snow_inches'] >= 6:
                snow_msg += " ❄️"
            lines.append(snow_msg)
        else:
            if outlook['has_precip']:
                lines.append(f"  Snow: None expected (too warm)")
            else:
                lines.append(f"  Snow: None expected")
        
        if outlook['temp_high_f'] and outlook['temp_low_f']:
            lines.append(f"  Temps: High {outlook['temp_high_f']}°F, Low {outlook['temp_low_f']}°F")
        
        if outlook['snow_level_feet']:
            lines.append(f"  Snow Level: {outlook['snow_level_feet']:,} ft")
        
        if outlook['conditions']:
            lines.append(f"  Conditions: {outlook['conditions']}")
        
        return "\n".join(lines)
    
    def create_storm_banner(self, outlook_data: List[Dict]) -> str:
        """
        Create storm watch banner for email (goes between table and charts).
        
        Args:
            outlook_data: List of dicts with 'resort_name' and 'outlook' keys
            
        Returns:
            HTML formatted banner string
        """
        # Filter for significant storms
        significant_storms = []
        snow_forecasts = []
        
        for item in outlook_data:
            outlook = item.get('outlook')
            resort = item.get('resort_name')
            
            if not outlook:
                continue
            
            # Collect snow forecasts
            if outlook['has_snow'] and outlook['total_snow_inches'] > 0:
                snow_forecasts.append({
                    'resort': resort,
                    'snow': outlook['total_snow_inches'],
                    'precip': outlook['total_precip_inches']
                })
            
            # Collect significant precip
            elif outlook['total_precip_inches'] >= 1.0:
                significant_storms.append({
                    'resort': resort,
                    'precip': outlook['total_precip_inches'],
                    'prob': outlook['precip_probability_max']
                })
        
        # Build banner if there's anything to report
        if not snow_forecasts and not significant_storms:
            return ""  # No banner needed
        
        html_parts = []
        html_parts.append('<div style="background: #fff3cd; border: 2px solid #ffc107; border-radius: 8px; padding: 15px; margin: 20px 0;">')
        html_parts.append('<h3 style="margin: 0 0 10px 0; color: #856404;">⚠️ STORM WATCH</h3>')
        html_parts.append('<p style="margin: 5px 0; color: #856404;">Heavy precipitation forecast - see detailed reports below</p>')
        
        # Snow forecasts
        if snow_forecasts:
            html_parts.append('<div style="margin-top: 10px;">')
            html_parts.append('<strong style="color: #004085;">❄️ Snow Forecast (48hr):</strong>')
            html_parts.append('<ul style="margin: 5px 0; padding-left: 20px;">')
            
            # Sort by snow amount
            snow_forecasts.sort(key=lambda x: x['snow'], reverse=True)
            
            for item in snow_forecasts:
                html_parts.append(
                    f'<li style="color: #004085;">{item["resort"]}: '
                    f'{item["snow"]}" snow</li>'
                )
            
            html_parts.append('</ul>')
            html_parts.append('</div>')
        
        # Heavy precip (no snow)
        if significant_storms:
            html_parts.append('<div style="margin-top: 10px;">')
            html_parts.append('<strong style="color: #856404;">🌧️ Heavy Rain Forecast (48hr):</strong>')
            html_parts.append('<ul style="margin: 5px 0; padding-left: 20px;">')
            
            # Sort by precip amount
            significant_storms.sort(key=lambda x: x['precip'], reverse=True)
            
            for item in significant_storms:
                html_parts.append(
                    f'<li style="color: #856404;">{item["resort"]}: '
                    f'{item["precip"]}\" ({item["prob"]}% chance)</li>'
                )
            
            html_parts.append('</ul>')
            html_parts.append('</div>')
        
        html_parts.append('</div>')
        
        return ''.join(html_parts)


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

