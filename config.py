"""
Configuration module for Resume Builder (Resumora).
Handles environment variables, central data paths, authentication, and email settings.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory (root of the application)
BASE_DIR = Path(__file__).resolve().parent

# Load .env file from the application root directory
env_path = BASE_DIR / '.env'
load_dotenv(dotenv_path=env_path)

# Environment mode: 'development' or 'production'
APP_ENV = os.environ.get('APP_ENV', 'production')

# Secret Key
SECRET_KEY = os.environ.get('SECRET_KEY', 'resumora-secure-secret-key-prod-2026')

# Centralized Data Directory (Single Source of Truth)
# Always resolves relative to BASE_DIR if a relative path is provided,
# ensuring the exact same centralized data folder is used in development and production.
data_dir_env = os.environ.get('DATA_DIR', 'data')
if os.path.isabs(data_dir_env):
    DATA_DIR = Path(data_dir_env).resolve()
else:
    DATA_DIR = (BASE_DIR / data_dir_env).resolve()

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Data files paths
RESUMES_FILE = DATA_DIR / 'resumes.json'
STATS_FILE = DATA_DIR / 'stats.json'
USERS_FILE = DATA_DIR / 'users.json'
FEEDBACK_FILE = DATA_DIR / 'feedback.json'
LOGS_FILE = DATA_DIR / 'logs.json'
ADMINS_FILE = DATA_DIR / 'admins.json'
ERROR_LOG_FILE = DATA_DIR / 'error.log'
PDF_TEMP_DIR = DATA_DIR / 'temp_pdfs'
PDF_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Initial Administrator Emails (seeded persistently if admins.json is missing)
INITIAL_ADMIN_EMAILS = [
    "laxminivasmorishetty143@gmail.com",
    "morishettylaxminivas@gmail.com",
    "hello.aivontraa@gmail.com"
]

# Admin Password (used alongside authorized email authentication)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'nivas@2026')

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get(
    'GOOGLE_CLIENT_ID',
    '493060599897-r5mk9n2v2t2pk3fgm1dqmg6j0hcg9dfu.apps.googleusercontent.com'
)
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')

# SMTP Email Configuration (Managed directly in config.py)
SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 465
SMTP_USE_SSL = True
SMTP_USERNAME = 'hello.aivontraa@gmail.com'
SMTP_PASSWORD = 'ewsr ywdn qawe jlbr'
SMTP_TIMEOUT = 30
SMTP_MAX_RETRIES = 3
SMTP_RETRY_DELAY = 5

EMAIL_FROM_NAME = 'Resumora'
EMAIL_FROM_ADDRESS = 'hello.aivontraa@gmail.com'

# Storage thresholds (MB)
STORAGE_WARN_MB = float(os.environ.get('STORAGE_WARN_MB', '50'))
STORAGE_CRITICAL_MB = float(os.environ.get('STORAGE_CRITICAL_MB', '70'))
STORAGE_MAX_MB = float(os.environ.get('STORAGE_MAX_MB', '80'))
