"""
Firebase Uploader for Snow Report Data

This script uploads scraped snow report data to Firebase Firestore.
It works alongside the main scraper without modifying it.

Usage:
    Call this after the main scraper runs to upload data to Firestore.
    Data is passed as a list of resort dictionaries.

Environment Variables Required:
    FIREBASE_SERVICE_ACCOUNT: Path to Firebase service account JSON
"""

import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import firebase_admin
from firebase_admin import credentials, firestore

# Global Firebase client
db = None


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
        
        # Collection: snow_reports
        collection_ref = db.collection('snow_reports')
        
        # Document ID: resort_YYYY-MM-DD_HH-MM-SS
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
    
    Args:
        resort_data_list: List of dictionaries containing resort data
        
    Returns:
        Dictionary with success/failure counts
    """
    if not resort_data_list:
        print("⚠️  No resort data to upload")
        return {'success': 0, 'failed': 0}
    
    # Initialize Firebase
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


def get_historical_data(resort_name: str, days: int = 5) -> List[Dict]:
    """
    Retrieve historical data for a resort from Firestore.
    
    Args:
        resort_name: Name of the resort
        days: Number of days of history to retrieve
        
    Returns:
        List of dictionaries containing historical data
    """
    if db is None:
        if initialize_firebase() is None:
            return []
    
    try:
        # Calculate date range
        today = datetime.now().date()
        start_date = today - timedelta(days=days)
        
        # Query Firestore
        collection_ref = db.collection('snow_reports')
        query = collection_ref.where('resort', '==', resort_name).where('date', '>=', start_date.strftime('%Y-%m-%d')).order_by('date', direction=firestore.Query.DESCENDING)
        
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


def get_chart_data_from_firestore(resort_name: str, days: int = 5) -> Dict:
    """
    Get chart-ready data for a resort from Firestore.
    
    Args:
        resort_name: Name of the resort
        days: Number of days to include in chart
        
    Returns:
        Dictionary with chart data (values, dates, totals)
    """
    historical_data = get_historical_data(resort_name, days)
    
    if not historical_data:
        today = datetime.now().date()
        return {
            'resort': resort_name,
            'values': [0] * days,
            'date_labels': [(today - timedelta(days=i)) for i in range(days)],
            'total': 0,
            'has_current_data': False,
            'most_recent_update': None
        }
    
    # Build chart data
    today = datetime.now().date()
    date_labels = [(today - timedelta(days=i)) for i in range(days)]
    values = [0] * days
    
    # Create date-to-data mapping
    data_by_date = {}
    for record in historical_data:
        record_date_str = record.get('date')
        if record_date_str:
            try:
                record_date = datetime.strptime(record_date_str, '%Y-%m-%d').date()
                data_by_date[record_date] = record
            except:
                pass
    
    # Fill in values for each day
    for i, date in enumerate(date_labels):
        if date in data_by_date:
            record = data_by_date[date]
            # Try summit first, then base
            snow = record.get('snow_24h_summit') or record.get('snow_24h_base') or '0'
            if isinstance(snow, str):
                snow = snow.replace('"', '').strip()
            try:
                values[i] = float(snow)
            except:
                values[i] = 0
    
    # Get most recent update
    most_recent_update = None
    if historical_data:
        latest_record = historical_data[0]
        last_updated_str = latest_record.get('date')
        if last_updated_str:
            try:
                most_recent_update = datetime.strptime(last_updated_str, '%Y-%m-%d').date()
            except:
                pass
    
    days_since_update = (today - most_recent_update).days if most_recent_update else 999
    
    return {
        'resort': resort_name,
        'values': values,
        'date_labels': date_labels,
        'total': sum(values),
        'has_current_data': days_since_update < 2,
        'most_recent_update': most_recent_update
    }


if __name__ == "__main__":
    # Test Firebase connection
    print("🧪 Testing Firebase connection...")
    
    if initialize_firebase():
        print("✅ Firebase connection successful!")
        
        # Test query
        print("\n🔍 Testing data retrieval...")
        test_data = get_historical_data('Snowbowl', days=7)
        print(f"Found {len(test_data)} records for Snowbowl")
        
        if test_data:
            print("\nSample record:")
            print(json.dumps(test_data[0], indent=2, default=str))
    else:
        print("❌ Firebase connection failed")
