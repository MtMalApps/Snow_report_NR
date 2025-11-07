#!/usr/bin/env python3
"""
Run scraper and upload to Firebase.
This is the main production script for GitHub Actions.
It gathers all data, saves it to CSV, and uploads the
complete data to Firebase.
"""
import sys
import os
import time

# Import all our library modules
from snow_report_scraper_oauth import * # Imports all scrapers, EmailTheme, etc.
import firebase_uploader # Imports our uploader functions

def main():
    """Run scraper with Firebase uploads."""
    
    # Initialize Firebase first
    if not firebase_uploader.initialize_firebase():
        print("⚠️ Firebase not available - continuing with email only")
    
    # --- Initialize all fetchers ---
    snotel_fetcher = SNOTELDataFetcher()
    nws_fetcher = NWSStormOutlook()
    # ---

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
        print(f"--- Fetching data for {scraper.resort_name} ---")
        snow_data = scraper.get_snow_report()
        
        if snow_data:
            resort_name = snow_data["resort"]

            # --- GATHER ALL DATA ---
            # 1. Get SNOTEL data and add it to the main object
            print(f"Fetching SNOTEL for: {resort_name}")
            try:
                snow_data['snotel_data'] = snotel_fetcher.get_snowfall_report(resort_name)
            except Exception as e:
                print(f"⚠️ SNOTEL fetch failed for {resort_name}: {e}")
                snow_data['snotel_data'] = None

            # 2. Get NWS data and add it to the main object
            print(f"Fetching NWS for: {resort_name}")
            coords = RESORT_COORDS.get(resort_name)
            if coords:
                try:
                    snow_data['nws_forecast'] = nws_fetcher.get_outlook(coords["lat"], coords["lon"])
                    time.sleep(0.3) # Respect NWS API rate limits
                except Exception as e:
                    print(f"⚠️ NWS fetch failed for {resort_name}: {e}")
                    snow_data['nws_forecast'] = None
            else:
                snow_data['nws_forecast'] = None
            # --- END OF DATA GATHERING ---

            # Add to the list for the email
            resort_data_list.append(snow_data)
            
            # 3. Save the *complete* data to the CSV
            try:
                scraper.save_to_csv(snow_data)
                print(f"✅ Complete data saved to CSV for {scraper.resort_name}")
            except Exception as e:
                print(f"❌ Error saving CSV for {scraper.resort_name}: {e}")
            
            # 4. Upload the *complete* data to Firebase
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
            # Pass the complete data to the sender (it no longer fetches)
            messenger.send_consolidated_report(resort_data_list)
            print("✅ Email sent successfully")
        except Exception as e:
            print(f"❌ Email failed: {e}")
    
    print("\n✅ Complete!")

if __name__ == "__main__":
    main()
