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
    
    # Initialize Firebase first
    if not firebase_uploader.initialize_firebase():
        print("⚠️ Firebase not available - continuing with email only")
    
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
        print(f"Fetching data for {scraper.resort_name}...")
        snow_data = scraper.get_snow_report()
        
        if snow_data:
            resort_data_list.append(snow_data)
            scraper.save_to_csv(snow_data)
            
            # Upload to Firebase immediately
            if firebase_uploader.db:
                firebase_uploader.save_resort_data_to_firestore(snow_data)
            
            print(f"✅ {scraper.resort_name} complete")
        else:
            print(f"⚠️ No data for {scraper.resort_name}")
    
    # Send consolidated email report
    if resort_data_list:
        print("\n📧 Sending email report...")
        gmail_address = os.environ.get('GMAIL_ADDRESS')
        credentials_path = os.environ.get('GMAIL_CREDENTIALS_PATH', 'credentials.json')
        token_path = os.environ.get('GMAIL_TOKEN_PATH', 'token.json')
        
        messenger = EmailSnowReportMessenger(
            gmail_address=gmail_address,
            credentials_path=credentials_path,
            token_path=token_path
        )
        
        try:
            messenger.send_consolidated_report(resort_data_list)
            print("✅ Email sent successfully")
        except Exception as e:
            print(f"❌ Email failed: {e}")
    
    print("\n✅ Complete!")

if __name__ == "__main__":
    main()
