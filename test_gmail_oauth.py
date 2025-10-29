#!/usr/bin/env python3
"""
Test script to verify Gmail OAuth2 credentials work.
Run this locally before migrating to GitHub to ensure email sending works.
"""

import os
import base64
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

def test_gmail_oauth():
    """Test Gmail API OAuth2 authentication and send a test email."""
    
    print("🔍 Testing Gmail OAuth2 credentials...")
    
    # Paths to OAuth files
    credentials_path = "credentials.json"
    token_path = "token.json"
    
    # Check if files exist
    if not os.path.exists(credentials_path):
        print(f"❌ ERROR: {credentials_path} not found!")
        return False
        
    if not os.path.exists(token_path):
        print(f"❌ ERROR: {token_path} not found!")
        return False
    
    print(f"✅ Found credentials.json")
    print(f"✅ Found token.json")
    
    try:
        # Load credentials
        creds = Credentials.from_authorized_user_file(token_path)
        
        # Check if credentials are valid
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                print("🔄 Token expired, refreshing...")
                creds.refresh(Request())
                # Save refreshed token
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
                print("✅ Token refreshed successfully")
            else:
                print("❌ ERROR: Credentials invalid and cannot be refreshed")
                return False
        
        print("✅ OAuth credentials are valid")
        
        # Build Gmail service
        service = build('gmail', 'v1', credentials=creds)
        print("✅ Gmail API service built successfully")
        
        # Get your email address
        profile = service.users().getProfile(userId='me').execute()
        email_address = profile['emailAddress']
        print(f"✅ Authenticated as: {email_address}")
        
        # Ask if user wants to send test email
        send_test = input("\n📧 Send a test email to yourself? (y/n): ").strip().lower()
        
        if send_test == 'y':
            print("📤 Sending test email...")
            
            # Create test message
            message = MIMEText("This is a test email from your snow report scraper!\n\nIf you're seeing this, your OAuth2 credentials are working perfectly! 🎿❄️")
            message['Subject'] = "✅ Snow Report Scraper - OAuth2 Test Successful"
            message['From'] = email_address
            message['To'] = email_address
            
            # Encode and send
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
            send_result = service.users().messages().send(
                userId='me',
                body={'raw': raw_message}
            ).execute()
            
            print(f"✅ Test email sent successfully!")
            print(f"   Message ID: {send_result['id']}")
            print(f"   Check your inbox at {email_address}")
        
        print("\n🎉 All tests passed! Your OAuth2 setup is ready for GitHub!")
        return True
        
    except HttpError as error:
        print(f"❌ Gmail API error: {error}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("Gmail OAuth2 Test Script")
    print("=" * 60)
    print()
    
    success = test_gmail_oauth()
    
    print()
    print("=" * 60)
    if success:
        print("✅ SUCCESS: Ready to migrate to GitHub!")
    else:
        print("❌ FAILED: Fix the errors above before migrating")
    print("=" * 60)
