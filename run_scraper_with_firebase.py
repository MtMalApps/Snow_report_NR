#!/usr/bin/env python3
"""
Run scraper and upload to Firebase.
Modified to upload data directly during scraping, not from CSV files.
"""
import sys
import os

# Import the scraper modules
from snow_report_scraper_oauth import *
import firebase_uploader

def main():
    """Run scraper with Firebase uploads."""
    
    # --- This script's MAIN is now just a wrapper ---
    # --- It calls the main() function from the other script ---
    
    # We must import the main scraper script itself
    import snow_report_scraper_oauth
    
    # Call the main function from the scraper script
    # This will do all the work:
    # 1. Scrape data
    # 2. Fetch NWS/SNOTEL
    # 3. Save complete CSVs
    # 4. Send the email
    snow_report_scraper_oauth.main()
    
    # After the main scraper runs, the CSVs are complete.
    # Now, we run the uploader script.
    
    # We can't just call firebase_uploader.main() because it's not a function.
    # We must run it as a separate process, or copy its `if __name__ == "__main__":` logic.
    # Easiest way: just call the uploader.
    
    print("\n--- Running Firebase Uploader ---")
    # This assumes firebase_uploader.py is in the same directory
    # and that the venv is active.
    
    # This is a bit of a hack, but it will work in GitHub Actions
    # We will re-initialize and call the upload_all_resorts function
    
    # We need pandas for this
    import pandas as pd
    
    DATA_DIR = os.path.expanduser("~/snow_reports/data")
    
    print("🚀 Starting Firebase Uploader...")
    
    # 1. Initialize Firebase
    if firebase_uploader.initialize_firebase() is None:
        print("❌ Firebase connection failed. Exiting.")
        sys.exit(1) # Exit the script with an error
    
    # 2. Find all the CSV files in the data directory
    try:
        all_files = os.listdir(DATA_DIR)
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
                df = pd.read_csv(csv_path, na_filter=False) 
                latest_data = df.iloc[-1].to_dict()
                all_resort_data.append(latest_data)
            except Exception as e:
                print(f"❌ Error reading {csv_file}: {e}")
        
        # 4. Upload all data to Firestore
        if all_resort_data:
            firebase_uploader.upload_all_resorts(all_resort_data)
        
        print("\n✅ Firebase Uploader finished.")

    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
