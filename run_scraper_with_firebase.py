#!/usr/bin/env python3
"""Run scraper and upload to Firebase"""
import subprocess
import sys
import os
import pandas as pd

DATA_DIR = os.path.expanduser("~/snow_reports/data")

print("Step 1: Running scraper...")
result = subprocess.run([sys.executable, "snow_report_scraper_oauth.py"])

if result.returncode != 0:
    print("Scraper failed!")
    sys.exit(1)

print("\nStep 2: Uploading to Firebase...")
try:
    import firebase_uploader
    
    if firebase_uploader.initialize_firebase():
        # Read CSV files and upload
        resorts = ['Snowbowl', 'Discovery', 'LookoutPass', 'BigMountain', 
                   'LostTrail', 'TetonPass', 'Showdown', 'Blacktail']
        
        uploaded = 0
        for resort in resorts:
            csv_file = os.path.join(DATA_DIR, f"{resort}_snow_report.csv")
            if os.path.exists(csv_file):
                try:
                    df = pd.read_csv(csv_file)
                    # Upload the most recent row
                    if not df.empty:
                        latest_data = df.iloc[-1].to_dict()
                        if firebase_uploader.save_resort_data_to_firestore(latest_data):
                            uploaded += 1
                except Exception as e:
                    print(f"Error uploading {resort}: {e}")
        
        print(f"\n✅ Uploaded {uploaded}/{len(resorts)} resorts to Firebase")
    else:
        print("⚠️  Firebase not configured")
        
except Exception as e:
    print(f"⚠️  Firebase upload skipped: {e}")

print("\n✅ Complete!")
