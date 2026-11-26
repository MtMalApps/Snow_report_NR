"""
Firebase Uploader for Snow Report Data

This script reads the most recent data from all CSVs in the data directory
and uploads them to Firebase Firestore.

Usage:
    Call this after the main scraper runs to upload data to Firestore.

Environment Variables Required:
    FIREBASE_SERVICE_ACCOUNT: Path to Firebase service account JSON
"""

import os
import json
import sys
import ast
import base64
import pandas as pd
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import List, Dict, Optional
import firebase_admin
from firebase_admin import credentials, firestore
from zoneinfo import ZoneInfo

# Global Firebase client
db = None

# --- We must define the data directory here ---
DATA_DIR = os.path.expanduser("~/snow_reports/data")


def initialize_firebase() -> Optional[firestore.Client]:
    """Initialize Firebase Admin SDK."""
    global db
    
    firebase_cred_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    
    if not firebase_cred_path:
        print("⚠️  FIREBASE_SERVICE_ACCOUNT environment variable not set")
        return None
    
    if not os.path.exists(firebase_cred_path):
        print(f"⚠️  Firebase credentials file not found: {firebase_cred_path}")
        return None
    
    try:
        # Check if already initialized
        try:
            firebase_admin.get_app()
            print("✅ Firebase already initialized")
        except ValueError:
            # Not initialized, initialize now
            cred = credentials.Certificate(firebase_cred_path)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized successfully")
            
        db = firestore.client()
        return db
    except Exception as e:
        print(f"❌ Error initializing Firebase: {e}")
        import traceback
        traceback.print_exc()
        return None


def _parse_object_field(value, field_name: str):
    """
    Safely convert a CSV cell back into a Python object.

    Handles:
      - already-a-dict/list → returned as-is
      - JSON strings        → json.loads
      - Python-literal dict → ast.literal_eval
      - bad/empty strings   → None
    """
    # Already in structured form
    if isinstance(value, (dict, list)):
        return value

    # Nothing to parse
    if value is None:
        return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None

    # Try JSON first
    try:
        return json.loads(text)
    except Exception:
        pass

    # Fallback to literal_eval (handles Python dict strings)
    try:
        return ast.literal_eval(text)
    except Exception:
        print(f"⚠️  Could not parse {field_name} = {repr(value)}, setting to null.")
        return None


def save_resort_data_to_firestore(resort_data: Dict) -> bool:
    """
    Save a single resort's data to Firestore.
    This function now expects data that has been read from a CSV.
    """
    if db is None:
        print("❌ Firebase not initialized")
        return False
    
    try:
        resort_name = resort_data.get('resort')
        date_str = resort_data.get('date')
        timestamp_str = resort_data.get('timestamp')
        
        if not resort_name or not date_str:
            print(f"⚠️  Missing required fields (resort or date)")
            return False
        
        # --- CONVERT STRING DATA BACK TO OBJECTS (nws_forecast, snotel_data) ---
        for key in ['nws_forecast', 'snotel_data']:
            resort_data[key] = _parse_object_field(resort_data.get(key), key)

        # Collection: snow_reports
        collection_ref = db.collection('snow_reports')
        
        # Document ID: resort_YYYY-MM-DD_HH-MM-SS  (keeps your original pattern)
        if not timestamp_str:
            # Fallback to just date if timestamp missing
            doc_id = f"{resort_name}_{date_str}"
        else:
            doc_id = f"{resort_name}_{timestamp_str.replace(' ', '_').replace(':', '-')}"
        
        # Check for existing entries on the same date (prevent duplicates)
        existing_query = (
            collection_ref
            .where('resort', '==', resort_name)
            .where('date', '==', date_str)
            .limit(5)
            .stream()
        )
        
        deleted_count = 0
        for doc in existing_query:
            doc.reference.delete()
            deleted_count += 1
        
        if deleted_count > 0:
            print(f"🗑️  Deleted {deleted_count} existing Firestore entry(ies) for {resort_name} on {date_str}")
        
        # Add new document (includes nested nws_forecast with 'wind', if present)
        collection_ref.document(doc_id).set(resort_data)
        print(f"✅ Saved to Firestore: {resort_name} ({date_str})")
        return True
        
    except Exception as e:
        print(f"❌ Error saving {resort_data.get('resort', 'Unknown')} to Firestore: {e}")
        import traceback
        traceback.print_exc()
        return False


LOCAL_TZ = ZoneInfo("America/Denver")

# Fields to watch for null-regression between scraper runs
MONITORED_FIELDS = [
    'snow_overnight', 'snow_24h_base', 'snow_24h_summit',
    'snow_48h_base', 'snow_48h_summit',
    'base_depth', 'summit_depth',
    'lifts_open', 'runs_open',
]

def _is_numeric(v) -> bool:
    if v is None:
        return False
    try:
        f = float(v)
        return f == f  # exclude NaN
    except (ValueError, TypeError):
        return False


def write_scraper_health(uploaded_data: List[Dict], upload_results: Dict[str, bool]) -> List[str]:
    """
    After uploading, compare each resort's data against yesterday's Firestore doc.
    Flag quarantined records, upload failures, and any numeric fields that
    regressed from a real value to null since the previous run.
    Writes a 'scraper_health/{today}' doc to Firestore and returns a list of alert strings.
    """
    if db is None:
        print("⚠️  Firebase not initialized — skipping health doc")
        return []

    today     = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch yesterday's docs for regression comparison
    prev_data: Dict[str, dict] = {}
    try:
        for doc in db.collection('snow_reports').where('date', '==', yesterday).stream():
            d = doc.to_dict()
            if d.get('resort'):
                prev_data[d['resort']] = d
    except Exception as e:
        print(f"⚠️  Could not fetch yesterday's data for regression check: {e}")

    health: Dict[str, dict] = {}
    alerts: List[str] = []

    for resort_data in uploaded_data:
        resort  = resort_data.get('resort', 'unknown')
        status  = 'ok'
        issues: List[str] = []

        # Quarantine / LLM-fallback markers written by the scraper
        data_source = str(resort_data.get('data_source') or '')
        scrape_warnings = resort_data.get('scrape_warnings') or []
        if isinstance(scrape_warnings, str):
            try:
                scrape_warnings = json.loads(scrape_warnings)
            except Exception:
                scrape_warnings = []

        if data_source == 'quarantined':
            status = 'error'
            issues.append('quarantined_by_scraper')
        elif 'partial' in data_source:
            status = 'partial'
            issues.append(data_source)

        if scrape_warnings:
            if status == 'ok':
                status = 'partial'
            issues.extend(scrape_warnings)

        # Stale last_updated from resort website
        lu = resort_data.get('last_updated') or ''
        if lu and len(lu) >= 10:
            try:
                lu_date = datetime.strptime(lu[:10], '%Y-%m-%d').date()
                age = (datetime.now(LOCAL_TZ).date() - lu_date).days
                if age >= 2:
                    if status == 'ok':
                        status = 'partial'
                    issues.append(f"resort_site_stale({lu[:10]}, {age}d old)")
            except ValueError:
                pass

        # Field regression vs yesterday
        prev = prev_data.get(resort, {})
        prev_base = prev.get('base_depth')
        if _is_numeric(prev_base) and float(prev_base) > 0:
            regressions = []
            for field in MONITORED_FIELDS:
                prev_v = prev.get(field)
                curr_v = resort_data.get(field)
                if _is_numeric(prev_v) and not _is_numeric(curr_v):
                    regressions.append(f"{field}({prev_v}→null)")
            if regressions:
                if status == 'ok':
                    status = 'partial'
                issues.append("regression: " + ", ".join(regressions))

        # Upload failure
        if not upload_results.get(resort, True):
            status = 'error'
            issues.append('upload_failed')

        health[resort] = {'status': status, 'issues': issues, 'data_source': data_source}
        if status != 'ok':
            alerts.append(f"[{status.upper()}] {resort}: {'; '.join(issues)}")

    health_doc = {
        'run_date':      today,
        'run_timestamp': datetime.now(LOCAL_TZ).isoformat(),
        'resorts':       health,
        'alerts':        alerts,
        'has_issues':    len(alerts) > 0,
    }
    try:
        db.collection('scraper_health').document(today).set(health_doc)
        issue_word = "alert" if len(alerts) == 1 else "alerts"
        print(f"✅ Health doc written for {today} — {len(alerts)} {issue_word}")
    except Exception as e:
        print(f"❌ Failed to write health doc: {e}")

    return alerts


def send_alert_email(alerts: List[str], run_date: str) -> None:
    """
    Send a plain-text alert email via Gmail API (same OAuth token as send_daily_report.py).
    Only called when there are alerts. No-ops silently if credentials aren't present.
    """
    gmail_address = os.environ.get('GMAIL_ADDRESS')
    token_path    = os.environ.get('GMAIL_TOKEN_PATH', 'token.json')

    if not gmail_address:
        print("⚠️  GMAIL_ADDRESS not set — skipping alert email")
        return
    if not os.path.exists(token_path):
        print(f"⚠️  Gmail token not found at {token_path} — skipping alert email")
        return

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(token_path)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("⚠️  Gmail token invalid/expired — cannot send alert email")
                return

        service = build('gmail', 'v1', credentials=creds)

        body_lines = [
            f"Snow Report Scraper Alert — {run_date}",
            "",
            f"{len(alerts)} issue(s) detected in today's scraper run:",
            "",
        ] + [f"  • {a}" for a in alerts] + [
            "",
            f"Full details: Firestore > scraper_health > {run_date}",
            "",
            "— Northern Rockies Snow Report automation",
        ]

        msg = MIMEText("\n".join(body_lines), 'plain')
        msg['Subject'] = f"⚠️ Snow Scraper Alert — {run_date}: {len(alerts)} issue(s)"
        msg['From']    = gmail_address
        msg['To']      = gmail_address

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
        print(f"✅ Alert email sent to {gmail_address}")

    except Exception as e:
        print(f"❌ Failed to send alert email: {e}")


def upload_all_resorts(resort_data_list: List[Dict]) -> Dict[str, int]:
    """
    Upload multiple resorts' data to Firestore.
    """
    if not resort_data_list:
        print("⚠️  No resort data to upload")
        return {'success': 0, 'failed': 0}
    
    # Initialize Firebase (should already be done, but good to check)
    if db is None:
        if initialize_firebase() is None:
            print("❌ Failed to initialize Firebase - aborting upload")
            return {'success': 0, 'failed': len(resort_data_list)}
    
    success_count = 0
    failed_count = 0
    upload_results: Dict[str, bool] = {}

    print(f"\n📤 Uploading {len(resort_data_list)} resort(s) to Firestore...")

    for resort_data in resort_data_list:
        resort = resort_data.get('resort', 'unknown')
        ok = save_resort_data_to_firestore(resort_data)
        upload_results[resort] = ok
        if ok:
            success_count += 1
        else:
            failed_count += 1

    print(f"\n📊 Upload Summary: {success_count} succeeded, {failed_count} failed")

    # --- Scraper health doc + alert email ---
    run_date = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    alerts = write_scraper_health(resort_data_list, upload_results)
    if alerts:
        send_alert_email(alerts, run_date)

    return {'success': success_count, 'failed': failed_count}


def get_historical_data(resort_name: str, days: int = 5) -> List[Dict]:
    """
    (This function is not used by the uploader, but is safe to keep for testing)
    Retrieve historical data for a resort from Firestore.
    """
    if db is None:
        if initialize_firebase() is None:
            return []
    
    try:
        today = datetime.now().date()
        start_date = today - timedelta(days=days)
        collection_ref = db.collection('snow_reports')
        query = (
            collection_ref
            .where('resort', '==', resort_name)
            .where('date', '>=', start_date.strftime('%Y-%m-%d'))
            .order_by('date', direction=firestore.Query.DESCENDING)
        )
        docs = query.stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            results.append(data)
        print(f"📊 Retrieved {len(results)} historical records for {resort_name}")
        return results
    except Exception as e:
        print(f"❌ Error retrieving historical data for {resort_name}: {e}")
        return []


if __name__ == "__main__":
    """
    Main execution block:
    1. Initialize Firebase
    2. Find all CSV files
    3. Read the *last row* of each CSV
    4. Upload that data to Firestore
    """
    
    print("🚀 Starting Firebase Uploader...")
    
    # 1. Initialize Firebase
    if initialize_firebase() is None:
        print("❌ Firebase connection failed. Exiting.")
        sys.exit(1)  # Exit the script with an error
    
    # 2. Find all the CSV files in the data directory
    try:
        all_files = os.listdir(DATA_DIR)
        # Find only the CSV files that are *not* in a sub-directory
        csv_files = [
            f for f in all_files
            if f.endswith('_snow_report.csv')
            and os.path.isfile(os.path.join(DATA_DIR, f))
        ]
        
        if not csv_files:
            print(f"⚠️  No CSV files found in {DATA_DIR}")
            sys.exit(0)
            
        print(f"🔍 Found {len(csv_files)} CSV files to process.")

        # 3. Read each CSV and convert to a list of dictionaries
        all_resort_data = []
        for csv_file in csv_files:
            try:
                csv_path = os.path.join(DATA_DIR, csv_file)
                # Use na_filter=False to prevent "nan" strings for null values
                df = pd.read_csv(csv_path, na_filter=False)
                
                # Get the most recent row (which is the last row)
                latest_data = df.iloc[-1].to_dict()
                all_resort_data.append(latest_data)
            except Exception as e:
                print(f"❌ Error reading {csv_file}: {e}")
        
        # 4. Upload all data to Firestore
        if all_resort_data:
            upload_all_resorts(all_resort_data)
        
        print("\n✅ Firebase Uploader finished.")

    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
