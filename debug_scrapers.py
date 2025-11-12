"""
Debug version of BigMountain, Showdown, and Snowbowl scrapers
to diagnose last_updated parsing issues.

Run this locally to see what's being extracted.
"""
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
}

def debug_bigmountain():
    """Debug BigMountain scraper."""
    print("\n" + "="*80)
    print("DEBUGGING BIG MOUNTAIN")
    print("="*80)
    
    url = "https://skiwhitefish.com/snowreport/"
    response = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Current logic from scraper
    print("\n1. Current scraper logic - looking in <p><strong>:")
    date = None
    time = None
    date_pattern = r'\w+,\s+\w+\.?\s+\d{1,2},\s+\d{4}'
    time_pattern = r'\d+(?::\d+)?\s*[ap]\.m\.'
    
    for p_tag in soup.find_all('p'):
        strong_tag = p_tag.find('strong')
        if strong_tag:
            text = strong_tag.text
            print(f"   Checking <p><strong>: '{text}'")
            if re.search(date_pattern, text):
                date = text.rstrip(':')
                print(f"   ✓ FOUND DATE: {date}")
            elif re.search(time_pattern, text, re.IGNORECASE):
                time_match = re.search(r'(\d+(?::\d+)?\s*[ap]\.m\.)', text, re.IGNORECASE)
                time = time_match.group(1)
                print(f"   ✓ FOUND TIME: {time}")
    
    print(f"\n   Final result: date={date}, time={time}")
    
    # Try alternate approaches
    print("\n2. Alternate approach - search ALL <p> tags:")
    for p_tag in soup.find_all('p'):
        text = p_tag.get_text(strip=True)
        if re.search(date_pattern, text):
            print(f"   Found date pattern: '{text}'")
        if re.search(time_pattern, text, re.IGNORECASE):
            print(f"   Found time pattern: '{text}'")
    
    print("\n3. Looking for divs with 'update' or 'date' in class:")
    for div in soup.find_all('div'):
        if div.get('class'):
            class_str = ' '.join(div.get('class'))
            if any(word in class_str.lower() for word in ['update', 'date', 'time', 'report']):
                print(f"   Class: {class_str}")
                print(f"   Text: {div.get_text(strip=True)[:150]}")
                print()

def debug_showdown():
    """Debug Showdown scraper."""
    print("\n" + "="*80)
    print("DEBUGGING SHOWDOWN")
    print("="*80)
    
    url = "https://www.showdownmontana.com/snow-report"
    response = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Current logic
    print("\n1. Current scraper logic - looking for 'Last Update' in <p class='sqsrte-large'>:")
    large_paragraphs = soup.find_all('p', class_='sqsrte-large')
    print(f"   Found {len(large_paragraphs)} paragraphs")
    
    time_str = None
    for i, p in enumerate(large_paragraphs):
        text = p.get_text(strip=True)
        print(f"   [{i}] {text[:80]}")
        if 'Last Update' in text:
            time_str = text
            print(f"   ✓ FOUND: {time_str}")
    
    print(f"\n   Final time_str: {time_str}")
    
    # Date in h3
    print("\n2. Looking for dates in <h3>:")
    all_h3s = soup.find_all('h3')
    for h3 in all_h3s:
        text = h3.get_text(strip=True)
        if re.match(r'\d{1,2}/\d{1,2}/\d{4}', text):
            print(f"   ✓ FOUND: {text}")
    
    # Alternative
    print("\n3. Searching for 'update' anywhere:")
    all_text = soup.get_text()
    for line in all_text.split('\n'):
        line = line.strip()
        if 'update' in line.lower() and 10 < len(line) < 100:
            print(f"   {line}")

def debug_snowbowl():
    """Debug Snowbowl scraper."""
    print("\n" + "="*80)
    print("DEBUGGING SNOWBOWL")
    print("="*80)
    
    url = "https://www.montanasnowbowl.com/report.php"
    response = requests.get(url, headers=headers, timeout=10, verify=False)
    html = response.text
    soup = BeautifulSoup(html, 'html.parser')
    
    # Check for HTML comment
    print("\n1. Checking for HTML comment polling data:")
    comment_pattern = r'<!--polling-->(.*?)<!--/polling-->'
    polling_data = re.search(comment_pattern, html, re.DOTALL)
    
    if polling_data:
        print("   ✓ Found polling comment")
        data_string = polling_data.group(1)
        pairs = data_string.split('|~')
        
        print(f"   Found {len(pairs)} data pairs")
        for pair in pairs[:10]:  # Show first 10
            if '|' in pair:
                key, value = pair.split('|', 1)
                print(f"   - {key.strip()}: {value.strip()}")
        
        # Look for lastupdated specifically
        for pair in pairs:
            if 'lastupdated' in pair.lower():
                key, value = pair.split('|', 1)
                print(f"\n   ✓ FOUND lastupdated: {value.strip()}")
    else:
        print("   ❌ No polling comment found")
    
    # Check table
    print("\n2. Checking HTML tables for data:")
    rows = soup.find_all('tr')
    print(f"   Found {len(rows)} table rows")
    
    for row in rows[:5]:  # Show first 5
        cells = row.find_all('td')
        if cells:
            cell_texts = [c.get_text(strip=True) for c in cells]
            print(f"   Row: {' | '.join(cell_texts[:4])}")

# Main execution
if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    
    print("DEBUGGING RESORT SCRAPERS")
    print("This will help diagnose why last_updated is not being captured")
    print("="*80)
    
    try:
        debug_bigmountain()
    except Exception as e:
        print(f"\n❌ BigMountain error: {e}")
    
    try:
        debug_showdown()
    except Exception as e:
        print(f"\n❌ Showdown error: {e}")
    
    try:
        debug_snowbowl()
    except Exception as e:
        print(f"\n❌ Snowbowl error: {e}")
    
    print("\n" + "="*80)
    print("DEBUG COMPLETE")
    print("="*80)
    print("\nNext steps:")
    print("1. Review the output above to see what's actually on the pages")
    print("2. Update the scraper code based on the actual HTML structure")
    print("3. If date/time info is in a different location, modify the selectors")
