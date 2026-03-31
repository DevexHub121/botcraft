# BotCraft Procfile & Deployment Guide

## Overview

This guide explains the **Procfile** and deployment process for the BotCraft application.

---

## Procfile Processes

### 1. **WEB Process** (Main Application)
```
web: gunicorn --workers=4 --worker-class=sync --timeout=120 --bind=0.0.0.0:$PORT app:app
```

**Responsibilities:**
- Handles all HTTP requests
- User authentication (JWT, Google OAuth)
- API endpoints for agents and widgets
- File uploads and serving
- Razorpay payment webhooks
- Admin and user dashboards

**Configuration:**
- **Workers**: 4 (handles concurrent requests)
- **Timeout**: 120 seconds (for long-running requests)
- **Bind**: 0.0.0.0:$PORT (listens on Heroku PORT variable)

**Expected Response Time**: < 2 seconds per request

---

### 2. **WORKER Process** (Background Tasks)
```
worker: python worker.py
```

**Responsibilities:**
- Email OTP delivery and notifications
- OpenAI API integration (async calls)
- File processing and validation
- Database maintenance tasks
- Log cleanup (runs every hour)
- OTP expiry cleanup (runs every 10 minutes)

**Benefits:**
- Non-blocking operations
- Independent scaling
- Prevents web process overload
- Reliable task execution

---

### 3. **RELEASE Process** (One-time Setup)
```
release: python -c "import app; print('Database initialized')" || true
```

**Responsibilities:**
- Database initialization on first deployment
- Runs once during deployment
- Creates tables if they don't exist

**When it Runs:**
- Only on new deployments
- Before scaling processes

---

## Deployment Platforms

### Heroku Deployment

#### Step 1: Create Heroku App
```bash
heroku create botcraft-app
```

#### Step 2: Set Environment Variables
```bash
heroku config:set SECRET_KEY=your-secret-key
heroku config:set OPENAI_API_KEY=your-openai-key
heroku config:set SMTP_EMAIL=your-email@gmail.com
heroku config:set SMTP_PASSWORD=your-app-password
heroku config:set RAZORPAY_KEY_ID=your-razorpay-key
heroku config:set RAZORPAY_KEY_SECRET=your-razorpay-secret
heroku config:set GOOGLE_CLIENT_ID=your-google-id
heroku config:set GOOGLE_CLIENT_SECRET=your-google-secret
heroku config:set ADMIN_EMAIL=admin@yourdomain.com
```

Or use the provided `.procfile.env` file:
```bash
heroku config:set --from-file .procfile.env
```

#### Step 3: Deploy
```bash
git push heroku main
```

#### Step 4: Scale Processes
```bash
# View current process types
heroku ps

# Scale web processes (usually 1 for free tier)
heroku ps:scale web=1

# Scale worker processes (optional, 1 is enough for most cases)
heroku ps:scale worker=1
```

#### Step 5: Monitor Logs
```bash
# View real-time logs
heroku logs --tail

# View specific process logs
heroku logs --dyno=worker -n 100
heroku logs --dyno=web -n 100
```

---

### Docker Deployment

#### Dockerfile Example
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Use the Procfile commands directly
CMD exec gunicorn --workers=4 --worker-class=sync --timeout=120 app:app
```

#### Docker Compose Example
```yaml
version: '3'
services:
  web:
    build: .
    ports:
      - "5000:5000"
    environment:
      - SECRET_KEY=your-secret-key
      - OPENAI_API_KEY=your-key
    volumes:
      - ./uploads:/app/uploads
      - ./db:/app/db
  
  worker:
    build: .
    command: python worker.py
    environment:
      - SECRET_KEY=your-secret-key
      - OPENAI_API_KEY=your-key
      - SMTP_SERVER=smtp.gmail.com
      - SMTP_PORT=587
    volumes:
      - ./db:/app/db
```

---

### Local Development

#### Run Web Server Locally
```bash
python app.py
# Or with Gunicorn
gunicorn app:app
```

#### Run Worker Locally
```bash
python worker.py
```

#### Both Processes with Foreman (Heroku Toolbelt)
```bash
pip install foreman
foreman start
# Reads Procfile and starts all processes
```

---

## Environment Variables Reference

| Variable | Purpose | Example |
|----------|---------|---------|
| `SECRET_KEY` | JWT encryption | `your-secret-key-123` |
| `OPENAI_API_KEY` | OpenAI authentication | `sk-...` |
| `SMTP_SERVER` | Email server | `smtp.gmail.com` |
| `SMTP_PORT` | Email port | `587` |
| `SMTP_EMAIL` | Email sender | `noreply@botcraft.com` |
| `SMTP_PASSWORD` | Email password | `app-specific-password` |
| `RAZORPAY_KEY_ID` | Payment gateway key | `rzp_test_...` |
| `RAZORPAY_KEY_SECRET` | Payment gateway secret | `...` |
| `GOOGLE_CLIENT_ID` | OAuth client ID | `...apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | OAuth secret | `...` |
| `ADMIN_EMAIL` | Admin email | `admin@botcraft.com` |
| `DATABASE_PATH` | SQLite database path | `db/chatbot.db` |

---

## Execution Data & Logging

### Web Process Logs
- HTTP request logs
- Error/exception traces
- Performance metrics
- Authentication failures

Example:
```
2026-03-31 10:15:23 [web.1] GET /api/agents 200 45ms
2026-03-31 10:15:24 [web.1] POST /auth/login 401 12ms
```

### Worker Process Logs
- Email delivery status
- Failed OTP sends
- Database cleanup results
- Health check results

Example:
```
2026-03-31 10:15:23 [worker.1] Email sent successfully to user@example.com
2026-03-31 10:16:00 [worker.1] Cleanup: Removed 5 expired OTPs
```

### View Logs
```bash
# All logs
heroku logs

# Real-time streaming
heroku logs --tail

# Last 50 lines
heroku logs -n 50

# Specific process
heroku logs --dyno=worker
heroku logs --dyno=web
```

---

## Performance Tuning

### Web Process
```
gunicorn --workers=4 \
         --worker-class=sync \
         --timeout=120 \
         --max-requests=1000 \
         --max-requests-jitter=100 \
         app:app
```

- **Workers**: 2 × CPU cores (for sync), or 1-4 for async
- **Timeout**: 120s for file uploads, 30s for API calls
- **Max Requests**: Prevents memory leaks (restart after N requests)

### Worker Process
- No special configuration needed
- Scales independently based on task backlog
- Consider multiple workers for high-volume tasks

---

## Troubleshooting

### Web Process Won't Start
```bash
# Check logs
heroku logs --dyno=web

# Check config variables
heroku config

# Restart process
heroku restart
```

### Worker Process Not Running Tasks
```bash
# Check worker logs
heroku logs --dyno=worker --tail

# Verify DATABASE_PATH exists
heroku run python -c "import sqlite3; print(sqlite3.connect('db/chatbot.db'))"

# Check environment variables
heroku config | grep DATABASE
```

### Email Not Sending
- Verify SMTP credentials
- Check if Gmail requires "Less secure app access" disabled
- Use app-specific password for Gmail
- Check worker process logs

### OTP Not Getting Cleaned Up
- Verify `logs` and `otps` tables exist
- Check worker process is running: `heroku ps`
- View worker logs: `heroku logs --dyno=worker`

---

## Production Checklist

- [ ] All environment variables set (`heroku config`)
- [ ] Secret key is unique and strong
- [ ] SMTP credentials are correct
- [ ] OAuth credentials configured
- [ ] Razorpay keys (production, not test)
- [ ] Admin email set
- [ ] Web process running (`heroku ps`)
- [ ] Worker process scaled appropriately
- [ ] Logs monitored for errors
- [ ] Database backed up before deployment
- [ ] SSL/HTTPS enabled (Heroku default)
- [ ] Error monitoring enabled (Sentry, etc.)

---

## Support

For more information:
- [Heroku Procfile Documentation](https://devcenter.heroku.com/articles/procfile)
- [Gunicorn Configuration](https://gunicorn.org/)
- [Flask Deployment](https://flask.palletsprojects.com/deployment/)
