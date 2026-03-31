"""
Background Worker Process for BotCraft
Handles asynchronous tasks that shouldn't block web requests:
- Email OTP sending
- OpenAI API calls
- File processing
- Scheduled maintenance tasks
"""

import os
import time
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

# Email Configuration
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_EMAIL = os.environ.get('SMTP_EMAIL', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')

# Database path
DB_PATH = os.environ.get('DATABASE_PATH', 'db/chatbot.db')


def send_email_task(recipient_email, subject, body):
    """
    Background task to send email (OTP, notifications, etc.)
    
    Args:
        recipient_email (str): Email address to send to
        subject (str): Email subject
        body (str): Email body/content
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        
        print(f"[{datetime.now()}] Email sent successfully to {recipient_email}")
        return True
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: Failed to send email to {recipient_email}: {str(e)}")
        return False


def cleanup_expired_otps():
    """
    Cleanup task to remove expired OTPs from database
    Runs periodically to maintain database health
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Delete OTPs older than 5 minutes (configurable)
        expiry_time = datetime.now() - timedelta(minutes=5)
        cursor.execute(
            "DELETE FROM otps WHERE created_at < ?",
            (expiry_time.isoformat(),)
        )
        conn.commit()
        deleted = cursor.rowcount
        
        if deleted > 0:
            print(f"[{datetime.now()}] Cleanup: Removed {deleted} expired OTPs")
        
        conn.close()
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: OTP cleanup failed: {str(e)}")


def cleanup_old_logs():
    """
    Cleanup task to remove old log entries
    Keeps database size manageable
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Delete logs older than 30 days
        cutoff_date = datetime.now() - timedelta(days=30)
        cursor.execute(
            "DELETE FROM logs WHERE timestamp < ?",
            (cutoff_date.isoformat(),)
        )
        conn.commit()
        deleted = cursor.rowcount
        
        if deleted > 0:
            print(f"[{datetime.now()}] Cleanup: Removed {deleted} old log entries")
        
        conn.close()
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: Log cleanup failed: {str(e)}")


def health_check():
    """
    Health check task to verify worker is running
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        print(f"[{datetime.now()}] Worker Health Check: OK")
        return True
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: Health check failed: {str(e)}")
        return False


def main():
    """
    Main worker loop
    Runs periodic background tasks
    """
    print("=" * 60)
    print("BotCraft Background Worker Started")
    print("=" * 60)
    print(f"[{datetime.now()}] Worker Process ID: {os.getpid()}")
    print(f"[{datetime.now()}] Database: {DB_PATH}")
    print(f"[{datetime.now()}] SMTP Server: {SMTP_SERVER}:{SMTP_PORT}")
    print("=" * 60)
    
    task_counter = 0
    
    try:
        while True:
            task_counter += 1
            
            # Health check every minute
            if task_counter % 60 == 0:
                health_check()
            
            # Cleanup expired OTPs every 10 minutes
            if task_counter % 600 == 0:
                cleanup_expired_otps()
            
            # Cleanup old logs every hour
            if task_counter % 3600 == 0:
                cleanup_old_logs()
                print(f"[{datetime.now()}] Worker is running normally")
            
            # Sleep for 1 second before next task cycle
            time.sleep(1)
    
    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Worker shutting down...")
    except Exception as e:
        print(f"[{datetime.now()}] FATAL ERROR in worker: {str(e)}")


if __name__ == '__main__':
    main()
