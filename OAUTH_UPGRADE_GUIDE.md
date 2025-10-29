# OAuth2 Migration - What Changed & What To Do

## 🎯 Summary

Your snow report scraper has been **upgraded from SMTP app password to OAuth2 Gmail API**. This is more secure and better for automation!

### What Changed:
- ✅ **More secure** - OAuth tokens can be revoked without changing password
- ✅ **Better for automation** - Tokens auto-refresh
- ✅ **No app password needed** - Uses credentials.json + token.json instead
- ✅ **Gmail API** - More reliable than SMTP

### What Didn't Change:
- ✅ Same scrapers, same resorts, same features
- ✅ Same email format and powder alerts
- ✅ Same scheduled times (7 AM and 8 AM MT)
- ✅ Same SNOTEL integration

---

## 📦 Your Files

All updated files are ready in the outputs folder:

### Core Files (Updated for OAuth):
1. **snow_report_scraper_oauth.py** - Main scraper (OAuth2 version)
2. **requirements.txt** - Added Gmail API libraries
3. **.env.example** - Updated for OAuth paths
4. **.gitignore** - Protects OAuth files
5. **README.md** - Complete OAuth documentation

### New Files:
6. **test_gmail_oauth.py** - Test your OAuth credentials BEFORE migrating
7. **snow_report_subscribers.csv** - Example CSV with your email
8. **MIGRATION_CHECKLIST_OAUTH.md** - Step-by-step OAuth migration guide

### GitHub Files (Updated):
9. **.github/workflows/scrape_montana.yml** - Updated for OAuth
10. **.github/workflows/scrape_idaho.yml** - Updated for OAuth

### Your OAuth Credentials (Already Have):
- **credentials.json** - You uploaded this ✅
- **token.json** - You uploaded this ✅

---

## 🚦 CRITICAL: Test Locally FIRST!

**Before migrating to GitHub, you MUST test locally** to avoid debugging in GitHub Actions.

### Quick Test (5 minutes):

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test OAuth credentials
python test_gmail_oauth.py
```

**Expected output:**
```
✅ Found credentials.json
✅ Found token.json
✅ OAuth credentials are valid
✅ Gmail API service built successfully
✅ Authenticated as: jriddering@gmail.com

📧 Send a test email to yourself? (y/n):
```

Type `y` to send yourself a test email. If it works, you're ready for GitHub!

### If Test Fails:

| Error | What It Means | Fix |
|-------|---------------|-----|
| "credentials.json not found" | File not in directory | Put credentials.json in same folder as script |
| "token.json not found" | File not in directory | Put token.json in same folder as script |
| "Token expired, refreshing..." | Normal! Token expired | Script auto-fixes this - new token.json created |
| "cannot be refreshed" | Token is completely invalid | You need to re-authenticate (contact me) |

---

## 📋 Migration Steps Overview

**Detailed steps are in MIGRATION_CHECKLIST_OAUTH.md**, but here's the quick version:

### Step 1: Test Locally ✅ (DO THIS FIRST!)
```bash
python test_gmail_oauth.py
```

### Step 2: Create Private GitHub Repo
- Name: `montana-snow-reports`
- **MUST be Private**

### Step 3: Upload Files via Web
**Upload these:**
- snow_report_scraper_oauth.py
- requirements.txt
- .gitignore
- .env.example
- test_gmail_oauth.py
- README.md
- .github/workflows/*.yml

**NEVER upload:**
- ❌ credentials.json
- ❌ token.json
- ❌ .env

### Step 4: Set Up 4 GitHub Secrets

In Settings → Secrets and variables → Actions:

1. **GMAIL_ADDRESS** = `jriddering@gmail.com`
2. **GMAIL_CREDENTIALS_JSON** = `{entire contents of credentials.json}`
3. **GMAIL_TOKEN_JSON** = `{entire contents of token.json}`
4. **SUBSCRIBER_EMAILS** = `jriddering@gmail.com` (one per line)

### Step 5: Test Manual Run
- Go to Actions tab
- Run "Scrape Montana Resorts" manually
- Check for ✅ success
- Check your email!

---

## 🔐 Security - What Goes Where

### Safe to Put in GitHub (in code):
- ✅ Python scripts
- ✅ Requirements.txt
- ✅ README and docs
- ✅ .gitignore
- ✅ GitHub workflows

### ONLY in GitHub Secrets (NOT in code):
- 🔒 credentials.json contents
- 🔒 token.json contents
- 🔒 Email addresses
- 🔒 Any passwords/tokens

### Never Anywhere Public:
- ❌ Don't commit credentials.json
- ❌ Don't commit token.json
- ❌ Don't share OAuth files publicly
- ❌ Don't post secrets in forums/chat

---

## 🆚 Old vs New Setup

### Old Way (SMTP App Password):
```python
# Used SMTP with app password
server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
server.login(email, app_password)
server.send_message(msg)
```

**Problems:**
- App passwords can be stolen
- Less secure
- Gmail might block SMTP
- No auto-refresh

### New Way (OAuth2 Gmail API):
```python
# Uses Gmail API with OAuth2
creds = Credentials.from_authorized_user_file('token.json')
if creds.expired:
    creds.refresh(Request())  # Auto-refresh!
service = build('gmail', 'v1', credentials=creds)
service.users().messages().send(...)
```

**Benefits:**
- ✅ Tokens auto-refresh
- ✅ Can be revoked separately
- ✅ Limited scope (only send email)
- ✅ More reliable
- ✅ Industry best practice

---

## 🧪 Testing Checklist

Before migrating to GitHub, verify locally:

- [ ] `pip install -r requirements.txt` succeeds
- [ ] `python test_gmail_oauth.py` shows all ✅
- [ ] Test email arrives in your inbox
- [ ] credentials.json and token.json are in same directory
- [ ] No errors when running test script

After migrating to GitHub:

- [ ] All 4 secrets configured in GitHub
- [ ] Manual workflow run succeeds
- [ ] Email received from manual run
- [ ] No errors in workflow logs
- [ ] Will check automated run tomorrow at 7 AM

---

## 🎓 Understanding OAuth Files

### credentials.json
**What it is:**
- OAuth2 client credentials from Google Cloud Console
- Contains: client_id, client_secret, project_id
- Created once when you set up OAuth

**When to update:**
- If you revoke/recreate OAuth client
- If you create a new Google Cloud project
- Almost never needs updating

### token.json
**What it is:**
- Your personal access token + refresh token
- Created when you first authenticate
- Used to send emails on your behalf

**When to update:**
- After running test_gmail_oauth.py (it auto-updates)
- If token expires (script refreshes automatically)
- If you see "invalid credentials" error

**Auto-refresh behavior:**
- Access token expires after 1 hour
- Script uses refresh_token to get new access token
- Refresh token lasts months/years
- All automatic - you don't do anything!

---

## 🔄 Token Refresh Flow

Here's what happens automatically:

```
1. Script starts
2. Loads token.json
3. Checks if token valid
   ├─ Valid? → Use it to send email
   └─ Expired? → Use refresh_token to get new access_token
       └─ Save new token to token.json
4. Send email
```

**You never need to manually refresh tokens** - the script does it automatically!

---

## 🆘 Troubleshooting Quick Reference

### "OAuth credentials are invalid and cannot be refreshed"

**Cause:** Refresh token revoked or expired

**Fix:**
1. You need to re-authenticate
2. This requires running OAuth flow locally
3. Contact me for help with this

### "No module named google.auth"

**Cause:** Missing dependencies

**Fix:**
```bash
pip install -r requirements.txt
```

### "GMAIL_CREDENTIALS_JSON secret not found"

**Cause:** Secret name wrong in GitHub

**Fix:**
- Check secret name is EXACTLY: `GMAIL_CREDENTIALS_JSON`
- Case sensitive!
- No spaces!

### "Malformed JSON in secret"

**Cause:** Copy/paste error

**Fix:**
1. Open credentials.json in text editor
2. Select ALL (Ctrl+A)
3. Copy (Ctrl+C)
4. Paste carefully into GitHub secret
5. Verify it starts with `{` and ends with `}`

### Manual run succeeds but scheduled run fails

**Cause:** Usually time zone confusion

**Fix:**
- Scheduled times are in UTC
- 7 AM MT = 14:00 UTC (winter)
- 8 AM MT = 15:00 UTC (winter)
- Check the cron expressions in workflow files

---

## 📅 What Happens Next

### Tomorrow Morning:
1. GitHub Actions will run at 7:00 AM MT automatically
2. Script scrapes all resorts
3. Email sent to everyone in SUBSCRIBER_EMAILS
4. Check Actions tab to see the run

### Every Day After:
- Montana scraper: 7:00 AM MT
- Idaho scraper: 8:00 AM MT  
- Fully automated!
- No action needed from you!

### When Ski Season Starts (November):
- Resorts will have real snow data
- Powder alerts will trigger on 6"+ days
- You might want to add more resorts
- You might want to add more subscribers

---

## 🎯 Your Next Conversation Prompt

When you come back (after migration or when season starts), use this:

```
I'm continuing my Montana/Idaho snow report scraper project.

Previously, we:
1. Upgraded from SMTP to OAuth2 Gmail API
2. Created test_gmail_oauth.py for local testing
3. Updated all files for OAuth authentication
4. Prepared for GitHub Actions migration

Current status: [Choose one]
- [ ] Tested locally successfully - ready to migrate to GitHub
- [ ] Migrated to GitHub - having issues with [describe issue]
- [ ] Migrated successfully - want to add features
- [ ] Ski season started - ready to add new resorts

Here's what I uploaded:
[Attach: snow_report_scraper_oauth.py, README.md, etc.]

My immediate goal: [describe what you want to do]

Additional context: [anything else I should know]
```

---

## 📚 File Download Links

Download all files from outputs folder:

**Core Files:**
1. [snow_report_scraper_oauth.py](computer:///mnt/user-data/outputs/snow_report_scraper_oauth.py)
2. [requirements.txt](computer:///mnt/user-data/outputs/requirements.txt)
3. [.env.example](computer:///mnt/user-data/outputs/.env.example)
4. [.gitignore](computer:///mnt/user-data/outputs/.gitignore)

**Documentation:**
5. [README.md](computer:///mnt/user-data/outputs/README.md)
6. [MIGRATION_CHECKLIST_OAUTH.md](computer:///mnt/user-data/outputs/MIGRATION_CHECKLIST_OAUTH.md)

**Testing:**
7. [test_gmail_oauth.py](computer:///mnt/user-data/outputs/test_gmail_oauth.py)
8. [snow_report_subscribers.csv](computer:///mnt/user-data/outputs/snow_report_subscribers.csv)

**GitHub Workflows:**
- Create `.github/workflows/` folder manually
- Then download these into that folder:
9. [scrape_montana.yml](computer:///mnt/user-data/outputs/.github/workflows/scrape_montana.yml)
10. [scrape_idaho.yml](computer:///mnt/user-data/outputs/.github/workflows/scrape_idaho.yml)

**Your OAuth Files (you already have these):**
- credentials.json ✅
- token.json ✅

---

## ✅ Final Checklist Before Migration

Have you:
- [ ] Downloaded all files from outputs
- [ ] Have credentials.json and token.json from previous work
- [ ] Installed Python dependencies: `pip install -r requirements.txt`
- [ ] Tested OAuth: `python test_gmail_oauth.py` - all ✅
- [ ] Test email received successfully
- [ ] Read MIGRATION_CHECKLIST_OAUTH.md
- [ ] Ready to create GitHub repo

**If all checked, you're ready to migrate!**

---

## 🎊 What You've Accomplished

You now have:
- ✅ Production-ready OAuth2 authentication
- ✅ Secure credential management
- ✅ Local testing capability
- ✅ Complete documentation
- ✅ GitHub Actions workflows ready
- ✅ Migration guide with troubleshooting

**Next step:** Follow MIGRATION_CHECKLIST_OAUTH.md to get it running on GitHub!

---

Good luck with your migration! Test locally first, and you'll have smooth sailing! 🎿❄️

Remember: **Test locally → GitHub is easy**. Skip testing → GitHub is frustrating!
