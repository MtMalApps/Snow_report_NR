# Montana/Idaho Snow Report Scraper

Automated daily snow report scraper for Montana and Idaho ski resorts with email notifications.

## 🎿 Features

- **8 Ski Resorts**: Snowbowl, Discovery, Lookout Pass, Big Mountain, Lost Trail, Teton Pass, Showdown, Blacktail
- **SNOTEL Integration**: Backcountry snow quality and depth data
- **Powder Alerts**: Special notifications when snowfall exceeds 6 inches
- **Automated Daily Emails**: GitHub Actions runs automatically at 7 AM and 8 AM MT
- **CSV Data Storage**: Historical data saved for analysis
- **OAuth2 Security**: Uses Gmail API with OAuth2 (more secure than app passwords)

## 📋 What You Have

All files are ready for GitHub migration:

```
montana-snow-reports/
├── snow_report_scraper_oauth.py   # Main scraper (OAuth2 version)
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment variable template
├── .gitignore                      # Git ignore file
├── credentials.json                # Gmail OAuth credentials (DON'T COMMIT!)
├── token.json                      # Gmail OAuth token (DON'T COMMIT!)
├── test_gmail_oauth.py            # Test script
├── snow_report_subscribers.csv    # Email recipients
└── .github/
    └── workflows/
        ├── scrape_montana.yml     # 7 AM MT schedule
        └── scrape_idaho.yml       # 8 AM MT schedule
```

## 🚀 Quick Start - Test Locally First

### 1. Test OAuth Credentials

Before migrating to GitHub, verify your Gmail OAuth setup works:

```bash
# Install dependencies
pip install -r requirements.txt

# Test Gmail authentication
python test_gmail_oauth.py
```

This will:
- ✅ Verify credentials.json and token.json exist
- ✅ Refresh OAuth token if needed
- ✅ Test Gmail API connection
- ✅ Optionally send a test email

### 2. Test the Scraper Locally

```bash
# Set up environment
cp .env.example .env
# Edit .env and set GMAIL_ADDRESS=jriddering@gmail.com

# Make sure credentials.json and token.json are in the same directory

# Run the scraper
python snow_report_scraper_oauth.py
```

## 🌐 GitHub Migration Guide

### Step 1: Create GitHub Repository

1. Go to https://github.com and log in
2. Click "+" → "New repository"
3. Name: `montana-snow-reports`
4. **IMPORTANT: Select "Private"**
5. Don't initialize with README
6. Click "Create repository"

### Step 2: Upload Files via Web Interface

**IMPORTANT: Do NOT upload credentials.json or token.json directly!**

1. In your new repo, click "uploading an existing file"
2. Upload these files:
   - `snow_report_scraper_oauth.py`
   - `requirements.txt`
   - `.gitignore`
   - `.env.example`
   - `test_gmail_oauth.py`
   - `README.md` (this file)
3. Create folder `.github/workflows/` and upload:
   - `scrape_montana.yml`
   - `scrape_idaho.yml`
4. Commit the files

### Step 3: Set Up GitHub Secrets

This is the critical step for security!

1. In your repo, go to: **Settings → Secrets and variables → Actions**
2. Click **"New repository secret"** and add each of these:

#### Secret 1: GMAIL_ADDRESS
```
Name: GMAIL_ADDRESS
Value: jriddering@gmail.com
```

#### Secret 2: GMAIL_CREDENTIALS_JSON
```
Name: GMAIL_CREDENTIALS_JSON
Value: [Paste entire contents of credentials.json file]
```

**How to get this:**
- Open `credentials.json` in a text editor
- Copy EVERYTHING (including the curly braces)
- Paste into the secret value

#### Secret 3: GMAIL_TOKEN_JSON
```
Name: GMAIL_TOKEN_JSON
Value: [Paste entire contents of token.json file]
```

**How to get this:**
- Open `token.json` in a text editor
- Copy EVERYTHING (including the curly braces)
- Paste into the secret value

#### Secret 4: SUBSCRIBER_EMAILS
```
Name: SUBSCRIBER_EMAILS
Value: [One email per line]
```

For testing, just use:
```
jriddering@gmail.com
```

Later, add more emails:
```
jriddering@gmail.com
friend1@example.com
friend2@example.com
```

### Step 4: Test GitHub Actions

1. Go to the **Actions** tab in your repo
2. Click on **"Scrape Montana Resorts"**
3. Click **"Run workflow"** dropdown → **"Run workflow"**
4. Wait 1-2 minutes
5. ✅ Check for green checkmark
6. 📧 Check your email!

**If it fails:**
- Click on the failed run to see error logs
- Common issues:
  - Wrong secret names (must match exactly!)
  - Malformed JSON in credentials/token
  - Expired OAuth token (run test_gmail_oauth.py locally to refresh)

### Step 5: Verify Schedule

The workflows will now run automatically:
- **Montana**: Daily at 7:00 AM MT
- **Idaho**: Daily at 8:00 AM MT

Check the Actions tab tomorrow morning to confirm they ran!

## 🔐 Security Notes

### What's Safe vs. Dangerous

**✅ SAFE to commit to GitHub:**
- Python code (`snow_report_scraper_oauth.py`)
- Requirements, README, .gitignore
- GitHub Actions workflows
- .env.example (template only)

**❌ NEVER commit to GitHub:**
- `credentials.json` - Contains OAuth client secret
- `token.json` - Contains your access token
- `.env` - Contains actual credentials
- CSV files with subscriber emails
- Any file with actual passwords/tokens

### Why OAuth is Better Than App Passwords

OAuth2 credentials:
- ✅ Can be revoked without changing your Gmail password
- ✅ Have limited scope (only Gmail sending)
- ✅ Auto-refresh tokens
- ✅ More secure for automation

## 📊 Data Schema

Each resort returns standardized data:

```python
{
    'resort': 'Snowbowl',
    'date': '2025-10-29',
    'timestamp': '2025-10-29 07:00:00',
    'last_updated': '2025-10-29 06:30 AM',
    'snow_overnight': '3',
    'snow_24h_base': '5',
    'snow_24h_summit': '7',
    'snow_48h_base': '10',
    'snow_48h_summit': '12',
    'snow_7day': None,
    'base_depth': '45',
    'summit_depth': '60',
    'temp_base': '28°F',
    'temp_summit': '22°F',
    'wind_speed': '10 mph',
    'conditions_surface': 'Powder',
    'weather_current': 'Light Snow',
    'lifts_open': '4/5',
    'runs_open': '25/35',
    'comments': 'Great conditions!'
}
```

## 🔧 Troubleshooting

### OAuth Token Expired

If you see "OAuth credentials are invalid":

```bash
# Run test script locally to refresh
python test_gmail_oauth.py

# Then update GitHub secret with new token.json contents
```

### Emails Not Sending

1. Verify all 4 secrets are set correctly in GitHub
2. Check secret names match exactly (case-sensitive)
3. Verify credentials.json and token.json are valid JSON
4. Run test_gmail_oauth.py locally to verify OAuth works

### Scraper Fails on Specific Resort

Resort websites change frequently. Check the error logs in GitHub Actions and:
1. Open the resort's website
2. Check if HTML structure changed
3. Update the scraper's HTML parsing code

## 🎯 Next Steps

### Phase 1: Get It Working (Now)
- ✅ Test OAuth locally
- ✅ Migrate to GitHub
- ✅ Verify automated runs

### Phase 2: Expand (November)
- Add new resorts: Bridger Bowl, Red Lodge, Great Divide, Turner
- Test with real snow data

### Phase 3: Weather Forecasts (December)
- Integrate National Weather Service API
- Add forecast data to emails
- Build foundation for mobile app

### Phase 4: Mobile App (Winter 2025/26)
- Simple forecast viewer
- Current conditions
- Historical charts
- Polish and release

## 📞 Support

Having issues? Common problems:

1. **"Invalid credentials"**: Refresh token locally, update GitHub secret
2. **"No module named google.auth"**: Run `pip install -r requirements.txt`
3. **"Subscribers file not found"**: Check SUBSCRIBER_EMAILS secret format
4. **Workflow not running**: Check cron schedule and Actions are enabled

## 📝 License

Personal project for automated snow reports. Not for commercial use.

---

Good luck with your migration! May you always wake up to powder days! 🎿❄️
