# GitHub Migration Checklist - OAuth2 Version

Follow these steps to migrate your snow report scraper to GitHub with OAuth2 Gmail authentication.

## ✅ Pre-Migration

- [x] Code updated for OAuth2
- [x] OAuth credentials working (credentials.json + token.json)
- [x] Test script created
- [x] GitHub Actions workflows created
- [x] Documentation updated

## 🧪 Step 1: Test Locally FIRST (15 minutes)

**CRITICAL: Do this before GitHub migration to avoid frustration!**

### 1a. Install Dependencies
```bash
pip install -r requirements.txt
```

### 1b. Test OAuth Credentials
```bash
python test_gmail_oauth.py
```

**Expected output:**
```
✅ Found credentials.json
✅ Found token.json
✅ OAuth credentials are valid
✅ Gmail API service built successfully
✅ Authenticated as: jriddering@gmail.com
```

**If you see errors:**
- ❌ "credentials.json not found" → Make sure file is in same directory
- ❌ "invalid credentials" → Your token expired, but test script will refresh it
- ❌ "cannot be refreshed" → You need to re-authenticate (see troubleshooting)

### 1c. Send Test Email (Optional)
When prompted, type 'y' to send a test email to yourself.

### 1d. Test Full Scraper (Optional but Recommended)
```bash
# Create .env file
cp .env.example .env

# Edit .env and set:
# GMAIL_ADDRESS=jriddering@gmail.com

# Run scraper
python snow_report_scraper_oauth.py
```

**If this works locally, GitHub migration will be smooth!**

## 🌐 Step 2: Create GitHub Repository (5 minutes)

1. Go to https://github.com and log in
2. Click "+" icon → "New repository"
3. Repository settings:
   - Name: `montana-snow-reports`
   - Description: "Automated snow report scraper for Montana/Idaho ski resorts"
   - **Privacy: Private** ⚠️ CRITICAL
   - Don't initialize with README
4. Click "Create repository"
5. **Save the repository URL** (you'll need it)

## 📤 Step 3: Upload Code (10 minutes)

### Option: Using GitHub Web Interface (Easier on Chromebook)

1. In your new empty repo, click **"uploading an existing file"**

2. **Drag and drop these files:**
   - `snow_report_scraper_oauth.py`
   - `requirements.txt`
   - `.gitignore`
   - `.env.example`
   - `test_gmail_oauth.py`
   - `README.md`

3. **DO NOT upload:**
   - ❌ `credentials.json`
   - ❌ `token.json`
   - ❌ `.env`
   - ❌ CSV files

4. Scroll down and click **"Commit changes"**

5. **Create the workflows folder:**
   - On the main repo page, click **"Add file" → "Create new file"**
   - In filename box, type: `.github/workflows/scrape_montana.yml`
   - Paste contents of `scrape_montana.yml`
   - Click "Commit new file"
   - Repeat for `scrape_idaho.yml`

## 🔐 Step 4: Set Up GitHub Secrets (10 minutes)

**THIS IS THE MOST IMPORTANT STEP!**

1. In your repo, click **Settings** (top right)
2. In left sidebar, click **Secrets and variables → Actions**
3. Click **"New repository secret"** button

### Secret #1: GMAIL_ADDRESS
```
Name: GMAIL_ADDRESS
Value: jriddering@gmail.com
```
Click "Add secret"

### Secret #2: GMAIL_CREDENTIALS_JSON

**⚠️ Pay close attention here!**

1. Click "New repository secret"
2. Name: `GMAIL_CREDENTIALS_JSON`
3. For the value:
   - Open `credentials.json` in a text editor
   - Select ALL text (Ctrl+A / Cmd+A)
   - Copy (Ctrl+C / Cmd+C)
   - Paste into the GitHub secret value box
4. Click "Add secret"

**The value should look like:**
```json
{"installed":{"client_id":"300440183873-...","project_id":"python-mailer-473720",...}}
```

### Secret #3: GMAIL_TOKEN_JSON

1. Click "New repository secret"
2. Name: `GMAIL_TOKEN_JSON`
3. For the value:
   - Open `token.json` in a text editor
   - Select ALL text
   - Copy
   - Paste into the GitHub secret value box
4. Click "Add secret"

**The value should look like:**
```json
{"token": "ya29.a0AQQ_BDR...","refresh_token": "1//06yuS4mT...","token_uri": ...}
```

### Secret #4: SUBSCRIBER_EMAILS

1. Click "New repository secret"
2. Name: `SUBSCRIBER_EMAILS`
3. For the value (testing with just your email):
   ```
   jriddering@gmail.com
   ```
4. Click "Add secret"

**Later, when ready for production, update this secret with:**
```
jriddering@gmail.com
friend1@example.com
friend2@example.com
```
(One email per line, no commas)

### ✅ Verify All Secrets Are Set

You should now see 4 secrets:
- `GMAIL_ADDRESS`
- `GMAIL_CREDENTIALS_JSON`
- `GMAIL_TOKEN_JSON`
- `SUBSCRIBER_EMAILS`

## 🧪 Step 5: Test GitHub Actions (5 minutes)

**Now for the moment of truth!**

1. Click the **"Actions"** tab in your repo
2. You should see two workflows:
   - "Scrape Montana Resorts"
   - "Scrape Idaho Resorts"
3. Click on **"Scrape Montana Resorts"**
4. Click the **"Run workflow"** dropdown button (right side)
5. Click **"Run workflow"** button
6. Page will refresh - click on the yellow dot/running icon
7. Click on the job name to see live logs
8. Wait 1-2 minutes...
9. Look for green checkmark! ✅

### What to Expect

**✅ Success looks like:**
```
Set up OAuth credentials
Run Montana scraper
Fetching data for Snowbowl...
Fetching data for Discovery...
...
Report sent successfully to 1 recipients
```

**❌ Failure looks like:**
Common errors and fixes:

| Error | Cause | Fix |
|-------|-------|-----|
| "invalid credentials" | Token expired | Run test_gmail_oauth.py locally, update GMAIL_TOKEN_JSON secret |
| "No module named google.auth" | Missing dependency | Check requirements.txt is uploaded |
| "GMAIL_CREDENTIALS_JSON not found" | Secret name wrong | Check secret name matches exactly |
| Malformed JSON | Copy/paste error in secret | Re-copy credentials.json contents carefully |

## 📧 Step 6: Check Your Email

If the workflow succeeded, you should receive an email at jriddering@gmail.com with:
- Subject: "Daily Snow Report - 2025-10-29"
- HTML formatted report
- Summary table of all resorts
- Individual resort details
- SNOTEL backcountry data

## ⏰ Step 7: Verify Schedule

Your workflows are now scheduled:
- **Montana**: Runs daily at 7:00 AM Mountain Time (14:00 UTC)
- **Idaho**: Runs daily at 8:00 AM Mountain Time (15:00 UTC)

**To verify:**
1. Go to Actions tab
2. Click on a workflow
3. Look for "on: schedule" section showing the cron schedule

**Note:** They won't run until tomorrow morning! Check the Actions tab tomorrow at 7 AM to see the automated run.

## 🎉 Success Criteria

You've successfully migrated when:
- ✅ All 4 secrets are configured
- ✅ Manual workflow run succeeds (green checkmark)
- ✅ Test email received
- ✅ No errors in workflow logs
- ✅ Scheduled runs will happen automatically tomorrow

## 🔧 Troubleshooting Guide

### OAuth Token Keeps Expiring

**Problem:** Workflow fails with "invalid credentials"

**Solution:**
1. Run `python test_gmail_oauth.py` locally
2. It will refresh your token automatically
3. Open `token.json` - it should have new values
4. Copy ALL contents of new `token.json`
5. Update GitHub secret `GMAIL_TOKEN_JSON` with new contents

### Workflow Runs But No Email

**Problem:** Workflow succeeds but email not received

**Check:**
1. Is GMAIL_ADDRESS secret set correctly?
2. Is SUBSCRIBER_EMAILS secret formatted correctly (one email per line)?
3. Check spam folder
4. Look at workflow logs for "Report sent successfully" message

### JSON Parse Errors

**Problem:** "Error parsing JSON" in workflow

**Solution:**
1. Open credentials.json or token.json in a text editor
2. Verify it's valid JSON (use https://jsonlint.com)
3. Copy entire contents carefully (including { and })
4. Update the corresponding GitHub secret

### Need to Re-authenticate OAuth

**Problem:** "cannot refresh token" error

**Solution:**
You need to regenerate OAuth credentials. This happens if:
- Refresh token was revoked
- Google project credentials changed
- Too much time passed

**Steps to regenerate:**
1. Go to https://console.cloud.google.com
2. Select project "python-mailer-473720"
3. APIs & Services → Credentials
4. Delete old OAuth 2.0 Client ID
5. Create new OAuth 2.0 Client ID
6. Download new credentials.json
7. Run authentication flow locally to get new token.json
8. Update both GitHub secrets

## 🚀 Next Steps After Migration

### Immediate (Next Day)
- Check Actions tab at 7 AM to verify automated run
- Confirm email arrives automatically
- Monitor for a few days

### Short Term (Before Ski Season)
- Add your friends' emails to SUBSCRIBER_EMAILS secret
- Consider adding new resort scrapers (Bridger, Red Lodge)
- Test with real snow data when resorts start reporting

### Long Term (This Winter)
- Integrate NWS weather forecasts
- Build mobile app for viewing reports
- Add historical data visualization

## 📝 Important Notes

### About OAuth Tokens
- Tokens auto-refresh when expired
- Refresh tokens are long-lived (months/years)
- If refresh fails, you need to re-authenticate
- Running test_gmail_oauth.py locally fixes most token issues

### About GitHub Actions
- Free tier: 2,000 minutes/month (you'll use ~20)
- Runs happen on GitHub's servers (not your Chromebook)
- Logs are kept for 90 days
- Failed runs don't send emails (good!)

### About Security
- Repository MUST be private
- Never commit credentials.json or token.json
- Secrets are encrypted at rest
- Only you can see secret values (not even in logs)

## ✅ Final Checklist

Before considering migration complete:

- [ ] All 4 GitHub secrets configured
- [ ] Manual workflow test passed
- [ ] Test email received
- [ ] Reviewed workflow logs (no errors)
- [ ] Scheduled times verified (7 AM and 8 AM MT)
- [ ] credentials.json and token.json NOT in repository
- [ ] Will check automated run tomorrow morning

---

## 🎊 Congratulations!

If you made it here and everything works, you now have:
- Fully automated daily snow reports
- Secure OAuth2 authentication
- Scheduled GitHub Actions workflows
- Email notifications ready to go

**Welcome to hands-free powder tracking!** 🎿❄️

Next conversation, we can work on adding new resorts or NWS forecasts!
