from flask import Flask, request, send_file, render_template, redirect, url_for, session, flash, send_from_directory, jsonify
from io import BytesIO, TextIOWrapper
import os
import json
import logging
from datetime import datetime
from functools import wraps
import requests
import re
import constants
import textwrap
from google.oauth2 import id_token
from google.auth.transport import requests as google_auth_requests
import uuid
import csv
import base64
import os
# Fix for WeasyPrint on macOS Apple Silicon finding Homebrew libraries
if os.path.exists('/opt/homebrew/lib'):
    os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = '/opt/homebrew/lib' + (':' + os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '') if os.environ.get('DYLD_FALLBACK_LIBRARY_PATH') else '')

from weasyprint import HTML
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import threading

# Load environment variables from the same directory as this file
from pathlib import Path
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)


# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')  # Use environment variable in production

os.makedirs('data/pdfs', exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
# Add file handler for persistent logs
file_handler = logging.FileHandler('data/error.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)
@app.after_request
def add_header(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    return response

# Constants
JSON_FILE_PATH = 'data/resumes.json'
STATS_FILE_PATH = 'data/stats.json'
PDF_DIR = 'data/pdfs'
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'hello.aivontraa.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'nivas@2026')

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# JSON Database Helper for Local Thread-Safe Storage
class JSONDatabase:
    def __init__(self):
        self.lock = threading.Lock()
        self.resumes_path = 'data/resumes.json'
        self.stats_path = 'data/stats.json'
        self.users_path = 'data/users.json'
        self.feedback_path = 'data/feedback.json'
        self.logs_path = 'data/logs.json'
        
        # Ensure files exist
        for path in [self.resumes_path, self.stats_path, self.users_path, self.feedback_path, self.logs_path]:
            if not os.path.exists(path):
                # stats and users are dict-based, others are list-based
                default_content = {} if path in [self.stats_path, self.users_path] else []
                try:
                    with open(path, 'w') as f:
                        json.dump(default_content, f, indent=4)
                except Exception as e:
                    print(f"Error creating file {path}: {e}")

    def _read(self, path):
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error reading JSON file {path}: {e}")
        return {} if path in [self.stats_path, self.users_path] else []

    def _write(self, path, data):
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Error writing JSON file {path}: {e}")

    # --- Resumes ---
    def get_resumes_by_user(self, email):
        with self.lock:
            resumes = self._read(self.resumes_path)
            return [r for r in resumes if r.get('user_email') == email]

    def get_resume_by_id(self, resume_id, email):
        with self.lock:
            resumes = self._read(self.resumes_path)
            for r in resumes:
                if r.get('_id') == resume_id and r.get('user_email') == email:
                    return r
            return None

    def get_latest_resume(self, email):
        with self.lock:
            resumes = self._read(self.resumes_path)
            user_resumes = [r for r in resumes if r.get('user_email') == email]
            if not user_resumes:
                return None
            user_resumes.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return user_resumes[0]

    def save_resume(self, resume_id, form_data):
        with self.lock:
            resumes = self._read(self.resumes_path)
            if resume_id:
                for r in resumes:
                    if r.get('_id') == resume_id and r.get('user_email') == form_data.get('user_email'):
                        r.update(form_data)
                        self._write(self.resumes_path, resumes)
                        return resume_id
            new_id = uuid.uuid4().hex
            form_data['_id'] = new_id
            resumes.append(form_data)
            self._write(self.resumes_path, resumes)
            return new_id

    def delete_resume(self, resume_id, email):
        with self.lock:
            resumes = self._read(self.resumes_path)
            new_resumes = [r for r in resumes if not (r.get('_id') == resume_id and r.get('user_email') == email)]
            if len(new_resumes) != len(resumes):
                self._write(self.resumes_path, new_resumes)
                return True
            return False

    def count_resumes(self):
        with self.lock:
            resumes = self._read(self.resumes_path)
            return len(resumes)

    def get_all_resumes_paginated(self, page, per_page):
        with self.lock:
            resumes = self._read(self.resumes_path)
            resumes.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            start = (page - 1) * per_page
            return resumes[start:start+per_page]

    # --- Users ---
    def update_user(self, email, name, google_id):
        with self.lock:
            users = self._read(self.users_path)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            is_returning = email in users
            if is_returning:
                users[email]['name'] = name
                users[email]['google_id'] = google_id
                users[email]['last_seen'] = now_str
            else:
                users[email] = {
                    'name': name,
                    'google_id': google_id,
                    'last_seen': now_str,
                    'created_at': now_str
                }
            self._write(self.users_path, users)
            return is_returning

    def get_user(self, email):
        with self.lock:
            users = self._read(self.users_path)
            user = users.get(email)
            if user:
                user['_id'] = email
            return user

    def count_users(self):
        with self.lock:
            users = self._read(self.users_path)
            return len(users)

    def get_all_users_paginated(self, page, per_page):
        with self.lock:
            users = self._read(self.users_path)
            user_list = []
            for email, u in users.items():
                u_copy = u.copy()
                u_copy['_id'] = email
                u_copy['email'] = email
                user_list.append(u_copy)
            user_list.sort(key=lambda x: x.get('last_seen', ''), reverse=True)
            start = (page - 1) * per_page
            return user_list[start:start+per_page]

    # --- Stats ---
    def track_stat(self, stat_key):
        with self.lock:
            stats = self._read(self.stats_path)
            today = datetime.now().strftime("%Y-%m-%d")
            if today not in stats:
                stats[today] = {}
            stats[today][stat_key] = stats[today].get(stat_key, 0) + 1
            self._write(self.stats_path, stats)

    def get_all_stats(self):
        with self.lock:
            stats = self._read(self.stats_path)
            result = []
            for day, s in stats.items():
                s_copy = s.copy()
                s_copy['_id'] = day
                result.append(s_copy)
            return result

    # --- Logs ---
    def log_event(self, event_type, email, data=None):
        with self.lock:
            logs = self._read(self.logs_path)
            log_entry = {
                "event": event_type,
                "email": email,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": data or {}
            }
            logs.append(log_entry)
            self._write(self.logs_path, logs)

    def count_logs(self):
        with self.lock:
            logs = self._read(self.logs_path)
            return len(logs)

    def get_all_logs_paginated(self, page, per_page):
        with self.lock:
            logs = self._read(self.logs_path)
            logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            start = (page - 1) * per_page
            return logs[start:start+per_page]

    def cleanup_old_logs(self):
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=15)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            logs = self._read(self.logs_path)
            new_logs = [l for l in logs if l.get('timestamp', '') >= cutoff_str]
            deleted_count = len(logs) - len(new_logs)
            if deleted_count > 0:
                self._write(self.logs_path, new_logs)
            return deleted_count

    # --- Feedback ---
    def save_feedback(self, feedback):
        with self.lock:
            feedbacks = self._read(self.feedback_path)
            feedbacks.append(feedback)
            self._write(self.feedback_path, feedbacks)

    def get_all_feedback(self):
        with self.lock:
            feedbacks = self._read(self.feedback_path)
            feedbacks.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return feedbacks

# Initialize JSON Database
db = JSONDatabase()

def send_email_with_attachment(to_email, pdf_bytes, filename, user_name=None):
    sender_email = 'hello.aivontraa@gmail.com'
    sender_password = 'lfwp ykno bhge gduw'
    
    if not to_email:
        logger.error("Recipient email is missing")
        return False
        
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Your Professional Resume - {user_name}" if user_name else 'Your Professional Resume - Resumora'
        msg['From'] = f"Resumora <{sender_email}>"
        msg['To'] = to_email
        msg['Bcc'] = sender_email
        
        greeting = f"Dear {user_name}," if user_name else "Dear Candidate,"
        
        body_text = f"""{greeting}

Thank you for using Resumora. Your professional resume has been successfully generated and is attached to this email.

We wish you the very best in your career pursuits!

Regards,
Resumora
AIVONTRAA AUTOMATION PVT.LTD

---
For any queries, contact:
Email: {sender_email}
Phone: +917893772246
"""
        msg.set_content(body_text)
        
        # HTML version for rich responsive styling
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Your Professional Resume</title>
        </head>
        <body style="margin: 0; padding: 0; background-color: #f4f5f7; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
            <table border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #f4f5f7; padding: 20px 0;">
                <tr>
                    <td align="center">
                        <table border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06); border: 1px solid #e5e7eb;">
                            <!-- Header -->
                            <tr>
                                <td style="background: linear-gradient(135deg, #4f46e5 0%, #3730a3 100%); padding: 40px 30px; text-align: center;">
                                    <h1 style="color: #ffffff; margin: 0; font-size: 28px; font-weight: 800; letter-spacing: -0.5px;">Resumora</h1>
                                    <p style="color: #e0e7ff; margin: 5px 0 0 0; font-size: 14px; font-weight: 500;">Your Career, Elevated</p>
                                </td>
                            </tr>
                            <!-- Body -->
                            <tr>
                                <td style="padding: 40px 30px;">
                                    <h2 style="color: #111827; margin-top: 0; margin-bottom: 20px; font-size: 20px; font-weight: 700;">{greeting}</h2>
                                    <p style="color: #4b5563; font-size: 16px; line-height: 1.6; margin-bottom: 24px;">
                                        Your professional resume has been successfully generated and is attached to this email as a PDF.
                                    </p>
                                    
                                    <!-- Attachment Card -->
                                    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #f9fafb; border-left: 4px solid #4f46e5; border-radius: 4px; margin-bottom: 24px;">
                                        <tr>
                                            <td style="padding: 16px 20px;">
                                                <p style="margin: 0; font-size: 14px; color: #374151; font-weight: 600;">
                                                    File Attached:
                                                </p>
                                                <p style="margin: 4px 0 0 0; font-size: 14px; color: #6b7280; font-family: monospace;">
                                                    {filename}
                                                </p>
                                            </td>
                                        </tr>
                                    </table>
                                    
                                    <p style="color: #4b5563; font-size: 16px; line-height: 1.6; margin-bottom: 32px;">
                                        We wish you the absolute best in your job search and future professional endeavors!
                                    </p>
                                    
                                    <!-- Signoff -->
                                    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="border-top: 1px solid #e5e7eb; padding-top: 24px;">
                                        <tr>
                                            <td>
                                                <p style="margin: 0; color: #9ca3af; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;">Best Regards,</p>
                                                <p style="margin: 4px 0 0 0; color: #111827; font-size: 16px; font-weight: 700;">Resumora</p>
                                                <p style="margin: 0; color: #4b5563; font-size: 14px; font-weight: 500;">AIVONTRAA AUTOMATION PVT.LTD</p>
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                            <!-- Footer -->
                            <tr>
                                <td style="background-color: #f9fafb; padding: 24px 30px; text-align: center; border-top: 1px solid #e5e7eb;">
                                    <p style="margin: 0 0 8px 0; color: #6b7280; font-size: 13px;">
                                        For any queries, please reach out to us:
                                    </p>
                                    <p style="margin: 0; color: #374151; font-size: 14px; font-weight: 600;">
                                        Email: <a href="mailto:{sender_email}" style="color: #4f46e5; text-decoration: none;">{sender_email}</a>
                                        <span style="color: #d1d5db; margin: 0 8px;">|</span>
                                        Phone: <a href="tel:+917893772246" style="color: #4f46e5; text-decoration: none;">+91 7893772246</a>
                                    </p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """
        msg.add_alternative(html_content, subtype='html')
        
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
            
        logger.info(f"Email successfully forwarded to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False

def track_stat(stat_key):
    """Track daily statistics in local JSON database with graceful failure"""
    try:
        db.track_stat(stat_key)
    except Exception as e:
        logger.error(f"Error tracking stat {stat_key}: {e} (Continuing without tracking)")

def cleanup_old_logs():
    """Delete logs older than 15 days"""
    try:
        deleted_count = db.cleanup_old_logs()
        logger.info(f"Cleaned up {deleted_count} old logs")
    except Exception as e:
        logger.error(f"Error cleaning up logs: {e}")

def log_event(event_type, email, data=None):
    """Log an event with user email and data (including location)"""
    try:
        db.log_event(event_type, email, data)
        # Run cleanup occasionally
        if datetime.now().minute % 15 == 0: # Check every 15 mins
            run_in_background(cleanup_old_logs)
    except Exception as e:
        logger.error(f"Error logging event: {e}")

@app.context_processor
def inject_icons():
    return dict(
        ICON_WEBSITE=constants.ICON_WEBSITE,
        ICON_LINKEDIN=constants.ICON_LINKEDIN,
        ICON_GITHUB=constants.ICON_GITHUB,
        ICON_EMAIL=constants.ICON_EMAIL,
        ICON_PHONE=constants.ICON_PHONE,
        ICON_LOCATION=constants.ICON_LOCATION
    )

# Decorator for admin-only routes
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash("Please log in to access this page", 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def save_to_json_backup(data):
    """Local JSON backup fallback method"""
    try:
        existing_data = []
        if os.path.exists(JSON_FILE_PATH):
            with open(JSON_FILE_PATH, 'r') as f:
                try:
                    existing_data = json.load(f)
                except:
                    existing_data = []
        existing_data.append(data)
        with open(JSON_FILE_PATH, 'w') as f:
            json.dump(existing_data, f, indent=4)
        logger.info("Saved to local JSON backup")
    except Exception as e:
        logger.error(f"Backup failed: {e}")

def save_to_db(data):
    """Save resume data to local database"""
    try:
        # Add session user info if logged in
        data['user_email'] = session.get('user_email')
        data['user_name'] = session.get('user_name')
        
        db.save_resume(None, data)
        logger.info("Data saved successfully to JSON database")
    except Exception as e:
        logger.error(f"Save failed, falling back to local JSON: {e}")
        save_to_json_backup(data)

def validate_form_data(form_data):
    """Validate required form fields"""
    errors = []
    personal = form_data.get('personal_details', {})
    if not personal.get('name'):
        errors.append("Name is required")
    if not personal.get('email'):
        errors.append("Email is required")
    if not personal.get('phone'):
        errors.append("Phone number is required")
    
    if errors:
        raise ValueError("; ".join(errors))

def format_url(url):
    """Ensure URLs have proper http/https prefix"""
    if url and not url.startswith(('http://', 'https://')):
        return 'http://' + url
    return url


def run_in_background(f, *args, **kwargs):
    """Run a function in a background thread"""
    threading.Thread(target=f, args=args, kwargs=kwargs).start()

@app.route('/')
def index():
    """Welcome / Landing page"""
    track_stat("visits")
    return render_template('welcome.html', google_client_id=GOOGLE_CLIENT_ID)

@app.route('/dashboard')
def dashboard():
    """User dashboard showing their resumes"""
    if not session.get('user_email'):
        return redirect(url_for('index'))
    
    try:
        resumes = db.get_resumes_by_user(session.get('user_email'))
        return render_template('dashboard.html', resumes=resumes)
    except Exception as e:
        logger.error(f"Error loading dashboard: {e}")
        return redirect(url_for('index'))

@app.route('/builder')
@app.route('/builder/<resume_id>')
def builder(resume_id=None):
    """Resume builder page, optionally loading an existing resume"""
    if not session.get('user_email'):
        return redirect(url_for('index', login='required'))
    return render_template('builder.html', resume_id=resume_id)

@app.route('/thank-you')
def thank_you():
    """Thank you page after resume generation"""
    return render_template('thank_you.html')

@app.route('/get_resume/<resume_id>')
def get_resume(resume_id):
    """Fetch a specific resume data from JSON database"""
    try:
        if not session.get('user_email'):
            return json.dumps({"status": "error", "message": "Unauthorized"}), 401
            
        resume = db.get_resume_by_id(resume_id, session.get('user_email'))
        
        if resume:
            return json.dumps(resume)
        return json.dumps({"status": "error", "message": "Not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching resume: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 500

@app.route('/last_resume')
def last_resume():
    """Fetch the latest saved resume data from JSON database"""
    try:
        if not session.get('user_email'):
            return json.dumps({})
            
        last = db.get_latest_resume(session.get('user_email'))
        if last:
            return json.dumps(last)
        return json.dumps({})
    except Exception as e:
        logger.error(f"Error fetching last resume: {e}")
        return json.dumps({})

@app.route('/save', methods=['POST'])
def save_data():
    """Save resume data to JSON database (Update if resume_id exists, else Insert)"""
    try:
        data = request.get_json()
        resume_id = data.get('resume_id')
        
        form_data = {
            'personal_details': data.get('personal_details', {}),
            'professional_details': data.get('professional_details', {}),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'user_email': session.get('user_email'),
            'user_name': session.get('user_name')
        }
        validate_form_data(form_data)
        
        inserted_id = db.save_resume(resume_id, form_data)
            
        run_in_background(track_stat, "form_starts")
        return json.dumps({"status": "success", "message": "Data saved successfully", "resume_id": inserted_id})
    except Exception as e:
        logger.error(f"Error in /save: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 400

@app.route('/download-pdf', methods=['POST'])
def download_pdf():
    """Generate and download PDF using Playwright"""
    try:
        data = request.get_json()
        if not data:
            return "No data provided", 400
            
        personal = data.get('personal_details', {})
        prof = data.get('professional_details', {})
        
        # Track generation stat
        track_stat("resumes_generated")

        # Prepare icons as base64
        icons = {}
        static_icons_path = os.path.join(app.root_path, 'static', 'icons')
        icon_names = ['email', 'phone', 'location', 'linkedin', 'github', 'website']
        for name in icon_names:
            icon_path = os.path.join(static_icons_path, f'{name}.png')
            if os.path.exists(icon_path):
                with open(icon_path, "rb") as f:
                    icons[name] = base64.b64encode(f.read()).decode('utf-8')

        # Render HTML template
        rendered_html = render_template(
            'resume_pdf.html',
            personal=personal,
            prof=prof,
            icons=icons
        )
        
        # Generate PDF using WeasyPrint
        pdf_content = HTML(string=rendered_html, base_url=request.host_url).write_pdf()
            
        if not pdf_content:
            return "Failed to generate PDF", 500
            
        pdf_file = BytesIO(pdf_content)
        pdf_file.seek(0)
        
        filename = f"{personal.get('name', 'Resume').replace(' ', '_')}_Resume.pdf"
        
        # Forward resume via email in the background if email is provided
        user_email = personal.get('email')
        if user_email:
            run_in_background(send_email_with_attachment, user_email, pdf_content, filename, personal.get('name'))
            
        return send_file(
            pdf_file,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        logger.error(f"Error generating PDF: {e}", exc_info=True)
        return str(e), 500


@app.route('/auth/google', methods=['POST'])
def auth_google():
    """Verify Google Sign-In JWT and log the user in"""
    try:
        data = request.get_json()
        token = data.get('token')
        if not token:
            return json.dumps({"status": "error", "message": "No token provided"}), 400
        
        # Verify the token using legacy Google Auth
        idinfo = id_token.verify_oauth2_token(token, google_auth_requests.Request(), GOOGLE_CLIENT_ID)
        
        userid = idinfo['sub']
        email = idinfo.get('email', '')
        name = idinfo.get('name', 'User')
        
        # Save user session
        session['user_id'] = userid
        session['user_email'] = email
        session['user_name'] = name
        session.permanent = True
        
        # Check if user exists
        existing_user = db.get_user(email)
        is_returning = existing_user is not None
        
        # Update or Insert user in JSON database
        db.update_user(email, name, userid)
        
        return json.dumps({
            "status": "success", 
            "returning": is_returning,
            "redirect": "/dashboard"
        })
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 401


@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            flash('Logged in successfully', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid username or password', 'error')
            
    return render_template('admin_login.html')

@app.route('/admin-dashboard')
@admin_required
def admin_dashboard():
    """Admin dashboard showing all resumes and users from JSON database"""
    try:
        # Pagination parameters
        res_page = int(request.args.get('res_page', 1))
        u_page = int(request.args.get('u_page', 1))
        l_page = int(request.args.get('l_page', 1))
        per_page = int(request.args.get('per_page', 25))

        # Fetch resumes
        res_total = db.count_resumes()
        resumes = db.get_all_resumes_paginated(res_page, per_page)
        resumes_display = []
        for r in resumes:
            r_copy = r.copy()
            if '_id' in r_copy: r_copy.pop('_id')
            resumes_display.append(r_copy)
            
        # Fetch users
        u_total = db.count_users()
        users = db.get_all_users_paginated(u_page, per_page)
        users_display = []
        for u in users:
            u_copy = u.copy()
            u_copy['email'] = u_copy.get('_id')
            if '_id' in u_copy: u_copy.pop('_id')
            users_display.append(u_copy)

        # Fetch logs
        l_total = db.count_logs()
        logs = db.get_all_logs_paginated(l_page, per_page)
        
        # Fetch feedbacks
        feedbacks = db.get_all_feedback()

        return render_template('admin_dashboard.html', 
                             resumes=resumes_display, res_total=res_total, res_page=res_page,
                             users=users_display, u_total=u_total, u_page=u_page,
                             logs=logs, l_total=l_total, l_page=l_page,
                             feedbacks=feedbacks, per_page=per_page)
    except Exception as e:
        logger.error(f"Error loading admin dashboard: {e}", exc_info=True)
        flash("Error loading admin dashboard", 'error')
        return redirect(url_for('admin_login'))

@app.route('/logout')
def logout():
    """Logout user or admin"""
    session.pop('admin_logged_in', None)
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('user_name', None)
    flash("You have been logged out", 'success')
    return redirect(url_for('index'))

@app.route('/admin/stats')
@admin_required
def admin_stats():
    """Get statistics for admin dashboard from JSON database"""
    try:
        stats = db.get_all_stats()
        
        total_visits = 0
        total_generated = 0
        total_starts = 0
        daily_stats = {}
        
        for s in stats:
            day = s.get('_id')
            daily_stats[day] = s
            total_visits += s.get('visits', 0)
            total_generated += s.get('resumes_generated', 0)
            total_starts += s.get('form_starts', 0)
            
        total_users = db.count_users()
        
        total_stats = {
            "total_visits": total_visits,
            "total_generated": total_generated,
            "total_starts": total_starts,
            "total_users": total_users,
            "daily_stats": daily_stats
        }
        return json.dumps(total_stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return json.dumps({"error": str(e)}), 500

@app.route('/admin/export-csv')
@admin_required
def export_csv():
    """Export all resume data from JSON database to a CSV file"""
    try:
        resumes = db.get_all_resumes_paginated(1, 1000000)
        
        if not resumes:
            return "No data found", 404
            
        output = BytesIO()
        wrapper = TextIOWrapper(output, encoding='utf-8-sig', write_through=True)
        
        headers = ['Timestamp', 'Account Name', 'Account Email', 'Resume Name', 'Resume Email', 'Phone', 'Location']
        writer = csv.DictWriter(wrapper, fieldnames=headers)
        writer.writeheader()
        
        for r in resumes:
            personal = r.get('personal_details', {})
            writer.writerow({
                'Timestamp': r.get('timestamp', ''),
                'Account Name': r.get('user_name', 'Guest'),
                'Account Email': r.get('user_email', 'N/A'),
                'Resume Name': personal.get('name', ''),
                'Resume Email': personal.get('email', ''),
                'Phone': personal.get('phone', ''),
                'Location': personal.get('location', '')
            })
            
        wrapper.detach()
        output.seek(0)
        
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"resumes_export_{datetime.now().strftime('%Y%m%d')}.csv"
        )
    except Exception as e:
        logger.error(f"Error exporting CSV: {e}")
        return str(e), 500

@app.route('/admin/export-users-csv')
@admin_required
def export_users_csv():
    """Export all user data from JSON database to a CSV file"""
    try:
        users = db.get_all_users_paginated(1, 1000000)
        
        if not users:
            return "No data found", 404
            
        output = BytesIO()
        wrapper = TextIOWrapper(output, encoding='utf-8-sig', write_through=True)
        
        headers = ['Email', 'Name', 'Google ID', 'Last Seen', 'Created At']
        writer = csv.DictWriter(wrapper, fieldnames=headers)
        writer.writeheader()
        
        for u in users:
            writer.writerow({
                'Email': u.get('_id', ''),
                'Name': u.get('name', ''),
                'Google ID': u.get('google_id', 'N/A'),
                'Last Seen': u.get('last_seen', ''),
                'Created At': u.get('created_at', 'N/A')
            })
            
        wrapper.detach()
        output.seek(0)
        
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"users_export_{datetime.now().strftime('%Y%m%d')}.csv"
        )
    except Exception as e:
        logger.error(f"Error exporting users CSV: {e}")
        return str(e), 500

@app.route('/download-pdf/<resume_id>')
def download_pdf_by_id(resume_id):
    """Generate and download PDF from stored data in JSON database"""
    try:
        if not session.get('user_email'):
            return "Unauthorized", 401
            
        resume = db.get_resume_by_id(resume_id, session.get('user_email'))
        
        if not resume:
            return "Resume not found", 404
            
        personal = resume.get('personal_details', {})
        prof = resume.get('professional_details', {})
        
        # Track generation stat
        track_stat("resumes_generated")
        
        # Prepare icons as base64
        icons = {}
        static_icons_path = os.path.join(app.root_path, 'static', 'icons')
        icon_names = ['email', 'phone', 'location', 'linkedin', 'github', 'website']
        for name in icon_names:
            icon_path = os.path.join(static_icons_path, f'{name}.png')
            if os.path.exists(icon_path):
                with open(icon_path, "rb") as f:
                    icons[name] = base64.b64encode(f.read()).decode('utf-8')

        # Render HTML template
        rendered_html = render_template(
            'resume_pdf.html',
            personal=personal,
            prof=prof,
            icons=icons
        )
        
        # Generate PDF using WeasyPrint
        pdf_content = HTML(string=rendered_html, base_url=request.host_url).write_pdf()
            
        if not pdf_content:
            return "Failed to generate PDF", 500
            
        filename = f"{personal.get('name', 'Resume').replace(' ', '_')}_Resume.pdf"
        
        # Forward resume via email in the background if email is provided
        user_email = personal.get('email')
        if user_email:
            run_in_background(send_email_with_attachment, user_email, pdf_content, filename, personal.get('name'))
            
        return send_file(
            BytesIO(pdf_content),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        logger.error(f"Error generating PDF from DB: {e}", exc_info=True)
        return str(e), 500

@app.route('/delete-resume/<resume_id>', methods=['DELETE'])
def delete_resume(resume_id):
    """Delete a resume from JSON database"""
    if not session.get('user_email'):
        return "Unauthorized", 401
    
    try:
        if db.delete_resume(resume_id, session.get('user_email')):
            return "Success", 200
        return "Not found or unauthorized", 404
    except Exception as e:
        logger.error(f"Error deleting resume: {e}")
        return str(e), 500

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    """Submit user feedback to local database"""
    try:
        data = request.get_json()
        feedback = {
            'email': session.get('user_email', 'guest'),
            'rating': data.get('rating'),
            'comment': data.get('comment'),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        db.save_feedback(feedback)
        return json.dumps({"status": "success"})
    except Exception as e:
        logger.error(f"Error submitting feedback: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 500

@app.route('/debug-env')
def debug_env():
    import subprocess
    results = {}
    
    # Check common executable paths
    paths_to_check = [
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
        '/usr/bin/chrome'
    ]
    for p in paths_to_check:
        results[p] = os.path.exists(p)
        
    # Run which commands
    for cmd in ['chromium', 'chromium-browser', 'google-chrome', 'chrome']:
        try:
            path = subprocess.check_output(['which', cmd], stderr=subprocess.STDOUT).decode('utf-8').strip()
            results[f'which {cmd}'] = path
        except Exception as e:
            results[f'which {cmd}'] = f"Error: {e}"
            
    # Also get python version and system image / os info
    try:
        results['uname'] = subprocess.check_output(['uname', '-a']).decode('utf-8').strip()
    except Exception as e:
        results['uname'] = str(e)
        
    return jsonify(results)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5003)