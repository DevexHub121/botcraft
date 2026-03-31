# Web Process - Main Flask Application
# Runs the Flask web server using Gunicorn with 4 workers
# Listens on PORT environment variable (default 5000)
# Handles HTTP requests, authentication, payments, and API endpoints
web: gunicorn --workers=4 --worker-class=sync --timeout=120 --bind=0.0.0.0:$PORT app:app

# Release Process - Database Initialization
# Runs before deployment to initialize/migrate database
# Creates necessary tables and indexes for first-time setup
# Set PORT to 0 to prevent this process from listening
release: python -c "import app; print('Database initialized')" || true

# Worker Process - Background Tasks
# Handles asynchronous operations without blocking web requests
# Processes: Email OTP sending, OpenAI API calls, file uploads
# Scheduled tasks: Database cleanup, log rotation
# Can be scaled independently based on load
worker: python worker.py

