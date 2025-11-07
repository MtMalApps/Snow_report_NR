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
            db = firestore.client()
            return db
        except ValueError:
            # Not initialized, initialize now
            cred = credentials.Certificate(firebase_cred_path)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("✅ Firebase initialized successfully")
            return db
    except Exception as e:
        print(f"❌ Error initializing Firebase: {e}")
        import traceback
        traceback.print_exc()
        return None


def save_resort_data_to_firestore(resort_data: Dict) -> bool:
    """
    Save a single resort's data to Firestore.
    
    Args:
        resort_data: Dictionary containing resort snow report data
        
    Returns:
        True if successful, False otherwise
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
        
        # --- NEW LOGIC TO CONVERT STRING DATA TO OBJECTS ---
        # Pandas reads the object columns as strings, we need to convert them back
        for key in ['nws_forecast', 'snotel_data']:
            if isinstance(resort_data.get(key), str):
                try:
                    # Convert the string '{"key": "value"}' into a real object
                    # We use eval() here because it handles Python dicts (like {'key': None})
                    # which json.loads would fail on.
                    resort_data[key] = eval(resort_data[key])
                except Exception:
                    # If it's a bad string (like "nan"), just set it to None
                    print(f"⚠️  Could not parse {key} string, setting to null.")
                    resort_data[key] = None
        # --- END NEW LOGIC ---

        # Collection: snow_reports (Your original, correct collection)
        collection_ref = db.collection('snow_reports')
        
        # Document ID: resort_YYYY-MM-DD_HH-MM-SS (Your original, correct ID)
        doc_id = f"{resort_name}_{timestamp_str.replace(' ', '_').replace(':', '-')}"
        
        # Check for existing entries on the same date (prevent duplicates)
        existing_query = collection_ref.where('resort', '==', resort_name).where('date', '==', date_str).limit(5).stream()
        
        deleted_count = 0
        for doc in existing_query:
            doc.reference.delete()
            deleted_count += 1
        
        if deleted_count > 0:
            print(f"🗑️  Deleted {deleted_count} existing Firestore entry(ies) for {resort_name} on {date_str}")
        
        # Add new document
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
    
    return {'success': success_count, 'failed': failed_count}


if __name__ == "__main__":
    """
    Main execution block:
    1. Initialize Firebase
    2. Find all CSV files
    3. Read the *last row* of each CSV
    4. Upload that data to Firestore
    """
    
    # We need pandas and sys for this main block to work
    import pandas as pd
    import sys 
    
    # --- We must define the data directory here ---
    DATA_DIR = os.path.expanduser("~/snow_reports/data")
    
    print("🚀 Starting Firebase Uploader...")
    
    # 1. Initialize Firebase
    if initialize_firebase() is None:
        print("❌ Firebase connection failed. Exiting.")
        sys.exit(1) # Exit the script with an error
    
    # 2. Find all the CSV files in the data directory
    try:
        all_files = os.listdir(DATA_DIR)
        # Find only the CSV files that are *not* in a sub-directory
        csv_files = [f for f in all_files if f.endswith('_snow_report.csv') and os.path.isfile(os.path.join(DATA_DIR, f))]
        
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
