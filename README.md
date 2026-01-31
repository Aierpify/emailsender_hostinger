# ⚡ High-Speed Email Sender - 100+ Email Optimized

A robust, high-performance email sender with concurrent SMTP processing, optimized for sending to 100+ recipients.

## 🚀 Performance Features

✅ **Bulk Mode** - Automatically switches to high-speed mode for 20+ emails
✅ **15-20 Concurrent Connections** - Sends multiple emails simultaneously
✅ **Smart Batching** - Efficient resource management
✅ **Auto-Retry** - 3 attempts per email with exponential backoff
✅ **Real-time Progress** - Live progress bar and statistics
✅ **Error Resilience** - Continues sending even if some fail

## 📊 Speed Benchmarks

| Recipients | Mode | Estimated Time | Concurrent |
|-----------|------|---------------|-----------|
| 1-19 | Standard | 1-2 min | 10 |
| 20-99 | Bulk | 2-4 min | 15 |
| 100-200 | Bulk | 4-8 min | 15 |
| 200-500 | Bulk | 8-15 min | 15-20 |

*Times vary based on SMTP server response and network speed*

## Quick Start

### 1. Install Dependencies

```bash
pip install Flask Flask-CORS
```

### 2. Start the Backend Server

```bash
python email_server.py
```

The server will start on `http://localhost:5000`

### 3. Open the Web Interface

Open `email_sender_optimized.html` in your web browser.

## Usage

1. **Enter SMTP Credentials**
   - Email Address: Your Hostinger email
   - Password: Your email password
   - SMTP Server: `smtp.hostinger.com` (pre-filled)
   - SMTP Port: `465` (pre-filled for SSL)

2. **Compose Email**
   - Subject: Email subject line
   - Body: Your message (HTML supported)

3. **Add Recipients** (100+ supported!)
   - Paste one email address per line
   - Watch the counter update in real-time
   - System auto-detects bulk mode for 20+ emails

4. **Send**
   - Click "🚀 Send Emails"
   - System automatically chooses optimal mode
   - Watch real-time progress

## 🎯 Optimizations for 100+ Emails

### Automatic Mode Switching
- **< 20 emails**: Standard Mode (10 concurrent)
- **≥ 20 emails**: Bulk Mode (15 concurrent)

### Backend Optimizations
- ThreadPoolExecutor for concurrent processing
- Connection pooling and reuse
- Smart retry logic with exponential backoff
- 30-second timeout per email
- Graceful error handling

### Tips for Best Performance

1. **Use Valid Email Lists**
   - Remove duplicates before sending
   - Validate email formats
   - Remove bounced addresses from previous sends

2. **SMTP Server Limits**
   - Check your provider's hourly/daily limits
   - Hostinger typically allows 100-500 emails/hour
   - Some providers may require warming up new accounts

3. **Split Large Batches**
   - For 500+ emails, consider splitting into multiple sessions
   - Wait 5-10 minutes between large batches
   - Monitor for rate limiting errors

4. **Network Considerations**
   - Use stable internet connection
   - Avoid VPN if possible (may trigger spam filters)
   - Run from server/cloud for best speed

## SMTP Settings

### Hostinger (Optimized)
```
Server: smtp.hostinger.com
Port: 465 (SSL)
Concurrent: 15-20
Rate Limit: ~300/hour
```

### Gmail
```
Server: smtp.gmail.com
Port: 465 or 587
Concurrent: 10-15
Rate Limit: ~100-500/day (varies)
Note: Use App Password
```

### Outlook/Office365
```
Server: smtp-mail.outlook.com
Port: 587
Concurrent: 10
Rate Limit: ~300/day
```

### SendGrid/SMTP2GO (Transactional)
```
Server: Check provider docs
Port: Usually 587
Concurrent: 20-50
Rate Limit: Depends on plan
```

## API Endpoints

### Bulk Send (Recommended for 20+)
```bash
curl -X POST http://localhost:5000/api/send-bulk \
  -H "Content-Type: application/json" \
  -d '{
    "from": "your@email.com",
    "password": "your_password",
    "recipients": ["email1@test.com", "email2@test.com", ...],
    "subject": "Subject",
    "body": "Message",
    "smtp_server": "smtp.hostinger.com",
    "smtp_port": "465",
    "max_workers": 15
  }'
```

### Single Send
```bash
curl -X POST http://localhost:5000/api/send-email \
  -H "Content-Type: application/json" \
  -d '{
    "from": "your@email.com",
    "password": "your_password",
    "to": "recipient@test.com",
    "subject": "Subject",
    "body": "Message"
  }'
```

## Performance Tuning

### Increase Concurrency (Advanced)

Edit `email_sender_optimized.html`:
```javascript
max_workers: 20  // Increase from 15 to 20
```

Edit `email_server.py`:
```python
max_workers = data.get('max_workers', 20)  // Default to 20
```

⚠️ **Warning**: Higher concurrency may trigger rate limits or spam filters

### Batch Size Adjustment

In `email_sender_optimized.html`:
```javascript
const batchSize = 15; // Increase from 10 to 15
```

## Troubleshooting 100+ Emails

### Rate Limiting
**Symptom**: Emails fail after first 50-100
**Solution**: 
- Reduce `max_workers` to 5-10
- Add delays between batches
- Check provider limits

### Timeout Errors
**Symptom**: Many "connection timeout" errors
**Solution**:
- Reduce concurrent connections
- Check internet stability
- Increase timeout in `email_server.py`

### Authentication Failures
**Symptom**: "Authentication failed" after some emails
**Solution**:
- Provider may be rate limiting login attempts
- Reduce concurrent connections
- Use App Password instead of regular password

### Memory Issues (500+ emails)
**Symptom**: Server slows down or crashes
**Solution**:
- Split into batches of 200-300
- Restart server between batches
- Increase server resources

## Best Practices for Bulk Sending

### Email Deliverability
1. **Warm Up New Accounts**
   - Start with 20-30 emails/day
   - Gradually increase over 2 weeks
   - Build sender reputation

2. **Avoid Spam Filters**
   - Use proper FROM name
   - Include unsubscribe option
   - Avoid spam trigger words
   - Don't use ALL CAPS in subject
   - Include text version with HTML

3. **List Hygiene**
   - Remove bounced emails
   - Validate email formats
   - Remove duplicates
   - Honor unsubscribe requests

4. **Monitor Results**
   - Track bounce rates
   - Watch for spam complaints
   - Monitor delivery rates
   - Check sender reputation

### Legal Compliance
- ✅ Get consent before sending (CAN-SPAM, GDPR)
- ✅ Include physical address
- ✅ Provide unsubscribe mechanism
- ✅ Honor opt-out requests within 10 days
- ✅ Don't buy email lists

## Production Deployment

For serious bulk sending, consider:

1. **Dedicated SMTP Service**
   - SendGrid, Mailgun, Amazon SES
   - Better deliverability
   - Higher rate limits
   - Detailed analytics

2. **Server Deployment**
```bash
# Install gunicorn
pip install gunicorn

# Run with 4 workers
gunicorn -w 4 -b 0.0.0.0:5000 email_server:app
```

3. **Monitoring**
   - Log all sends
   - Track bounce rates
   - Monitor sender reputation
   - Set up alerts for failures

4. **Security**
   - Use environment variables for credentials
   - Add API authentication
   - Implement rate limiting
   - Use HTTPS

## Example: Sending to 100 Emails

```
Recipients: 100 emails
Mode: Bulk (automatic)
Concurrent: 15 connections
Time: ~3-4 minutes
Success Rate: 95-98% (typical)
```

**What Happens:**
1. System detects 100 emails → switches to Bulk Mode
2. Backend creates 15 concurrent SMTP connections
3. Sends in batches of 15 until complete
4. Retries failed emails (up to 3 times)
5. Shows final report with success/failure breakdown

## Files

- `email_sender_optimized.html` - UI with bulk mode support
- `email_server.py` - Backend with 15-20 concurrent workers
- `requirements.txt` - Python dependencies
- `README.md` - This file

## Security Notes

⚠️ For production:
- Use environment variables for passwords
- Add API authentication
- Implement rate limiting
- Use HTTPS/SSL
- Don't expose to public internet without auth

## Support

Having issues with 100+ emails?
1. Check SMTP provider limits
2. Reduce concurrent connections
3. Check server logs for errors
4. Verify email list quality
5. Monitor for rate limiting

---

🚀 Built for speed | 💪 Built for scale | 🛡️ Built to last
