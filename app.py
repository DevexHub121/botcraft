from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import hashlib
import jwt
import datetime
import os
import uuid
from functools import wraps
from openai import OpenAI
import json
import requests
import smtplib
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
import secrets

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)

# Configuration from environment variables
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
app.config['UPLOAD_FOLDER'] = 'uploads'
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# Email Configuration for OTP
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_EMAIL = os.environ.get('SMTP_EMAIL', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
OTP_EXPIRY_MINUTES = 5

# Create uploads folder
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Razorpay Configuration
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', 'rzp_test_YOUR_KEY_ID')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', 'YOUR_KEY_SECRET')

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI', 'https://botcraft.devexhub.com/auth/google/callback')

# Admin Configuration
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')

# Allow OAuth over HTTP for local development (disable in production)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Temporary storage for OAuth states (in production, use Redis or database)
oauth_states = {}

# Plan limits configuration
PLAN_LIMITS = {
    'free': {
        'agents': 1,
        'messages': 100,
        'files': 1,
        'file_size_mb': 2,
        'domains': 1,
        'models': ['gpt-4o-mini'],
        'custom_branding': False
    },
    'pro': {
        'agents': 4,
        'messages': 15000,
        'files': -1,  # Unlimited
        'file_size_mb': 10,
        'domains': 4,
        'models': ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo', 'gpt-3.5-turbo'],
        'custom_branding': True
    },
    'business': {
        'agents': 10,
        'messages': 40000,
        'files': -1,  # Unlimited
        'file_size_mb': 50,
        'domains': 8,
        'models': ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo', 'gpt-3.5-turbo', 'gpt-4', 'o1-mini', 'o1-preview'],
        'custom_branding': True
    }
}

# Plan prices in paise (for Razorpay) - Using INR to support UPI/QR
PLAN_PRICES = {
    'pro': 5 * 100,      # ₹500 (approx $5)
    'business': 10 * 100  # ₹1000 (approx $10)
}

# Database initialization
def init_db():
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Agents table
    c.execute('''CREATE TABLE IF NOT EXISTS agents
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  assistant_id TEXT,
                  prompt TEXT,
                  model TEXT DEFAULT 'gpt-4-turbo-preview',
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users(id))''')
    
    # Files table
    c.execute('''CREATE TABLE IF NOT EXISTS files
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_id INTEGER NOT NULL,
                  filename TEXT NOT NULL,
                  file_id TEXT NOT NULL,
                  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (agent_id) REFERENCES agents(id))''')
    
    # Conversations table
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_id INTEGER NOT NULL,
                  thread_id TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (agent_id) REFERENCES agents(id))''')
    
    # Messages table
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (conversation_id) REFERENCES conversations(id))''')
    
    # Webhooks table
    c.execute('''CREATE TABLE IF NOT EXISTS webhooks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_id INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  data TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (agent_id) REFERENCES agents(id))''')
    
    # User Knowledge Base Files table
    c.execute('''CREATE TABLE IF NOT EXISTS user_files
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  filename TEXT NOT NULL,
                  openai_file_id TEXT NOT NULL,
                  file_size INTEGER,
                  purpose TEXT DEFAULT 'assistants',
                  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users(id))''')
    
    # OTP codes table for email verification
    c.execute('''CREATE TABLE IF NOT EXISTS otp_codes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL,
                  otp TEXT NOT NULL,
                  purpose TEXT NOT NULL,
                  temp_data TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  expires_at TIMESTAMP NOT NULL,
                  used INTEGER DEFAULT 0)''')
    
    # Migration: Add allowed_domain and domain_key columns to agents table
    try:
        c.execute('ALTER TABLE agents ADD COLUMN allowed_domain TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    try:
        c.execute('ALTER TABLE agents ADD COLUMN domain_key TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    # Migration: Add allowed_domains column for multiple domains (JSON array)
    try:
        c.execute('ALTER TABLE agents ADD COLUMN allowed_domains TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    # Add checks for new columns in users table
    c.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in c.fetchall()]
    
    if 'plan' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    if 'message_count' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN message_count INTEGER DEFAULT 0")
    if 'message_reset_date' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN message_reset_date TIMESTAMP")
    if 'razorpay_customer_id' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN razorpay_customer_id TEXT")
    if 'razorpay_order_id' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN razorpay_order_id TEXT")
    if 'plan_expires_at' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN plan_expires_at TIMESTAMP")
    if 'plan_start_date' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN plan_start_date TIMESTAMP")
    if 'razorpay_subscription_id' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN razorpay_subscription_id TEXT")
    if 'phone' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if 'last_usage_alert_month' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN last_usage_alert_month TEXT")
    
    # Notifications table
    c.execute('''CREATE TABLE IF NOT EXISTS notifications
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  title TEXT NOT NULL,
                  message TEXT NOT NULL,
                  type TEXT DEFAULT 'info',
                  is_read INTEGER DEFAULT 0,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users(id))''')
    
    conn.commit()
    conn.close()

init_db()

# Generate 6-digit OTP
def generate_otp():
    return str(random.randint(100000, 999999))

# Send OTP email
def send_otp_email(to_email, otp, purpose='login'):
    try:
        subject = 'Your OTP for AI Chatbot Builder'
        
        if purpose == 'signup':
            body = f'''
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h2 style="color: #667eea;">Welcome to AI Chatbot Builder!</h2>
                <p>Thank you for signing up. Please use the following OTP to complete your registration:</p>
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            padding: 20px; 
                            border-radius: 10px; 
                            text-align: center; 
                            margin: 20px 0;">
                    <h1 style="color: white; margin: 0; letter-spacing: 8px;">{otp}</h1>
                </div>
                <p>This OTP is valid for {OTP_EXPIRY_MINUTES} minutes.</p>
                <p style="color: #666;">If you didn't request this, please ignore this email.</p>
            </body>
            </html>
            '''
        else:
            body = f'''
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h2 style="color: #667eea;">Login Verification</h2>
                <p>Please use the following OTP to complete your login:</p>
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            padding: 20px; 
                            border-radius: 10px; 
                            text-align: center; 
                            margin: 20px 0;">
                    <h1 style="color: white; margin: 0; letter-spacing: 8px;">{otp}</h1>
                </div>
                <p>This OTP is valid for {OTP_EXPIRY_MINUTES} minutes.</p>
                <p style="color: #666;">If you didn't request this, please ignore this email.</p>
            </body>
            </html>
            '''
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = to_email
        
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        
        return True
    except Exception as e:
        print(f"Email sending error: {e}")
        return False

# Send plan expiry reminder email
def send_expiry_reminder_email(to_email, user_name, plan, days_left):
    try:
        if days_left <= 0:
            subject = f"⚠️ Your {plan.title()} Plan Has Expired - AI Chatbot Builder"
            status_message = "has expired"
            action_message = "Renew now to continue using premium features!"
            urgency_color = "#dc3545"
        elif days_left == 1:
            subject = f"⏰ Last Day! Your {plan.title()} Plan Expires Tomorrow - AI Chatbot Builder"
            status_message = "expires tomorrow"
            action_message = "Renew today to avoid any interruption in service!"
            urgency_color = "#fd7e14"
        else:
            subject = f"📅 Your {plan.title()} Plan Expires in {days_left} Days - AI Chatbot Builder"
            status_message = f"expires in {days_left} days"
            action_message = "Consider renewing to continue enjoying premium features."
            urgency_color = "#ffc107"
        
        body = f'''
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="text-align: center; margin-bottom: 20px;">
                <h1 style="color: #667eea; margin: 0;">AI Chatbot Builder</h1>
            </div>
            <h2 style="color: #333;">Hi {user_name},</h2>
            <p>Your <strong>{plan.title()}</strong> plan {status_message}.</p>
            <div style="background: {urgency_color}; 
                        padding: 20px; 
                        border-radius: 10px; 
                        text-align: center; 
                        margin: 20px 0;">
                <h2 style="color: white; margin: 0;">{action_message}</h2>
            </div>
            <p>What you'll lose without renewal:</p>
            <ul>
                <li>Extra AI Agents</li>
                <li>Increased message limits</li>
                <li>Custom branding</li>
                <li>Priority support</li>
            </ul>
            <div style="text-align: center; margin: 30px 0;">
                <a href="http://localhost:5000/dashboard.html" 
                   style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                          color: white; 
                          padding: 15px 30px; 
                          text-decoration: none; 
                          border-radius: 8px; 
                          font-weight: bold;">
                    Renew Your Plan
                </a>
            </div>
            <p style="color: #666;">Thank you for using AI Chatbot Builder!</p>
        </body>
        </html>
        '''
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = to_email
        
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        
        print(f"Expiry reminder email sent to {to_email}")
        return True
    except Exception as e:
        print(f"Expiry email error: {e}")
        return False

# Create a notification (dashboard + optional email)
def create_notification(user_id, title, message, type='info', send_email=True):
    try:
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        
        # Save to DB for dashboard
        c.execute('''INSERT INTO notifications (user_id, title, message, type)
                     VALUES (?, ?, ?, ?)''', (user_id, title, message, type))
        conn.commit()
        
        # Send Email if requested
        if send_email:
            c.execute('SELECT email, name FROM users WHERE id = ?', (user_id,))
            user = c.fetchone()
            if user:
                email, name = user[0], user[1]
                
                # Basic email body for general notifications
                subject = f"🔔 Notification: {title}"
                body = f'''
                <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h2 style="color: #667eea;">{title}</h2>
                    <p>Hi {name},</p>
                    <p>{message}</p>
                    <div style="margin-top: 20px;">
                        <a href="http://localhost:5000/dashboard.html" 
                           style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                                  color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
                            Go to Dashboard
                        </a>
                    </div>
                </body>
                </html>
                '''
                
                msg = MIMEMultipart('alternative')
                msg['Subject'] = subject
                msg['From'] = SMTP_EMAIL
                msg['To'] = email
                msg.attach(MIMEText(body, 'html'))
                
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                    server.starttls()
                    server.login(SMTP_EMAIL, SMTP_PASSWORD)
                    server.sendmail(SMTP_EMAIL, email, msg.as_string())
        
        conn.close()
        return True
    except Exception as e:
        print(f"Error creating notification: {e}")
        return False

# Get user's current plan and usage
def get_user_plan(user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('''SELECT plan, message_count, message_reset_date, plan_expires_at 
                 FROM users WHERE id = ?''', (user_id,))
    result = c.fetchone()
    
    # Count user's agents
    c.execute('SELECT COUNT(*) FROM agents WHERE user_id = ?', (user_id,))
    agent_count = c.fetchone()[0]
    
    # Count user's files
    c.execute('SELECT COUNT(*) FROM user_files WHERE user_id = ?', (user_id,))
    file_count = c.fetchone()[0]
    
    conn.close()
    
    if result:
        plan = result[0] or 'free'
        message_count = result[1] or 0
        plan_expires_at = result[3]
        
        # Check for plan expiration
        if plan != 'free' and plan_expires_at:
            try:
                expires_date = datetime.datetime.fromisoformat(plan_expires_at)
                if datetime.datetime.now() > expires_date:
                    # Plan expired, revert to free
                    plan = 'free'
                    
                    # Optional: Update DB to reflect downgrade immediately
                    # c = conn.cursor()
                    # c.execute("UPDATE users SET plan = 'free' WHERE id = ?", (user_id,))
                    # conn.commit()
            except Exception as e:
                print(f"Error checking plan expiry: {e}")
                
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])
        
        return {
            'plan': plan,
            'message_count': message_count,
            'message_limit': limits['messages'],
            'agent_count': agent_count,
            'agent_limit': limits['agents'],
            'file_count': file_count,
            'file_limit': limits['files'],
            'domain_limit': limits['domains'],
            'custom_branding': limits['custom_branding'],
            'allowed_models': limits['models'],
            'plan_expires_at': plan_expires_at
        }
    return None

# Check if user can create more agents
def check_agent_limit(user_id):
    info = get_user_plan(user_id)
    if not info:
        return False, "User not found"
    
    if info['agent_count'] >= info['agent_limit']:
        return False, f"Agent limit reached ({info['agent_limit']}). Upgrade your plan for more agents."
    return True, "OK"

# Check if user has messages left
def check_message_limit(user_id):
    # First reset if needed
    reset_message_count_if_needed(user_id)
    
    info = get_user_plan(user_id)
    if not info:
        return False, "User not found"
    
    if info['message_count'] >= info['message_limit']:
        return False, f"Message limit reached ({info['message_limit']}/month). Upgrade your plan for more messages."
    return True, "OK"

# Increment message count
def increment_message_count(user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET message_count = COALESCE(message_count, 0) + 1 WHERE id = ?', (user_id,))
    conn.commit()
    
    # Check for 90% usage alert
    c.execute('SELECT plan, message_count, last_usage_alert_month FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    if user:
        plan, count, last_alert_month = user[0], user[1], user[2]
        limits = PLAN_LIMITS.get(plan or 'free', PLAN_LIMITS['free'])
        limit = limits['messages']
        
        current_month = datetime.datetime.now().strftime('%Y-%m')
        if count >= (limit * 0.9) and last_alert_month != current_month:
            create_notification(
                user_id, 
                "⚠️ High Usage Alert", 
                f"You have used 90% of your monthly message limit ({count}/{limit}).", 
                'warning'
            )
            c.execute('UPDATE users SET last_usage_alert_month = ? WHERE id = ?', (current_month, user_id))
            conn.commit()
            
    conn.close()

# Reset message count if new month
def reset_message_count_if_needed(user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT message_reset_date FROM users WHERE id = ?', (user_id,))
    result = c.fetchone()
    
    now = datetime.datetime.now()
    should_reset = False
    
    if result and result[0]:
        try:
            reset_date = datetime.datetime.fromisoformat(result[0])
            if now >= reset_date:
                should_reset = True
        except:
            should_reset = True
    else:
        should_reset = True
    
    if should_reset:
        # Set next reset date to 1st of next month
        if now.month == 12:
            next_reset = datetime.datetime(now.year + 1, 1, 1)
        else:
            next_reset = datetime.datetime(now.year, now.month + 1, 1)
        
        c.execute('''UPDATE users SET message_count = 0, message_reset_date = ? WHERE id = ?''',
                  (next_reset.isoformat(), user_id))
        conn.commit()
    
    conn.close()

# Authentication decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        
        try:
            if token.startswith('Bearer '):
                token = token[7:]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user_id = data['user_id']
        except:
            return jsonify({'error': 'Token is invalid'}), 401
        
        return f(current_user_id, *args, **kwargs)
    
    return decorated

# Hash password
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# Serve static files
@app.route('/')
def index():
    return send_from_directory('home', 'index.html')

# Get available OpenAI models
@app.route('/api/models', methods=['GET'])
@token_required
def get_models(current_user_id):
    try:
        models = client.models.list()
        # Filter to show only GPT models suitable for assistants
        gpt_models = []
        for model in models.data:
            model_id = model.id
            # Include GPT-4, GPT-3.5, and O1 models
            if any(x in model_id.lower() for x in ['gpt-4', 'gpt-3.5', 'o1', 'gpt-4o']):
                gpt_models.append({
                    'id': model_id,
                    'name': model_id
                })
        
        # Sort by model name
        gpt_models.sort(key=lambda x: x['id'], reverse=True)
        
        return jsonify({'models': gpt_models}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@app.route('/dashboard.html')
def dashboard():
    return send_from_directory('user', 'dashboard.html')

@app.route('/admin/dashboard')
@app.route('/admin_dashboard.html')
def admin_dashboard():
    return send_from_directory('admin', 'dashboard.html')

@app.route('/admin/dashboard.css')
@app.route('/admin_dashboard.css')
def admin_dashboard_css():
    return send_from_directory('admin', 'dashboard.css')

@app.route('/widget.html')
def widget():
    return send_from_directory('user', 'widget.html')

@app.route('/user/dashboard.css')
@app.route('/dashboard.css')
def dashboard_css():
    return send_from_directory('user', 'dashboard.css')

@app.route('/index.css')
def index_css():
    return send_from_directory('home', 'index.css')

@app.route('/privacy.html')
def privacy_page():
    return send_from_directory('home', 'privacy.html')

@app.route('/privacy')
def privacy_redirect():
    return send_from_directory('home', 'privacy.html')

@app.route('/terms.html')
def terms_page():
    return send_from_directory('home', 'terms.html')

@app.route('/terms')
def terms_redirect():
    return send_from_directory('home', 'terms.html')

@app.route('/about.html')
def about_page():
    return send_from_directory('home', 'about.html')

@app.route('/about')
def about_redirect():
    return send_from_directory('home', 'about.html')

@app.route('/logo.png')
def logo():
    return send_from_directory('home', 'logo.png')

@app.route('/Botcraft_fav.jpg')
@app.route('/favicon.ico')
def favicon():
    return send_from_directory('home', 'Botcraft_fav.jpg')

@app.route('/infovideo.mp4')
def info_video():
    return send_from_directory('home', 'infovideo.mp4')

# ==================== RAZORPAY / SUBSCRIPTION ENDPOINTS ====================

# Get current user plan and usage
@app.route('/api/user/plan', methods=['GET'])
@token_required
def get_plan(current_user_id):
    plan_info = get_user_plan(current_user_id)
    if not plan_info:
        return jsonify({'error': 'User not found'}), 404
    
    # Check if plan is expiring and send email reminder (only once per day)
    if plan_info.get('plan') != 'free' and plan_info.get('plan_expires_at'):
        try:
            expires_at = datetime.datetime.fromisoformat(plan_info['plan_expires_at'])
            days_left = (expires_at - datetime.datetime.now()).days
            
            # Send email for 3 days, 1 day, or 0 days left
            if days_left in [3, 1, 0]:
                # Get user info for email
                conn = sqlite3.connect('db/chatbot.db')
                c = conn.cursor()
                c.execute('SELECT email, name, last_expiry_email_date FROM users WHERE id = ?', (current_user_id,))
                user = c.fetchone()
                
                if user:
                    email, name = user[0], user[1]
                    last_email_date = user[2] if len(user) > 2 else None
                    today = datetime.datetime.now().strftime('%Y-%m-%d')
                    
                    # Only send one email/notification per day
                    if last_email_date != today:
                        # Email
                        send_expiry_reminder_email(email, name, plan_info['plan'], days_left)
                        
                        # Dashboard Notification
                        title = "⏰ Plan Expiry Reminder"
                        if days_left == 0:
                            msg = f"Your {plan_info['plan']} plan expires today! Renew now to keep your premium features."
                        else:
                            msg = f"Your {plan_info['plan']} plan expires in {days_left} day{'s' if days_left > 1 else ''}."
                        
                        create_notification(current_user_id, title, msg, 'warning', send_email=False)
                        
                        # Update last email date
                        c.execute('UPDATE users SET last_expiry_email_date = ? WHERE id = ?', (today, current_user_id))
                        conn.commit()
                
                conn.close()
        except Exception as e:
            print(f"Expiry check error: {e}")
    
    return jsonify(plan_info), 200

# Create Razorpay order for upgrade
@app.route('/api/create-order', methods=['POST'])
@token_required
def create_order(current_user_id):
    data = request.json
    plan = data.get('plan')  # 'pro' or 'business'
    
    if plan not in PLAN_PRICES:
        return jsonify({'error': 'Invalid plan'}), 400
    
    amount = PLAN_PRICES[plan]
    
    try:
        # Create Razorpay order using API
        import base64
        auth_string = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()
        
        order_data = {
            "amount": amount,
            "currency": "USD",
            "receipt": f"order_{current_user_id}_{plan}_{datetime.datetime.now().timestamp()}",
            "notes": {
                "user_id": str(current_user_id),
                "plan": plan
            }
        }
        
        print(f"Creating Razorpay order: {order_data}")
        print(f"Using Key ID: {RAZORPAY_KEY_ID}")
        
        response = requests.post(
            'https://api.razorpay.com/v1/orders',
            json=order_data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Basic {auth_bytes}'
            }
        )
        
        print(f"Razorpay response status: {response.status_code}")
        print(f"Razorpay response: {response.text}")
        
        if response.status_code == 200:
            order = response.json()
            
            # Save order ID to user
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            c.execute('UPDATE users SET razorpay_order_id = ? WHERE id = ?',
                      (order['id'], current_user_id))
            conn.commit()
            conn.close()
            
            return jsonify({
                'order_id': order['id'],
                'amount': amount,
                'currency': 'USD',
                'key_id': RAZORPAY_KEY_ID,
                'plan': plan
            }), 200
        else:
            return jsonify({'error': 'Failed to create order', 'details': response.text}), 500
            
    except Exception as e:
        print(f"Create order error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Create Razorpay subscription for recurring payments (AUTOPAY)
@app.route('/api/create-subscription', methods=['POST'])
@token_required
def create_subscription(current_user_id):
    data = request.json
    plan = data.get('plan')  # 'pro' or 'business'
    
    if plan not in RAZORPAY_PLAN_IDS:
        return jsonify({'error': 'Invalid plan'}), 400
    
    plan_id = RAZORPAY_PLAN_IDS[plan]
    
    # Check if plan_id is still placeholder
    if 'REPLACE' in plan_id:
        return jsonify({'error': 'Razorpay Plan IDs not configured. Please update in .env file.'}), 500
    
    try:
        import base64
        auth_string = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()
        
        # Get user email for subscription
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
        user = c.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        user_email = user[0]
        
        # Create subscription
        subscription_data = {
            "plan_id": plan_id,
            "total_count": 12,  # 12 billing cycles (1 year)
            "quantity": 1,
            "customer_notify": 1,
            "notes": {
                "user_id": str(current_user_id),
                "plan": plan
            }
        }
        
        print(f"Creating Razorpay subscription: {subscription_data}")
        
        response = requests.post(
            'https://api.razorpay.com/v1/subscriptions',
            json=subscription_data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Basic {auth_bytes}'
            }
        )
        
        print(f"Razorpay subscription response: {response.status_code} - {response.text}")
        
        if response.status_code == 200:
            subscription = response.json()
            
            return jsonify({
                'subscription_id': subscription['id'],
                'key_id': RAZORPAY_KEY_ID,
                'plan': plan,
                'amount': PLAN_PRICES[plan],
                'subscription_status': subscription.get('status', 'created')
            }), 200
        else:
            return jsonify({'error': 'Failed to create subscription', 'details': response.text}), 500
            
    except Exception as e:
        print(f"Create subscription error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# Verify Razorpay payment and upgrade plan
@app.route('/api/verify-payment', methods=['POST'])
@token_required
def verify_payment(current_user_id):
    data = request.json
    razorpay_order_id = data.get('razorpay_order_id')
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_signature = data.get('razorpay_signature')
    plan = data.get('plan')
    
    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, plan]):
        return jsonify({'error': 'Missing payment details'}), 400
    
    try:
        # Verify signature
        import hmac
        import hashlib
        
        message = f"{razorpay_order_id}|{razorpay_payment_id}"
        generated_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if generated_signature != razorpay_signature:
            return jsonify({'error': 'Invalid signature'}), 400
        
        # Upgrade user plan
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        
        # Get user details
        c.execute('SELECT name, email FROM users WHERE id = ?', (current_user_id,))
        user_result = c.fetchone()
        user_name = user_result[0] if user_result else 'Unknown'
        user_email = user_result[1] if user_result else 'Unknown'
        
        # Set plan expiry to 30 days from now
        start_date = datetime.datetime.now().isoformat()
        expires_at = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
        
        c.execute('''UPDATE users SET plan = ?, plan_expires_at = ?, plan_start_date = ?,
                     razorpay_customer_id = ?, message_count = 0 WHERE id = ?''',
                  (plan, expires_at, start_date, razorpay_payment_id, current_user_id))
        
        # Save payment record
        amount = PLAN_PRICES.get(plan, 0)
        c.execute('''INSERT INTO payments (user_id, user_name, user_email, plan, amount, 
                     razorpay_order_id, razorpay_payment_id, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 'success')''',
                  (current_user_id, user_name, user_email, plan, amount, 
                   razorpay_order_id, razorpay_payment_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'Successfully upgraded to {plan} plan!',
            'plan': plan,
            'expires_at': expires_at
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Razorpay webhook (for payment and subscription events)
@app.route('/api/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    data = request.json
    event = data.get('event')
    
    print(f"Razorpay webhook received: {event}")
    
    # Handle one-time payment captured
    if event == 'payment.captured':
        payload = data.get('payload', {}).get('payment', {}).get('entity', {})
        notes = payload.get('notes', {})
        user_id = notes.get('user_id')
        plan = notes.get('plan')
        
        if user_id and plan:
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            start_date = datetime.datetime.now().isoformat()
            expires_at = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            c.execute('UPDATE users SET plan = ?, plan_expires_at = ?, plan_start_date = ? WHERE id = ?',
                      (plan, expires_at, start_date, user_id))
            conn.commit()
            conn.close()
            print(f"User {user_id} upgraded to {plan} via one-time payment")
    
    # Handle subscription activated (first payment successful)
    elif event == 'subscription.activated':
        payload = data.get('payload', {}).get('subscription', {}).get('entity', {})
        subscription_id = payload.get('id')
        notes = payload.get('notes', {})
        user_id = notes.get('user_id')
        plan = notes.get('plan')
        
        if user_id and plan:
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            start_date = datetime.datetime.now().isoformat()
            # Subscription auto-renews, set expiry far in future (will update each charge)
            expires_at = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            c.execute('''UPDATE users SET plan = ?, plan_expires_at = ?, plan_start_date = ?, 
                        razorpay_subscription_id = ? WHERE id = ?''',
                      (plan, expires_at, start_date, subscription_id, user_id))
            conn.commit()
            conn.close()
            print(f"User {user_id} subscription {subscription_id} activated for {plan}")
    
    # Handle subscription charged (monthly renewal)
    elif event == 'subscription.charged':
        payload = data.get('payload', {}).get('subscription', {}).get('entity', {})
        subscription_id = payload.get('id')
        notes = payload.get('notes', {})
        user_id = notes.get('user_id')
        plan = notes.get('plan')
        
        if user_id:
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            # Extend plan for another 30 days
            expires_at = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            c.execute('UPDATE users SET plan_expires_at = ?, message_count = 0 WHERE id = ?',
                      (expires_at, user_id))
            conn.commit()
            conn.close()
            print(f"User {user_id} subscription renewed, plan extended")
    
    # Handle subscription cancelled
    elif event == 'subscription.cancelled':
        payload = data.get('payload', {}).get('subscription', {}).get('entity', {})
        notes = payload.get('notes', {})
        user_id = notes.get('user_id')
        
        if user_id:
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            # Plan will expire at current expiry date, then revert to free
            c.execute('UPDATE users SET razorpay_subscription_id = NULL WHERE id = ?', (user_id,))
            conn.commit()
            conn.close()
            print(f"User {user_id} subscription cancelled")
    
    return jsonify({'status': 'ok'}), 200

# Cancel subscription endpoint
@app.route('/api/cancel-subscription', methods=['POST'])
@token_required
def cancel_subscription(current_user_id):
    try:
        # Get user's subscription ID
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        c.execute('SELECT razorpay_subscription_id FROM users WHERE id = ?', (current_user_id,))
        result = c.fetchone()
        conn.close()
        
        if not result or not result[0]:
            return jsonify({'error': 'No active subscription found'}), 404
        
        subscription_id = result[0]
        
        import base64
        auth_string = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()
        
        # Cancel subscription via Razorpay API
        response = requests.post(
            f'https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel',
            json={"cancel_at_cycle_end": 1},  # Cancel at end of billing cycle
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Basic {auth_bytes}'
            }
        )
        
        if response.status_code == 200:
            # Clear subscription ID from user
            conn = sqlite3.connect('db/chatbot.db')
            c = conn.cursor()
            c.execute('UPDATE users SET razorpay_subscription_id = NULL WHERE id = ?', (current_user_id,))
            conn.commit()
            conn.close()
            
            return jsonify({'message': 'Subscription cancelled. Plan will remain active until expiry.'}), 200
        else:
            return jsonify({'error': 'Failed to cancel subscription', 'details': response.text}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== END RAZORPAY ENDPOINTS ====================


# Register endpoint
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    
    if not name or not email or not password:
        return jsonify({'error': 'All fields are required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    try:
        hashed_password = hash_password(password)
        c.execute('INSERT INTO users (name, email, password) VALUES (?, ?, ?)',
                  (name, email, hashed_password))
        conn.commit()
        user_id = c.lastrowid
        
        token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        
        conn.close()
        
        return jsonify({
            'message': 'Registration successful',
            'token': token,
            'user': {'id': user_id, 'name': name, 'email': email}
        }), 201
        
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Email already exists'}), 400

# Login endpoint
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    hashed_password = hash_password(password)
    c.execute('SELECT id, name, email FROM users WHERE email = ? AND password = ?',
              (email, hashed_password))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({'error': 'Invalid credentials'}), 401
    
    token = jwt.encode({
        'user_id': user[0],
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    
    return jsonify({
        'message': 'Login successful',
        'token': token,
        'user': {'id': user[0], 'name': user[1], 'email': user[2]}
    }), 200

# Send OTP endpoint - Step 1 of authentication
@app.route('/api/send-otp', methods=['POST'])
def send_otp():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')  # Only for signup
    phone = data.get('phone')  # Only for signup
    purpose = data.get('purpose', 'login')  # 'login' or 'signup'
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    hashed_password = hash_password(password)
    
    if purpose == 'signup':
        # Check if email already exists for signup
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        if c.fetchone():
            conn.close()
            return jsonify({'error': 'Email already exists'}), 400
        
        if not name:
            conn.close()
            return jsonify({'error': 'Name is required for signup'}), 400
        
        # Store signup data temporarily
        temp_data = json.dumps({'name': name, 'email': email, 'password': hashed_password, 'phone': phone})
    else:
        # Verify credentials for login
        c.execute('SELECT id, name, email FROM users WHERE email = ? AND password = ?',
                  (email, hashed_password))
        user = c.fetchone()
        
        if not user:
            conn.close()
            return jsonify({'error': 'Invalid credentials'}), 401
        
        temp_data = json.dumps({'user_id': user[0], 'name': user[1], 'email': user[2]})
    
    # Generate OTP
    otp = generate_otp()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_EXPIRY_MINUTES)
    
    # Invalidate any existing OTPs for this email
    c.execute('UPDATE otp_codes SET used = 1 WHERE email = ? AND used = 0', (email,))
    
    # Store new OTP
    c.execute('''INSERT INTO otp_codes (email, otp, purpose, temp_data, expires_at) 
                 VALUES (?, ?, ?, ?, ?)''',
              (email, otp, purpose, temp_data, expires_at))
    conn.commit()
    conn.close()
    
    # Send OTP email
    if send_otp_email(email, otp, purpose):
        return jsonify({
            'message': 'OTP sent to your email',
            'email': email,
            'purpose': purpose
        }), 200
    else:
        return jsonify({'error': 'Failed to send OTP. Please check email configuration.'}), 500

        return jsonify({'error': 'Failed to send OTP. Please check email configuration.'}), 500

# Helper function to send welcome email
def send_welcome_email(email, name):
    try:
        subject = 'Welcome to AI Chatbot Builder! 🚀'
        body = f'''
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 30px;">
                <h2 style="color: #667eea; margin-top: 0;">Welcome aboard, {name}! 🎉</h2>
                <p>We're thrilled to have you join <strong>AI Chatbot Builder</strong>.</p>
                
                <p>You're now ready to create intelligent custom AI agents that can automate your customer support, lead gen, and more.</p>
                
                <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <h3 style="margin-top: 0; font-size: 18px;">🚀 Quick Start Guide:</h3>
                    <ol style="padding-left: 20px;">
                        <li style="margin-bottom: 10px;"><strong>Create an Agent:</strong> Give it a name and choose a model (GPT-4o).</li>
                        <li style="margin-bottom: 10px;"><strong>Train it:</strong> Upload your PDFs or docs in the Knowledge Base tab.</li>
                        <li style="margin-bottom: 10px;"><strong>Embed it:</strong> Copy the one-line code snippet to your website.</li>
                    </ol>
                </div>
                
                <p>If you need any help, just reply to this email.</p>
                
                <div style="text-align: center; margin-top: 30px;">
                    <a href="http://localhost:8000" style="background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">Go to Dashboard</a>
                </div>
                
                <p style="margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; color: #777; font-size: 12px;">
                    © 2024 AI Chatbot Builder. All rights reserved.
                </p>
            </div>
        </body>
        </html>
        '''
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = email
        
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, email, msg.as_string())
            
        print(f"Welcome email sent to {email}")
        return True
    except Exception as e:
        print(f"Failed to send welcome email: {e}")
        return False

# Verify OTP endpoint - Step 2 of authentication
@app.route('/api/verify-otp', methods=['POST'])
def verify_otp():
    data = request.json
    email = data.get('email')
    otp = data.get('otp')
    purpose = data.get('purpose', 'login')
    
    if not email or not otp:
        return jsonify({'error': 'Email and OTP are required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get the latest unused OTP for this email
    c.execute('''SELECT id, otp, temp_data, expires_at FROM otp_codes 
                 WHERE email = ? AND purpose = ? AND used = 0 
                 ORDER BY created_at DESC LIMIT 1''', (email, purpose))
    otp_record = c.fetchone()
    
    if not otp_record:
        conn.close()
        return jsonify({'error': 'No valid OTP found. Please request a new one.'}), 400
    
    otp_id, stored_otp, temp_data, expires_at = otp_record
    
    # Check if OTP is expired
    expires_at_dt = datetime.datetime.fromisoformat(expires_at)
    if datetime.datetime.utcnow() > expires_at_dt:
        c.execute('UPDATE otp_codes SET used = 1 WHERE id = ?', (otp_id,))
        conn.commit()
        conn.close()
        return jsonify({'error': 'OTP has expired. Please request a new one.'}), 400
    
    # Verify OTP
    if otp != stored_otp:
        conn.close()
        return jsonify({'error': 'Invalid OTP'}), 400
    
    # Mark OTP as used
    c.execute('UPDATE otp_codes SET used = 1 WHERE id = ?', (otp_id,))
    conn.commit()
    
    user_data = json.loads(temp_data)
    
    if purpose == 'signup':
        # Create new user
        try:
            c.execute('INSERT INTO users (name, email, password, phone) VALUES (?, ?, ?, ?)',
                      (user_data['name'], user_data['email'], user_data['password'], user_data.get('phone')))
            conn.commit()
            user_id = c.lastrowid
            
            # Send welcome email
            send_welcome_email(user_data['email'], user_data['name'])
            
            token = jwt.encode({
                'user_id': user_id,
                'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
            }, app.config['SECRET_KEY'], algorithm='HS256')
            
            conn.close()
            
            return jsonify({
                'message': 'Registration successful',
                'token': token,
                'user': {'id': user_id, 'name': user_data['name'], 'email': user_data['email']}
            }), 201
            
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'error': 'Email already exists'}), 400
    else:
        # Login user
        token = jwt.encode({
            'user_id': user_data['user_id'],
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        
        conn.close()
        
        return jsonify({
            'message': 'Login successful',
            'token': token,
            'user': {'id': user_data['user_id'], 'name': user_data['name'], 'email': user_data['email']}
        }), 200

# Resend OTP endpoint
@app.route('/api/resend-otp', methods=['POST'])
def resend_otp():
    data = request.json
    email = data.get('email')
    purpose = data.get('purpose', 'login')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get temp_data from existing OTP record
    c.execute('''SELECT temp_data FROM otp_codes 
                 WHERE email = ? AND purpose = ? 
                 ORDER BY created_at DESC LIMIT 1''', (email, purpose))
    record = c.fetchone()
    
    if not record:
        conn.close()
        return jsonify({'error': 'No pending verification found'}), 400
    
    temp_data = record[0]
    
    # Generate new OTP
    otp = generate_otp()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_EXPIRY_MINUTES)
    
    # Invalidate existing OTPs
    c.execute('UPDATE otp_codes SET used = 1 WHERE email = ? AND used = 0', (email,))
    
    # Store new OTP
    c.execute('''INSERT INTO otp_codes (email, otp, purpose, temp_data, expires_at) 
                 VALUES (?, ?, ?, ?, ?)''',
              (email, otp, purpose, temp_data, expires_at))
    conn.commit()
    conn.close()
    
    # Send OTP email
    if send_otp_email(email, otp, purpose):
        return jsonify({
            'message': 'New OTP sent to your email',
            'email': email
        }), 200
    else:
        return jsonify({'error': 'Failed to send OTP'}), 500

# Forgot Password - Step 1: Send OTP to email
@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Check if user exists
    c.execute('SELECT id, name FROM users WHERE email = ?', (email,))
    user = c.fetchone()
    
    if not user:
        conn.close()
        return jsonify({'error': 'No account found with this email'}), 404
    
    # Generate OTP
    otp = generate_otp()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_EXPIRY_MINUTES)
    
    # Store temp data
    temp_data = json.dumps({'user_id': user[0], 'email': email})
    
    # Invalidate any existing OTPs for this email
    c.execute('UPDATE otp_codes SET used = 1 WHERE email = ? AND purpose = ? AND used = 0', 
              (email, 'forgot_password'))
    
    # Store new OTP
    c.execute('''INSERT INTO otp_codes (email, otp, purpose, temp_data, expires_at) 
                 VALUES (?, ?, ?, ?, ?)''',
              (email, otp, 'forgot_password', temp_data, expires_at))
    conn.commit()
    conn.close()
    
    # Send OTP email with forgot password template
    try:
        subject = 'Password Reset OTP - AI Chatbot Builder'
        body = f'''
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #667eea;">Password Reset Request</h2>
            <p>You have requested to reset your password. Use the following OTP to proceed:</p>
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                        padding: 20px; 
                        border-radius: 10px; 
                        text-align: center; 
                        margin: 20px 0;">
                <h1 style="color: white; margin: 0; letter-spacing: 8px;">{otp}</h1>
            </div>
            <p>This OTP is valid for {OTP_EXPIRY_MINUTES} minutes.</p>
            <p style="color: #666;">If you didn't request this, please ignore this email or contact support.</p>
        </body>
        </html>
        '''
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = email
        
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, email, msg.as_string())
        
        return jsonify({
            'message': 'OTP sent to your email',
            'email': email
        }), 200
    except Exception as e:
        print(f"Forgot password email error: {e}")
        return jsonify({'error': 'Failed to send OTP'}), 500

# Forgot Password - Step 2: Verify OTP
@app.route('/api/verify-forgot-otp', methods=['POST'])
def verify_forgot_otp():
    data = request.json
    email = data.get('email')
    otp = data.get('otp')
    
    if not email or not otp:
        return jsonify({'error': 'Email and OTP are required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get the latest unused OTP for forgot password
    c.execute('''SELECT id, otp, expires_at FROM otp_codes 
                 WHERE email = ? AND purpose = ? AND used = 0 
                 ORDER BY created_at DESC LIMIT 1''', (email, 'forgot_password'))
    otp_record = c.fetchone()
    
    if not otp_record:
        conn.close()
        return jsonify({'error': 'No valid OTP found. Please request a new one.'}), 400
    
    otp_id, stored_otp, expires_at = otp_record
    
    # Check if OTP is expired
    expires_at_dt = datetime.datetime.fromisoformat(expires_at)
    if datetime.datetime.utcnow() > expires_at_dt:
        c.execute('UPDATE otp_codes SET used = 1 WHERE id = ?', (otp_id,))
        conn.commit()
        conn.close()
        return jsonify({'error': 'OTP has expired. Please request a new one.'}), 400
    
    # Verify OTP
    if otp != stored_otp:
        conn.close()
        return jsonify({'error': 'Invalid OTP'}), 400
    
    conn.close()
    
    return jsonify({
        'message': 'OTP verified successfully',
        'email': email,
        'verified': True
    }), 200

# Forgot Password - Step 3: Reset Password
@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    email = data.get('email')
    otp = data.get('otp')
    new_password = data.get('new_password')
    
    if not email or not otp or not new_password:
        return jsonify({'error': 'Email, OTP and new password are required'}), 400
    
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Verify OTP again
    c.execute('''SELECT id, otp, expires_at FROM otp_codes 
                 WHERE email = ? AND purpose = ? AND used = 0 
                 ORDER BY created_at DESC LIMIT 1''', (email, 'forgot_password'))
    otp_record = c.fetchone()
    
    if not otp_record:
        conn.close()
        return jsonify({'error': 'Invalid or expired session. Please try again.'}), 400
    
    otp_id, stored_otp, expires_at = otp_record
    
    # Check OTP
    if otp != stored_otp:
        conn.close()
        return jsonify({'error': 'Invalid OTP'}), 400
    
    # Check expiry
    expires_at_dt = datetime.datetime.fromisoformat(expires_at)
    if datetime.datetime.utcnow() > expires_at_dt:
        conn.close()
        return jsonify({'error': 'Session expired. Please try again.'}), 400
    
    # Update password
    hashed_password = hash_password(new_password)
    c.execute('UPDATE users SET password = ? WHERE email = ?', (hashed_password, email))
    
    # Mark OTP as used
    c.execute('UPDATE otp_codes SET used = 1 WHERE id = ?', (otp_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'message': 'Password reset successful! You can now login with your new password.'
    }), 200

# ==================== GOOGLE OAUTH ROUTES ====================

# Google OAuth - Initiate authentication
@app.route('/auth/google', methods=['GET'])
def google_auth():
    try:
        # Create flow instance to manage OAuth 2.0 Authorization Grant Flow
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI]
                }
            },
            scopes=['https://www.googleapis.com/auth/userinfo.email',
                    'https://www.googleapis.com/auth/userinfo.profile',
                    'openid']
        )
        
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        
        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)
        oauth_states[state] = True
        
        # Get authorization URL
        authorization_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state
        )
        
        return jsonify({'url': authorization_url}), 200
        
    except Exception as e:
        print(f"Google OAuth error: {str(e)}")
        return jsonify({'error': 'Failed to initiate Google authentication'}), 500

# Google OAuth - Callback handler
@app.route('/auth/google/callback', methods=['GET'])
def google_callback():
    try:
        # Verify state to prevent CSRF
        state = request.args.get('state')
        if not state or state not in oauth_states:
            return f"""
                <html>
                    <body>
                        <h3 style="color: red;">Authentication failed: Invalid state</h3>
                        <p><a href="/">Return to login</a></p>
                    </body>
                </html>
            """, 400
        
        # Remove used state
        del oauth_states[state]
        
        # Create flow instance
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI]
                }
            },
            scopes=['https://www.googleapis.com/auth/userinfo.email',
                    'https://www.googleapis.com/auth/userinfo.profile',
                    'openid']
        )
        
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        
        # Exchange authorization code for tokens
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        
        # Get credentials
        credentials = flow.credentials
        
        # Get user info from Google
        user_info_response = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {credentials.token}'}
        )
        
        if user_info_response.status_code != 200:
            return f"""
                <html>
                    <body>
                        <h3 style="color: red;">Failed to fetch user information</h3>
                        <p><a href="/">Return to login</a></p>
                    </body>
                </html>
            """, 400
        
        user_info = user_info_response.json()
        google_id = user_info.get('id')
        email = user_info.get('email')
        name = user_info.get('name', email.split('@')[0])
        
        # Check if user exists
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        
        # First check if database schema supports Google auth
        c.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in c.fetchall()]
        
        # Add google_id and auth_provider columns if they don't exist
        if 'google_id' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN google_id TEXT')
        if 'auth_provider' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT "email"')
        conn.commit()
        
        # Check if user exists by email or google_id
        c.execute('SELECT id, name, email, google_id FROM users WHERE email = ? OR google_id = ?',
                  (email, google_id))
        existing_user = c.fetchone()
        
        if existing_user:
            # Update existing user with Google ID if not set
            user_id = existing_user[0]
            if not existing_user[3]:  # google_id not set
                c.execute('UPDATE users SET google_id = ?, auth_provider = ? WHERE id = ?',
                          (google_id, 'google', user_id))
                conn.commit()
        else:
            # Create new user
            c.execute('''INSERT INTO users (name, email, phone, password, google_id, auth_provider, plan, plan_start_date) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (name, email, '', None, google_id, 'google', 'free',
                       datetime.datetime.utcnow().isoformat()))
            conn.commit()
            user_id = c.lastrowid
            
            # Send welcome email
            send_welcome_email(email, name)
        
        conn.close()
        
        # Generate JWT token
        token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        
        # Return HTML that stores token and redirects
        return f"""
            <html>
                <head>
                    <title>Authentication Successful</title>
                </head>
                <body>
                    <h3>Authentication successful! Redirecting...</h3>
                    <script>
                        sessionStorage.setItem('token', '{token}');
                        sessionStorage.setItem('user', JSON.stringify({{
                            'id': {user_id},
                            'name': '{name}',
                            'email': '{email}'
                        }}));
                        window.location.href = '/';
                    </script>
                </body>
            </html>
        """
        
    except Exception as e:
        print(f"Google callback error: {str(e)}")
        return f"""
            <html>
                <body>
                    <h3 style="color: red;">Authentication error: {str(e)}</h3>
                    <p><a href="/">Return to login</a></p>
                </body>
            </html>
        """, 500


# Create agent endpoint
@app.route('/api/agents', methods=['POST'])
@token_required
def create_agent(current_user_id):
    # Check agent limit based on plan
    can_create, limit_error = check_agent_limit(current_user_id)
    if not can_create:
        return jsonify({'error': limit_error, 'upgrade_required': True}), 403
    
    data = request.json
    name = data.get('name')
    prompt = data.get('prompt')
    model = data.get('model', 'gpt-4o-mini')
    tools = data.get('tools', [{"type": "code_interpreter"}, {"type": "file_search"}])
    file_ids = data.get('file_ids', [])
    allowed_domain = data.get('allowed_domain', '')  # Domain restriction (required)
    
    if not name or not prompt:
        return jsonify({'error': 'Name and prompt are required'}), 400
    
    if not allowed_domain:
        return jsonify({'error': 'Allowed domain is required'}), 400
    
    # Generate unique domain key
    domain_key = str(uuid.uuid4())
    
    try:
        # Check if file_search is enabled and files are selected
        has_file_search = any(t.get('type') == 'file_search' for t in tools)
        vector_store_id = None
        
        if has_file_search and file_ids:
            # Create a vector store using direct API call
            headers = {
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
                'OpenAI-Beta': 'assistants=v2'
            }
            
            # Create vector store
            vs_response = requests.post(
                'https://api.openai.com/v1/vector_stores',
                headers=headers,
                json={'name': f"{name} - Knowledge Base"}
            )
            
            if vs_response.status_code == 200:
                vector_store_id = vs_response.json().get('id')
                
                # Add files to vector store
                for file_id in file_ids:
                    requests.post(
                        f'https://api.openai.com/v1/vector_stores/{vector_store_id}/files',
                        headers=headers,
                        json={'file_id': file_id}
                    )
        
        # Prepare tool_resources if we have a vector store
        tool_resources = None
        if vector_store_id:
            tool_resources = {
                "file_search": {
                    "vector_store_ids": [vector_store_id]
                }
            }
        
        # Create OpenAI Assistant (v2 API)
        assistant_params = {
            "name": name,
            "instructions": prompt,
            "model": model,
            "tools": tools
        }
        
        if tool_resources:
            assistant_params["tool_resources"] = tool_resources
        
        print(f"Creating OpenAI assistant with params: {assistant_params}")
        try:
            assistant = client.beta.assistants.create(**assistant_params)
            print(f"Assistant created successfully: {assistant.id}")
        except Exception as openai_error:
            print(f"OpenAI Error: {str(openai_error)}")
            return jsonify({'error': f'OpenAI Error: {str(openai_error)}'}), 500
        
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        c.execute('INSERT INTO agents (user_id, name, assistant_id, prompt, model, allowed_domain, domain_key) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  (current_user_id, name, assistant.id, prompt, model, allowed_domain, domain_key))
        conn.commit()
        agent_id = c.lastrowid
        conn.close()
        
        # Store webhook event
        store_webhook_event(agent_id, 'agent_created', {
            'agent_id': agent_id,
            'name': name,
            'assistant_id': assistant.id,
            'files_attached': len(file_ids),
            'allowed_domain': allowed_domain
        })
        
        return jsonify({
            'message': 'Agent created successfully' + (f' with {len(file_ids)} files attached' if file_ids else ''),
            'agent': {
                'id': agent_id,
                'name': name,
                'assistant_id': assistant.id,
                'prompt': prompt,
                'model': model,
                'allowed_domain': allowed_domain,
                'domain_key': domain_key
            }
        }), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Get agents endpoint
@app.route('/api/agents', methods=['GET'])
@token_required
def get_agents(current_user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT id, name, assistant_id, prompt, model, created_at FROM agents WHERE user_id = ?',
              (current_user_id,))
    agents = c.fetchall()
    conn.close()
    
    agents_list = []
    for agent in agents:
        agents_list.append({
            'id': agent[0],
            'name': agent[1],
            'assistant_id': agent[2],
            'prompt': agent[3],
            'model': agent[4],
            'created_at': agent[5]
        })
    
    return jsonify({'agents': agents_list}), 200

# Get single agent endpoint
@app.route('/api/agents/<int:agent_id>', methods=['GET'])
@token_required
def get_agent(current_user_id, agent_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT id, name, assistant_id, prompt, model, created_at, allowed_domain, domain_key, allowed_domains FROM agents WHERE id = ? AND user_id = ?',
              (agent_id, current_user_id))
    agent = c.fetchone()
    conn.close()
    
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    
    # Get allowed_domains from the new column or fallback to old allowed_domain
    allowed_domains_data = agent[8] or agent[6] or ''
    
    return jsonify({
        'agent': {
            'id': agent[0],
            'name': agent[1],
            'assistant_id': agent[2],
            'prompt': agent[3],
            'model': agent[4],
            'created_at': agent[5],
            'allowed_domain': agent[6] or '',  # Keep for backward compatibility
            'domain_key': agent[7] or '',
            'allowed_domains': allowed_domains_data  # New field for multiple domains
        }
    }), 200

# Update agent endpoint
@app.route('/api/agents/<int:agent_id>', methods=['PUT'])
@token_required
def update_agent(current_user_id, agent_id):
    data = request.get_json(force=True)

    name = data.get('name')
    prompt = data.get('prompt')
    file_ids = data.get('file_ids') or []
    allowed_domain = data.get('allowed_domain') or ''

    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()

    try:
        # Verify ownership
        c.execute(
            'SELECT assistant_id, domain_key FROM agents WHERE id = ? AND user_id = ?',
            (agent_id, current_user_id)
        )
        result = c.fetchone()

        if not result:
            return jsonify({'error': 'Agent not found'}), 404

        assistant_id, domain_key = result

        if not domain_key:
            domain_key = str(uuid.uuid4())

        # Update DB
        c.execute(
            '''
            UPDATE agents
            SET name = ?, prompt = ?, allowed_domain = ?, domain_key = ?
            WHERE id = ?
            ''',
            (name, prompt, allowed_domain, domain_key, agent_id)
        )
        conn.commit()

        # Fetch updated agent (IMPORTANT)
        c.execute(
            'SELECT id, name, prompt, allowed_domain, domain_key FROM agents WHERE id = ?',
            (agent_id,)
        )
        agent = c.fetchone()

    finally:
        conn.close()

    # Update OpenAI Assistant
    headers = {
        'Authorization': f'Bearer {OPENAI_API_KEY}',
        'Content-Type': 'application/json',
        'OpenAI-Beta': 'assistants=v2'
    }

    update_data = {
        'name': name,
        'instructions': prompt
    }

    # File search handling
    if file_ids:
        vs_response = requests.post(
            'https://api.openai.com/v1/vector_stores',
            headers=headers,
            json={'name': f'{name} - Knowledge Base'}
        )

        if vs_response.status_code != 200:
            return jsonify({'error': 'Failed to create vector store'}), 500

        vector_store_id = vs_response.json()['id']

        for file_id in file_ids:
            requests.post(
                f'https://api.openai.com/v1/vector_stores/{vector_store_id}/files',
                headers=headers,
                json={'file_id': file_id}
            )

        update_data['tool_resources'] = {
            'file_search': {
                'vector_store_ids': [vector_store_id]
            }
        }

    # PATCH assistant (NOT POST)
    assistant_res = requests.patch(
        f'https://api.openai.com/v1/assistants/{assistant_id}',
        headers=headers,
        json=update_data
    )

    if assistant_res.status_code != 200:
        return jsonify({'error': 'Failed to update assistant'}), 500

    # ✅ Final response frontend needs
    return jsonify({
        'message': 'Agent updated successfully',
        'agent': {
            'id': agent[0],
            'name': agent[1],
            'prompt': agent[2],
            'allowed_domain': agent[3],
            'domain_key': agent[4]
        }
    }), 200


# Delete agent endpoint
@app.route('/api/agents/<int:agent_id>', methods=['DELETE'])
@token_required
def delete_agent(current_user_id, agent_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get agent and verify ownership
    c.execute('SELECT assistant_id FROM agents WHERE id = ? AND user_id = ?',
              (agent_id, current_user_id))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return jsonify({'error': 'Agent not found'}), 404
    
    assistant_id = result[0]
    
    try:
        # Delete from OpenAI
        if assistant_id:
            client.beta.assistants.delete(assistant_id)
    except Exception as e:
        print(f"OpenAI delete error: {e}")
    
    # Delete related files, conversations, messages, and webhooks
    c.execute('DELETE FROM files WHERE agent_id = ?', (agent_id,))
    c.execute('DELETE FROM webhooks WHERE agent_id = ?', (agent_id,))
    c.execute('''DELETE FROM messages WHERE conversation_id IN 
                 (SELECT id FROM conversations WHERE agent_id = ?)''', (agent_id,))
    c.execute('DELETE FROM conversations WHERE agent_id = ?', (agent_id,))
    c.execute('DELETE FROM agents WHERE id = ?', (agent_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'message': 'Agent deleted successfully'}), 200

# Validate domain endpoint (for widget)
@app.route('/api/widget/validate', methods=['GET'])
def validate_widget_domain():
    agent_id = request.args.get('agent')
    domain_key = request.args.get('key')
    origin_domain = request.args.get('domain', '')
    
    if not agent_id or not domain_key:
        return jsonify({'valid': False, 'error': 'Missing agent or key'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT allowed_domain, domain_key FROM agents WHERE id = ?', (agent_id,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return jsonify({'valid': False, 'error': 'Agent not found'}), 404
    
    allowed_domain = result[0] or ''
    stored_key = result[1] or ''
    
    # Check domain key
    if domain_key != stored_key:
        return jsonify({'valid': False, 'error': 'Invalid domain key'}), 403
    
    # If no domain restriction set, allow all
    if not allowed_domain:
        return jsonify({'valid': True, 'message': 'No domain restriction'}), 200
    
    # Helper function to extract domain from URL
    def extract_domain(url):
        url = url.lower().strip()
        # Remove protocol
        url = url.replace('https://', '').replace('http://', '')
        # Remove www.
        url = url.replace('www.', '')
        # Get just the domain (before any path/query)
        domain = url.split('/')[0].split('?')[0].split('#')[0]
        return domain
    
    # Extract domains for comparison
    allowed_clean = extract_domain(allowed_domain)
    origin_clean = extract_domain(origin_domain)
    
    # Check if domain matches (exact match or subdomain)
    if origin_clean == allowed_clean or origin_clean.endswith('.' + allowed_clean):
        return jsonify({'valid': True, 'message': 'Domain validated'}), 200
    
    return jsonify({'valid': False, 'error': f'Widget not authorized for this domain. Allowed: {allowed_clean}'}), 403

# Upload file endpoint
@app.route('/api/agents/<int:agent_id>/files', methods=['POST'])
@token_required
def upload_file(current_user_id, agent_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    try:
        # Upload file to OpenAI
        uploaded_file = client.files.create(
            file=file,
            purpose='assistants'
        )
        
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        
        # Get assistant_id
        c.execute('SELECT assistant_id FROM agents WHERE id = ? AND user_id = ?',
                  (agent_id, current_user_id))
        result = c.fetchone()
        
        if not result:
            conn.close()
            return jsonify({'error': 'Agent not found'}), 404
        
        assistant_id = result[0]
        
        # Update assistant with file
        client.beta.assistants.update(
            assistant_id=assistant_id,
            tool_resources={"file_search": {"vector_store_ids": []}}
        )
        
        # Store file info in database
        c.execute('INSERT INTO files (agent_id, filename, file_id) VALUES (?, ?, ?)',
                  (agent_id, file.filename, uploaded_file.id))
        conn.commit()
        file_db_id = c.lastrowid
        conn.close()
        
        # Store webhook event
        store_webhook_event(agent_id, 'file_uploaded', {
            'filename': file.filename,
            'file_id': uploaded_file.id
        })
        
        return jsonify({
            'message': 'File uploaded successfully',
            'file': {
                'id': file_db_id,
                'filename': file.filename,
                'file_id': uploaded_file.id
            }
        }), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Get files for an agent
@app.route('/api/agents/<int:agent_id>/files', methods=['GET'])
@token_required
def get_files(current_user_id, agent_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('''SELECT f.id, f.filename, f.file_id, f.uploaded_at 
                 FROM files f
                 JOIN agents a ON f.agent_id = a.id
                 WHERE a.id = ? AND a.user_id = ?''',
              (agent_id, current_user_id))
    files = c.fetchall()
    conn.close()
    
    files_list = []
    for file in files:
        files_list.append({
            'id': file[0],
            'filename': file[1],
            'file_id': file[2],
            'uploaded_at': file[3]
        })
    
    return jsonify({'files': files_list}), 200

# Store webhook event
def store_webhook_event(agent_id, event_type, data):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('INSERT INTO webhooks (agent_id, event_type, data) VALUES (?, ?, ?)',
              (agent_id, event_type, json.dumps(data)))
    conn.commit()
    conn.close()

# Get webhooks
@app.route('/api/webhooks/<int:agent_id>', methods=['GET'])
@token_required
def get_webhooks(current_user_id, agent_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('''SELECT w.id, w.event_type, w.data, w.created_at
                 FROM webhooks w
                 JOIN agents a ON w.agent_id = a.id
                 WHERE a.id = ? AND a.user_id = ?
                 ORDER BY w.created_at DESC
                 LIMIT 100''',
              (agent_id, current_user_id))
    webhooks = c.fetchall()
    conn.close()
    
    webhooks_list = []
    for webhook in webhooks:
        webhooks_list.append({
            'id': webhook[0],
            'event_type': webhook[1],
            'data': json.loads(webhook[2]),
            'created_at': webhook[3]
        })
    
    return jsonify({'webhooks': webhooks_list}), 200

# Chat with agent (public endpoint for widget)
@app.route('/api/chat/<int:agent_id>', methods=['POST'])
def chat_with_agent(agent_id):
    data = request.json
    message = data.get('message')
    thread_id = data.get('thread_id')
    
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get agent and its owner
    c.execute('SELECT assistant_id, user_id FROM agents WHERE id = ?', (agent_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return jsonify({'error': 'Agent not found'}), 404
    
    assistant_id = result[0]
    agent_owner_id = result[1]
    
    # Check message limit for the agent owner
    can_send, limit_error = check_message_limit(agent_owner_id)
    if not can_send:
        conn.close()
        return jsonify({'error': limit_error, 'upgrade_required': True}), 403
    
    # Increment message count
    increment_message_count(agent_owner_id)
    
    print(f"Chat request for agent {agent_id}, using assistant_id: {assistant_id}")
    
    try:
        # Create or use existing thread
        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
            
            # Store conversation
            c.execute('INSERT INTO conversations (agent_id, thread_id) VALUES (?, ?)',
                      (agent_id, thread_id))
            conn.commit()
            conversation_id = c.lastrowid
        else:
            c.execute('SELECT id FROM conversations WHERE thread_id = ?', (thread_id,))
            conversation_id = c.fetchone()[0]
        
        # Add message to thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message
        )
        
        # Store user message
        c.execute('INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)',
                  (conversation_id, 'user', message))
        conn.commit()
        
        # Run assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        
        # Wait for completion
        import time
        while run.status in ['queued', 'in_progress']:
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )
        
        # Get assistant's response
        messages = client.beta.threads.messages.list(thread_id=thread_id)
        assistant_message = messages.data[0].content[0].text.value
        
        # Store assistant message
        c.execute('INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)',
                  (conversation_id, 'assistant', assistant_message))
        conn.commit()
        conn.close()
        
        # Count assistant response as another message
        increment_message_count(agent_owner_id)
        
        # Store webhook event
        store_webhook_event(agent_id, 'message_received', {
            'message': message,
            'response': assistant_message,
            'thread_id': thread_id
        })
        
        return jsonify({
            'response': assistant_message,
            'thread_id': thread_id
        }), 200
        
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

# ==================== USER KNOWLEDGE BASE FILES ====================

# Get user's knowledge base files
@app.route('/api/user/files', methods=['GET'])
@token_required
def get_user_files(current_user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('''SELECT id, filename, openai_file_id, file_size, purpose, uploaded_at 
                 FROM user_files WHERE user_id = ? ORDER BY uploaded_at DESC''',
              (current_user_id,))
    files = c.fetchall()
    conn.close()
    
    files_list = []
    for file in files:
        files_list.append({
            'id': file[0],
            'filename': file[1],
            'openai_file_id': file[2],
            'file_size': file[3],
            'purpose': file[4],
            'uploaded_at': file[5]
        })
    
    return jsonify({'files': files_list}), 200

# Upload file to OpenAI and store in knowledge base
@app.route('/api/user/files', methods=['POST'])
@token_required
def upload_user_file(current_user_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    try:
        # Read file content
        file_content = file.read()
        file_size = len(file_content)
        filename = file.filename
        
        # Upload file to OpenAI (must be tuple: filename, content, content_type)
        uploaded_file = client.files.create(
            file=(filename, file_content),
            purpose='assistants'
        )
        
        # Store in database
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        c.execute('''INSERT INTO user_files (user_id, filename, openai_file_id, file_size, purpose) 
                     VALUES (?, ?, ?, ?, ?)''',
                  (current_user_id, file.filename, uploaded_file.id, file_size, 'assistants'))
        conn.commit()
        file_db_id = c.lastrowid
        conn.close()
        
        return jsonify({
            'message': 'File uploaded successfully',
            'file': {
                'id': file_db_id,
                'filename': file.filename,
                'openai_file_id': uploaded_file.id,
                'file_size': file_size
            }
        }), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Delete file from OpenAI and database
@app.route('/api/user/files/<int:file_id>', methods=['DELETE'])
@token_required
def delete_user_file(current_user_id, file_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    
    # Get file info and verify ownership
    c.execute('SELECT openai_file_id FROM user_files WHERE id = ? AND user_id = ?',
              (file_id, current_user_id))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    
    openai_file_id = result[0]
    
    try:
        # Delete from OpenAI
        client.files.delete(openai_file_id)
    except Exception as e:
        # Continue even if OpenAI delete fails (file might already be deleted)
        print(f"OpenAI delete error: {e}")
    
    # Delete from database
    c.execute('DELETE FROM user_files WHERE id = ?', (file_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'message': 'File deleted successfully'}), 200

# Check if user is admin
@app.route('/api/check-admin', methods=['GET'])
@token_required
def check_admin(current_user_id):
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return jsonify({'is_admin': False}), 200
    
    user_email = result[0]
    is_admin = user_email.lower() == ADMIN_EMAIL.lower()
    
    return jsonify({'is_admin': is_admin, 'email': user_email}), 200

# Get all users (admin only)
@app.route('/api/admin/users', methods=['GET'])
@token_required
def get_all_users(current_user_id):
    # Check if user is admin
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
    result = c.fetchone()
    
    if not result or result[0].lower() != ADMIN_EMAIL.lower():
        conn.close()
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Get all users with their stats
    c.execute('''SELECT id, name, email, created_at, plan, message_count, 
                 plan_expires_at, plan_start_date 
                 FROM users ORDER BY created_at DESC''')
    users = c.fetchall()
    
    users_list = []
    for user in users:
        user_id = user[0]
        
        # Count agents for each user
        c.execute('SELECT COUNT(*) FROM agents WHERE user_id = ?', (user_id,))
        agent_count = c.fetchone()[0]
        
        # Count files for each user
        c.execute('SELECT COUNT(*) FROM user_files WHERE user_id = ?', (user_id,))
        file_count = c.fetchone()[0]
        
        users_list.append({
            'id': user_id,
            'name': user[1],
            'email': user[2],
            'created_at': user[3],
            'plan': user[4] or 'free',
            'message_count': user[5] or 0,
            'plan_expires_at': user[6],
            'plan_start_date': user[7],
            'agent_count': agent_count,
            'file_count': file_count
        })
    
    # Count agents created this month
    current_month = datetime.datetime.now().strftime('%Y-%m')
    c.execute("SELECT COUNT(*) FROM agents WHERE strftime('%Y-%m', created_at) = ?", (current_month,))
    agents_this_month = c.fetchone()[0]

    conn.close()
    return jsonify({
        'users': users_list,
        'stats': {
            'agents_this_month': agents_this_month
        }
    }), 200

# Get all payments (admin only)
@app.route('/api/admin/payments', methods=['GET'])
@token_required
def get_all_payments(current_user_id):
    # Check if user is admin
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
    result = c.fetchone()
    
    if not result or result[0].lower() != ADMIN_EMAIL.lower():
        conn.close()
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Get all payments
    c.execute('''SELECT id, user_id, user_name, user_email, plan, amount, currency,
                 razorpay_order_id, razorpay_payment_id, status, created_at
                 FROM payments ORDER BY created_at DESC''')
    payments = c.fetchall()
    
    payments_list = []
    total_revenue = 0
    pro_revenue = 0
    business_revenue = 0
    
    for payment in payments:
        amount = payment[5] / 100  # Convert paise to rupees
        total_revenue += amount
        
        if payment[4] == 'pro':
            pro_revenue += amount
        elif payment[4] == 'business':
            business_revenue += amount
        
        payments_list.append({
            'id': payment[0],
            'user_id': payment[1],
            'user_name': payment[2],
            'user_email': payment[3],
            'plan': payment[4],
            'amount': amount,
            'currency': payment[6],
            'razorpay_order_id': payment[7],
            'razorpay_payment_id': payment[8],
            'status': payment[9],
            'created_at': payment[10]
        })
    
    conn.close()
    return jsonify({
        'payments': payments_list,
        'total_revenue': total_revenue,
        'pro_revenue': pro_revenue,
        'business_revenue': business_revenue,
        'total_transactions': len(payments_list)
    }), 200

# Sync existing paid users to payments table (admin only)
@app.route('/api/admin/sync-payments', methods=['POST'])
@token_required
def sync_payments(current_user_id):
    # Check if user is admin
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
    result = c.fetchone()
    
    if not result or result[0].lower() != ADMIN_EMAIL.lower():
        conn.close()
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Get all paid users who don't have payment records
    c.execute('''SELECT id, name, email, plan, plan_start_date, razorpay_customer_id 
                 FROM users WHERE plan IN ('pro', 'business')''')
    paid_users = c.fetchall()
    
    synced = 0
    for user in paid_users:
        user_id, name, email, plan, start_date, razorpay_id = user
        
        # Check if payment record already exists for this user
        c.execute('SELECT id FROM payments WHERE user_id = ? AND plan = ?', (user_id, plan))
        if c.fetchone():
            continue  # Already has record
        
        # Create payment record
        amount = PLAN_PRICES.get(plan, 0)
        c.execute('''INSERT INTO payments (user_id, user_name, user_email, plan, amount, 
                     razorpay_payment_id, status, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, 'success', ?)''',
                  (user_id, name, email, plan, amount, razorpay_id or 'synced', start_date or datetime.datetime.now().isoformat()))
        synced += 1
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': f'Payment check completed. Found {synced} new records',
        'synced_count': synced
    }), 200

# Delete user (admin only)
@app.route('/api/admin/users/<int:user_id>', methods=['DELETE', 'OPTIONS'])
@token_required
def delete_user(current_user_id, user_id):
    # Check if user is admin
    conn = sqlite3.connect('db/chatbot.db')
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (current_user_id,))
    result = c.fetchone()
    
    if not result or result[0].lower() != ADMIN_EMAIL.lower():
        conn.close()
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Don't allow admin to delete themselves
    if user_id == current_user_id:
        conn.close()
        return jsonify({'error': 'Cannot delete your own account'}), 400
    
    # Check if user exists
    c.execute('SELECT id FROM users WHERE id = ?', (user_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    
    try:
        # Delete user's agents
        c.execute('DELETE FROM agents WHERE user_id = ?', (user_id,))
        
        # Delete user's files
        c.execute('DELETE FROM user_files WHERE user_id = ?', (user_id,))
        
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        
        conn.commit()
        conn.close()
        return jsonify({'message': 'User deleted successfully'}), 200
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

# ==================== NOTIFICATIONS ENDPOINTS ====================

@app.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications(current_user_id):
    try:
        conn = sqlite3.connect('db/chatbot.db')
        c = conn.cursor()
        c.execute('''SELECT id, title, message, type, is_read, created_at 
                     FROM notifications WHERE user_id = ? 
                     ORDER BY created_at DESC LIMIT 50''', (current_user_id,))
        rows = c.fetchall()
        
        notifications = []
        for row in rows:
            notifications.append({
                'id': row[0],
                'title': row[1],
                'message': row[2],
                'type': row[3],
                'is_read': bool(row[4]),
                'created_at': row[5]
            })
            
        # Get unread count
        c.execute('SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0', (current_user_id,))
        unread_count = c.fetchone()[0]
        
        conn.close()
        return jsonify({'notifications': notifications, 'unread_count': unread_count}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications/read', methods=['POST'])
@token_required
def mark_notifications_read(current_user_id):
    data = request.json
    notification_id = data.get('notification_id') # If None, mark all as read
    
    try:
        conn = sqlite3.connect('chatbot.db')
        c = conn.cursor()
        
        if notification_id:
            c.execute('UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?', 
                      (notification_id, current_user_id))
        else:
            c.execute('UPDATE notifications SET is_read = 1 WHERE user_id = ?', (current_user_id,))
            
        conn.commit()
        conn.close()
        return jsonify({'message': 'Notifications marked as read'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, port=8000, host='0.0.0.0')