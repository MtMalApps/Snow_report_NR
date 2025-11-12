import os
import json
from datetime import datetime, timedelta
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase
cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if not cred_path:
    print("ERROR: FIREBASE_SERVICE_ACCOUNT environment variable not set.")
    exit(1)

if not os.path.exists(cred_path):
    print(f"ERROR: Firebase credentials file not found: {cred_path}")
    exit(1)

try:
    firebase_admin.get_app()
except ValueError:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()

print("=" * 80)
print("FIRESTORE DATABASE DIAGNOSTIC")
print("=" * 80)
print()

# 1. Check what dates are available in the database
print("1. CHECKING AVAILABLE DATES IN DATABASE")
print("-" * 80)
try:
    all_docs = db.collection("snow_reports").stream()
    dates = set()
    doc_count = 0
    for doc in all_docs:
        data = doc.to_dict()
        if 'date' in data:
            dates.add(data['date'])
        doc_count += 1
    
    print(f"Total documents in database: {doc_count}")
    print(f"Unique dates found: {len(dates)}")
    if dates:
        sorted_dates = sorted(dates, reverse=True)
        print(f"Most recent date: {sorted_dates[0]}")
        print(f"Oldest date: {sorted_dates[-1]}")
        print(f"\nAll dates in database (most recent first):")
        for d in sorted_dates:
            print(f"  - {d}")
    else:
        print("NO DATES FOUND IN DATABASE!")
except Exception as e:
    print(f"ERROR: {e}")

print()
print()

# 2. Check the last 5 days specifically (what the chart function looks for)
print("2. CHECKING LAST 5 DAYS OF DATA (what load_historical_data() queries)")
print("-" * 80)
dates_to_check = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]
print(f"Looking for these dates: {dates_to_check}")
print()

for date_str in dates_to_check:
    try:
        docs = db.collection("snow_reports").where(
            filter=firestore.FieldFilter("date", "==", date_str)
        ).stream()
        
        rows = [d.to_dict() for d in docs]
        print(f"Date {date_str}: {len(rows)} documents")
        
        if rows:
            # Sample first document
            sample = rows[0]
            print(f"  Sample resort: {sample.get('resort', 'N/A')}")
            print(f"  Last updated: {sample.get('last_updated', 'N/A')}")
            print(f"  24h snow (summit): {sample.get('snow_24h_summit', 'N/A')}")
            print(f"  24h snow (base): {sample.get('snow_24h_base', 'N/A')}")
            
            # Check if last_updated matches the query date
            last_updated = sample.get('last_updated', '')
            if last_updated:
                try:
                    last_updated_dt = pd.to_datetime(last_updated)
                    query_dt = pd.to_datetime(date_str)
                    print(f"  Last updated date: {last_updated_dt.date()}")
                    print(f"  Query date: {query_dt.date()}")
                    print(f"  Dates match: {last_updated_dt.date() == query_dt.date()}")
                except:
                    print(f"  Could not parse dates for comparison")
        print()
    except Exception as e:
        print(f"Date {date_str}: ERROR - {e}")
        print()

print()
print()

# 3. Load and analyze what load_historical_data() would return
print("3. SIMULATING load_historical_data() FUNCTION")
print("-" * 80)
try:
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]
    all_data = []
    
    for date_str in dates:
        docs = db.collection("snow_reports").where(
            filter=firestore.FieldFilter("date", "==", date_str)
        ).stream()
        for doc in docs:
            data = doc.to_dict()
            data['query_date'] = date_str
            all_data.append(data)
    
    print(f"Total records collected: {len(all_data)}")
    
    if all_data:
        df = pd.DataFrame(all_data)
        df['query_date'] = pd.to_datetime(df['query_date'])
        df['last_updated_dt'] = pd.to_datetime(df['last_updated'], errors='coerce')
        
        for col in ['snow_24h_summit', 'snow_24h_base']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        
        print(f"DataFrame shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        print()
        print("Summary of snow data:")
        print(f"  snow_24h_summit - min: {df['snow_24h_summit'].min()}, max: {df['snow_24h_summit'].max()}, sum: {df['snow_24h_summit'].sum()}")
        print(f"  snow_24h_base - min: {df['snow_24h_base'].min()}, max: {df['snow_24h_base'].max()}, sum: {df['snow_24h_base'].sum()}")
        print()
        print("Sample of data (first 5 rows):")
        print(df[['resort', 'query_date', 'last_updated_dt', 'snow_24h_summit', 'snow_24h_base']].head())
        print()
        
        # Check for stale data
        print("Checking for stale data (where last_updated_dt != query_date):")
        df['is_stale'] = df.apply(
            lambda row: row['last_updated_dt'].date() != row['query_date'].date() 
            if pd.notna(row['last_updated_dt']) else True, 
            axis=1
        )
        stale_count = df['is_stale'].sum()
        print(f"  Stale records: {stale_count} out of {len(df)} ({stale_count/len(df)*100:.1f}%)")
        
    else:
        print("NO DATA FOUND - df would be empty!")
        
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()

print()
print()

# 4. Simulate prepare_chart_data() logic
print("4. SIMULATING prepare_chart_data() FUNCTION")
print("-" * 80)

try:
    if all_data:
        df_hist = pd.DataFrame(all_data)
        df_hist['query_date'] = pd.to_datetime(df_hist['query_date'])
        df_hist['last_updated_dt'] = pd.to_datetime(df_hist['last_updated'], errors='coerce')
        
        for col in ['snow_24h_summit', 'snow_24h_base']:
            if col in df_hist.columns:
                df_hist[col] = pd.to_numeric(df_hist[col], errors='coerce').fillna(0)
        
        # Get current data (most recent date)
        latest_date = df_hist['query_date'].max()
        df_current = df_hist[df_hist['query_date'] == latest_date].copy()
        
        print(f"df_hist is empty: {df_hist.empty}")
        print(f"Number of unique resorts: {df_hist['resort'].nunique()}")
        print()
        
        chart_data = []
        today = datetime.now().date()
        date_range = [(today - timedelta(days=i)) for i in range(4, -1, -1)]
        
        print("Processing chart data for each resort and date:")
        for resort in df_current['resort'].unique():
            total_snow = 0
            print(f"\n  Resort: {resort}")
            
            for date in date_range:
                day_label = date.strftime('%a %m/%d')
                snow_val = 0
                
                day_data = df_hist[
                    (df_hist['resort'] == resort) & 
                    (df_hist['query_date'].dt.date == date)
                ]
                
                if not day_data.empty:
                    row = day_data.iloc[0]
                    last_updated_date = row['last_updated_dt'].date() if pd.notna(row['last_updated_dt']) else None
                    is_stale = last_updated_date != date if last_updated_date else True
                    
                    print(f"    {day_label}: last_updated={last_updated_date}, is_stale={is_stale}", end="")
                    
                    if not is_stale:
                        snow = row['snow_24h_summit']
                        if pd.isna(snow) or snow == 0:
                            snow = row['snow_24h_base']
                        snow_val = float(snow) if pd.notna(snow) else 0
                        print(f", snow={snow_val}\"")
                    else:
                        print(f", snow=0\" (stale)")
                else:
                    print(f"    {day_label}: No data found")
                
                chart_data.append({
                    "resort": resort,
                    "date": date,
                    "day_label": day_label,
                    "snow": snow_val
                })
                total_snow += snow_val
            
            print(f"  Total snow for {resort}: {total_snow}\"")
        
        chart_df = pd.DataFrame(chart_data)
        print(f"\nFinal chart_df shape: {chart_df.shape}")
        print(f"chart_df is empty: {chart_df.empty}")
        
        if not chart_df.empty:
            total_snow_df = chart_df.groupby('resort')['snow'].sum().reset_index().rename(columns={'snow': 'total_snow'})
            chart_df = chart_df.merge(total_snow_df, on='resort')
            print(f"Total snow across all resorts: {chart_df['total_snow'].sum()}")
            print(f"\nCondition check:")
            print(f"  not chart_df.empty: {not chart_df.empty}")
            print(f"  chart_df['total_snow'].sum() > 0: {chart_df['total_snow'].sum() > 0}")
            print(f"  WILL SHOW CHART: {not chart_df.empty and chart_df['total_snow'].sum() > 0}")
        
    else:
        print("No data to process - would return empty DataFrame")
        
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 80)
print("DIAGNOSTIC COMPLETE")
print("=" * 80)
