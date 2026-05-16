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
from pymongo import MongoClient
import gridfs
import base64
from playwright.sync_api import sync_playwright
from bson.objectid import ObjectId
from dotenv import load_dotenv
import certifi

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
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin@resumora.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# MongoDB Config
MONGO_URI = os.environ.get("MONGO_URI")
# Standard Atlas connection configuration
client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=5000
)
# MongoDB Collections
db = client['resume_builder']
resumes_col = db['resumes']
stats_col = db['stats']
users_col = db['users']
logs_col = db['logs']
feedback_col = db['feedback']


fs = gridfs.GridFS(db)

def migrate_data():
    """Migrate all data including PDFs to MongoDB Cluster"""
    try:
        # 1. Migrate Resumes & Users
        if os.path.exists(JSON_FILE_PATH):
            with open(JSON_FILE_PATH, 'r') as f:
                resumes = json.load(f)
                if resumes and isinstance(resumes, list):
                    if resumes_col.count_documents({}) == 0:
                        resumes_col.insert_many(resumes)
                        logger.info(f"Migrated {len(resumes)} resumes to MongoDB")
                        
                        users = {}
                        for r in resumes:
                            email = r.get('user_email')
                            if email and email not in users:
                                users[email] = {
                                    "_id": email,
                                    "name": r.get('user_name', 'User'),
                                    "last_seen": r.get('timestamp')
                                }
                        if users:
                            for user_data in users.values():
                                users_col.update_one({"_id": user_data["_id"]}, {"$set": user_data}, upsert=True)
                            logger.info(f"Migrated {len(users)} users to MongoDB")
            
        # 2. Migrate Stats
        if os.path.exists(STATS_FILE_PATH):
            with open(STATS_FILE_PATH, 'r') as f:
                stats = json.load(f)
                if stats and isinstance(stats, dict):
                    if stats_col.count_documents({}) == 0:
                        for day, data in stats.items():
                            data['_id'] = day
                            stats_col.insert_one(data)
                        logger.info("Migrated stats to MongoDB")

    except Exception as e:
        logger.error(f"Migration error: {e}")

# Run migration if needed (comment out after first successful run)
# migrate_data()

def track_stat(stat_key):
    """Track daily statistics in MongoDB with graceful failure"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        stats_col.update_one(
            {"_id": today},
            {"$inc": {stat_key: 1}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error tracking stat {stat_key}: {e} (Continuing without tracking)")

def cleanup_old_logs():
    """Delete logs older than 15 days"""
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=15)
        # Assuming timestamp is stored as ISO string or datetime
        # For simplicity, we'll use a string comparison if stored as YYYY-MM-DD
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        result = logs_col.delete_many({"timestamp": {"$lt": cutoff_str}})
        logger.info(f"Cleaned up {result.deleted_count} old logs")
    except Exception as e:
        logger.error(f"Error cleaning up logs: {e}")

def log_event(event_type, email, data=None):
    """Log an event with user email and data (including location)"""
    try:
        log_entry = {
            "event": event_type,
            "email": email,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": data or {}
        }
        logs_col.insert_one(log_entry)
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
    """Local JSON backup in case MongoDB fails"""
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
    """Save resume data to MongoDB with local JSON fallback"""
    try:
        # Add session user info if logged in
        data['user_email'] = session.get('user_email')
        data['user_name'] = session.get('user_name')
        
        resumes_col.insert_one(data)
        logger.info("Data saved successfully to MongoDB")
    except Exception as e:
        logger.error(f"MongoDB save failed, falling back to local JSON: {e}")
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


import threading

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
        resumes = list(resumes_col.find({"user_email": session.get('user_email')}).sort('timestamp', -1))
        for r in resumes:
            if '_id' in r: r['_id'] = str(r['_id'])
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
    """Fetch a specific resume data from MongoDB"""
    try:
        if not session.get('user_email'):
            return json.dumps({"status": "error", "message": "Unauthorized"}), 401
            
        resume = resumes_col.find_one({
            "_id": ObjectId(resume_id),
            "user_email": session.get('user_email')
        })
        
        if resume:
            resume['_id'] = str(resume['_id'])
            return json.dumps(resume)
        return json.dumps({"status": "error", "message": "Not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching resume: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 500

@app.route('/last_resume')
def last_resume():
    """Fetch the latest saved resume data from MongoDB"""
    try:
        if not session.get('user_email'):
            return json.dumps({})
            
        last = resumes_col.find_one({"user_email": session.get('user_email')}, sort=[('timestamp', -1)])
        if last:
            last['_id'] = str(last['_id'])
            return json.dumps(last)
        return json.dumps({})
    except Exception as e:
        logger.error(f"Error fetching last resume: {e}")
        return json.dumps({})

@app.route('/save', methods=['POST'])
def save_data():
    """Save resume data to MongoDB (Update if resume_id exists, else Insert)"""
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
        
        if resume_id:
            resumes_col.update_one(
                {"_id": ObjectId(resume_id), "user_email": session.get('user_email')},
                {"$set": form_data}
            )
            inserted_id = resume_id
        else:
            result = resumes_col.insert_one(form_data)
            inserted_id = str(result.inserted_id)
            
        run_in_background(track_stat, "form_starts")
        return json.dumps({"status": "success", "message": "Data saved successfully", "resume_id": inserted_id})
    except Exception as e:
        logger.error(f"Error in /save: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 400

@app.route('/download-pdf', methods=['POST'])
def download_pdf():
    """Generate and download PDF using WeasyPrint"""
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
        
        # Generate PDF using Playwright
        pdf_content = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # Set content and wait for images/fonts to load
            page.set_content(rendered_html, wait_until="load")
            # Generate PDF with A4 settings
            pdf_content = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"}
            )
            browser.close()
            
        if not pdf_content:
            return "Failed to generate PDF", 500
            
        pdf_file = BytesIO(pdf_content)
        pdf_file.seek(0)
        
        filename = f"{personal.get('name', 'Resume').replace(' ', '_')}_Resume.pdf"
        
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
        existing_user = users_col.find_one({"_id": email})
        is_returning = existing_user is not None
        
        # Update or Insert user in MongoDB
        users_col.update_one(
            {"_id": email},
            {
                "$set": {
                    "name": name,
                    "google_id": userid,
                    "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                },
                "$setOnInsert": {
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            },
            upsert=True
        )
        
        return json.dumps({
            "status": "success", 
            "returning": is_returning,
            "redirect": "/dashboard"
        })
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 401




@app.route('/admin-dashboard')
@admin_required
def admin_dashboard():
    """Admin dashboard showing all resumes and users from MongoDB"""
    try:
        # Pagination parameters
        res_page = int(request.args.get('res_page', 1))
        u_page = int(request.args.get('u_page', 1))
        l_page = int(request.args.get('l_page', 1))
        per_page = int(request.args.get('per_page', 25))

        # Fetch resumes
        res_total = resumes_col.count_documents({})
        resumes = list(resumes_col.find().sort('timestamp', -1).skip((res_page-1)*per_page).limit(per_page))
        for r in resumes:
            if '_id' in r: r.pop('_id')
            
        # Fetch users
        u_total = users_col.count_documents({})
        users = list(users_col.find().sort('last_seen', -1).skip((u_page-1)*per_page).limit(per_page))
        for u in users:
            u['email'] = u.get('_id')
            if '_id' in u: u.pop('_id')

        # Fetch logs
        l_total = logs_col.count_documents({})
        logs = list(logs_col.find().sort('timestamp', -1).skip((l_page-1)*per_page).limit(per_page))
        
        # Fetch feedbacks
        feedbacks = list(feedback_col.find().sort('timestamp', -1))

        return render_template('admin_dashboard.html', 
                             resumes=resumes, res_total=res_total, res_page=res_page,
                             users=users, u_total=u_total, u_page=u_page,
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
    """Get statistics for admin dashboard from MongoDB"""
    try:
        stats = list(stats_col.find())
        
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
            
        total_users = users_col.count_documents({})
        
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
    """Export all resume data from MongoDB to a CSV file"""
    try:
        resumes = list(resumes_col.find().sort('timestamp', -1))
        
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
    """Export all user data from MongoDB to a CSV file"""
    try:
        users = list(users_col.find().sort('last_seen', -1))
        
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
    """Generate and download PDF from stored data in MongoDB"""
    try:
        if not session.get('user_email'):
            return "Unauthorized", 401
            
        resume = resumes_col.find_one({
            "_id": ObjectId(resume_id),
            "user_email": session.get('user_email')
        })
        
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
        
        # Generate PDF using Playwright
        pdf_content = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(rendered_html, wait_until="load")
            pdf_content = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"}
            )
            browser.close()
            
        if not pdf_content:
            return "Failed to generate PDF", 500
            
        filename = f"{personal.get('name', 'Resume').replace(' ', '_')}_Resume.pdf"
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
    """Delete a resume from database"""
    if not session.get('user_email'):
        return "Unauthorized", 401
    
    try:
        # Verify ownership and delete
        resumes_col.delete_one({"_id": ObjectId(resume_id), "user_email": session.get('user_email')})
        return "Success", 200
    except Exception as e:
        logger.error(f"Error deleting resume: {e}")
        return str(e), 500

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    """Submit user feedback to MongoDB"""
    try:
        data = request.get_json()
        feedback = {
            'email': session.get('user_email', 'guest'),
            'rating': data.get('rating'),
            'comment': data.get('comment'),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        feedback_col.insert_one(feedback)
        return json.dumps({"status": "success"})
    except Exception as e:
        logger.error(f"Error submitting feedback: {e}")
        return json.dumps({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5003)