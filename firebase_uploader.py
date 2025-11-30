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
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import firebase_admin
from firebase_admin import credentials, firestore

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
    
    print(f"\n📤 Uploading {len(resort_data_list)} resort(s) to Firestore...")
    
    for resort_data in resort_data_list:
        if save_resort_data_to_firestore(resort_data):
            success_count += 1
        else:
            failed_count += 1
    
    print(f"\n📊 Upload Summary: {success_count} succeeded, {failed_count} failed")
    
    # FIXED: return real counts, not zeros
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
