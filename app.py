import os
import sys
import re
import json
import uuid
import csv
import time
import base64
import logging
import smtplib
import threading
from pathlib import Path
from io import BytesIO, TextIOWrapper
from datetime import datetime, timedelta
from functools import wraps
from email.message import EmailMessage

from flask import (
    Flask, request, render_template, redirect,
    url_for, session, flash, jsonify, send_file
)
from google.oauth2 import id_token
from google.auth.transport import requests as google_auth_requests

# Import central configuration and constants
import config
import constants
from pdf_service import generate_resume_pdf

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if config.APP_ENV == 'development' else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('resumora')

try:
    file_handler = logging.FileHandler(str(config.ERROR_LOG_FILE), encoding='utf-8')
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(file_handler)
except Exception as e:
    print(f"Warning: Could not attach file logger: {e}")

# ---------------------------------------------------------------------------
# Flask Application
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = config.SECRET_KEY

@app.after_request
def add_header(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    return response


# ===========================================================================
# Centralized JSON Database (Thread-Safe Single Source of Truth)
# ===========================================================================
class JSONDatabase:
    def __init__(self, data_dir=config.DATA_DIR):
        self.lock = threading.Lock()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.resumes_path = self.data_dir / 'resumes.json'
        self.stats_path = self.data_dir / 'stats.json'
        self.users_path = self.data_dir / 'users.json'
        self.feedback_path = self.data_dir / 'feedback.json'
        self.logs_path = self.data_dir / 'logs.json'
        self.admins_path = self.data_dir / 'admins.json'

        # Ensure all required JSON files exist with correct default structures
        initial_configs = [
            (self.resumes_path, []),
            (self.stats_path, {}),
            (self.users_path, {}),
            (self.feedback_path, []),
            (self.logs_path, []),
            (self.admins_path, {
                "max_allowed": 10,
                "authorized_emails": list(config.INITIAL_ADMIN_EMAILS)
            }),
        ]

        for path, default in initial_configs:
            if not path.exists():
                try:
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(default, f, indent=4, ensure_ascii=False)
                except Exception as e:
                    logger.error(f"Error initializing {path}: {e}")

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------
    def _read(self, path):
        path = Path(path)
        try:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error reading {path}: {e}")

        # Fallback defaults based on file path
        if path in [self.stats_path, self.users_path, self.admins_path]:
            return {}
        return []

    def _write(self, path, data):
        path = Path(path)
        tmp_path = path.with_suffix('.tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Error writing {path}: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Resumes Operations
    # ------------------------------------------------------------------
    def _account_key_for_resume(self, r):
        """
        Extract unique account identifier.
        Prioritizes user_email (authenticated user account).
        Falls back to personal_details email, or guest_<id> for anonymous records.
        """
        if not r:
            return 'unknown'
        email = (r.get('user_email') or '').strip().lower()
        if not email and isinstance(r.get('personal_details'), dict):
            email = (r.get('personal_details', {}).get('email') or '').strip().lower()
        if not email:
            return f"guest_{r.get('_id', 'unknown')}"
        return email

    def get_resumes_by_user(self, email):
        if not email:
            return []
        email_clean = email.strip().lower()
        with self.lock:
            resumes = self._read(self.resumes_path)
            user_resumes = [
                r for r in resumes
                if self._account_key_for_resume(r) == email_clean
            ]
            user_resumes.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
            return user_resumes

    def get_resume_by_id(self, resume_id, email):
        if not email:
            return None
        email_clean = email.strip().lower()
        with self.lock:
            resumes = self._read(self.resumes_path)
            for r in resumes:
                if str(r.get('_id')) == str(resume_id) and self._account_key_for_resume(r) == email_clean:
                    return r
            return None

    def get_resume_by_id_admin(self, resume_id):
        """Retrieve full resume details by ID for admin viewing."""
        with self.lock:
            resumes = self._read(self.resumes_path)
            for r in resumes:
                if str(r.get('_id')) == str(resume_id):
                    return r
            return None

    def get_latest_resume(self, email):
        user_resumes = self.get_resumes_by_user(email)
        return user_resumes[0] if user_resumes else None

    def get_all_resumes_for_account(self, account_key):
        """Return all historical versions generated by an account, latest first."""
        if not account_key:
            return []
        account_clean = account_key.strip().lower()
        with self.lock:
            resumes = self._read(self.resumes_path)
            matching = [
                r for r in resumes
                if self._account_key_for_resume(r) == account_clean
            ]
            matching.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
            return matching

    def get_unique_active_resumes(self):
        """
        Identify unique accounts and return exactly ONE latest active resume per account.
        Duplicate generation records are not counted as separate active resumes.
        """
        with self.lock:
            resumes = self._read(self.resumes_path)
            grouped = {}
            for r in resumes:
                key = self._account_key_for_resume(r)
                grouped.setdefault(key, []).append(r)

            active_resumes = []
            for acct_key, items in grouped.items():
                items.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
                latest = items[0].copy()
                latest['account_key'] = acct_key
                latest['version_count'] = len(items)
                latest['is_latest'] = True
                active_resumes.append(latest)

            active_resumes.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
            return active_resumes

    def count_active_resumes(self):
        """Count unique active resumes (one per registered account)."""
        return len(self.get_unique_active_resumes())

    def count_total_resumes_generated(self):
        """Count total historical resume-generation records."""
        with self.lock:
            resumes = self._read(self.resumes_path)
            return len(resumes)

    def count_resumes(self):
        """Alias for count_active_resumes to preserve compatibility."""
        return self.count_active_resumes()

    def save_resume(self, resume_id, form_data):
        with self.lock:
            resumes = self._read(self.resumes_path)
            if resume_id:
                for r in resumes:
                    if str(r.get('_id')) == str(resume_id) and self._account_key_for_resume(r) == self._account_key_for_resume(form_data):
                        r.update(form_data)
                        self._write(self.resumes_path, resumes)
                        return resume_id

            new_id = uuid.uuid4().hex
            form_data['_id'] = new_id
            resumes.append(form_data)
            self._write(self.resumes_path, resumes)
            return new_id

    def delete_resume(self, resume_id, email):
        email_clean = (email or '').strip().lower()
        with self.lock:
            resumes = self._read(self.resumes_path)
            new_resumes = [
                r for r in resumes
                if not (str(r.get('_id')) == str(resume_id) and self._account_key_for_resume(r) == email_clean)
            ]
            if len(new_resumes) != len(resumes):
                self._write(self.resumes_path, new_resumes)
                return True
            return False

    def get_all_resumes_paginated(self, page, per_page):
        with self.lock:
            resumes = self._read(self.resumes_path)
            resumes_copy = [r.copy() for r in resumes]
            resumes_copy.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
            start = (page - 1) * per_page
            return resumes_copy[start:start + per_page]

    def get_resume_counts_by_user(self):
        with self.lock:
            resumes = self._read(self.resumes_path)
            counts = {}
            for r in resumes:
                email = self._account_key_for_resume(r)
                if email:
                    counts[email] = counts.get(email, 0) + 1
            return counts

    def get_latest_resume_dates_by_user(self):
        with self.lock:
            resumes = self._read(self.resumes_path)
            latest = {}
            for r in resumes:
                email = self._account_key_for_resume(r)
                ts = r.get('timestamp', '')
                if email and ts:
                    if email not in latest or ts > latest[email]:
                        latest[email] = ts
            return latest

    # ------------------------------------------------------------------
    # Users Operations (Deduplicated Unique Accounts)
    # ------------------------------------------------------------------
    def _get_consolidated_users_map(self):
        """
        Deduplicate and consolidate unique accounts.
        Normalizes all emails to lowercase/stripped.
        If an account has multiple records, consolidates them using the latest valid information.
        Preserves different accounts even if their names are identical.
        Also includes accounts that exist in resumes.
        """
        raw_users = self._read(self.users_path)
        raw_resumes = self._read(self.resumes_path)

        consolidated = {}
        for raw_email, u in raw_users.items():
            clean = (raw_email or '').strip().lower()
            if not clean:
                continue
            if clean not in consolidated:
                consolidated[clean] = {
                    'email': clean,
                    'name': (u.get('name') or '').strip(),
                    'google_id': u.get('google_id', ''),
                    'last_seen': u.get('last_seen', ''),
                    'created_at': u.get('created_at', '')
                }
            else:
                c = consolidated[clean]
                if not c['name'] and u.get('name'):
                    c['name'] = u.get('name').strip()
                if not c['google_id'] and u.get('google_id'):
                    c['google_id'] = u.get('google_id')
                c['last_seen'] = max(c['last_seen'], u.get('last_seen', ''))
                c['created_at'] = min(c['created_at'] or '9999', u.get('created_at', '') or '9999')

        for r in raw_resumes:
            clean = (r.get('user_email') or r.get('personal_details', {}).get('email') or '').strip().lower()
            if not clean:
                continue
            r_name = (r.get('user_name') or r.get('personal_details', {}).get('name') or '').strip()
            r_ts = r.get('timestamp', '')
            if clean not in consolidated:
                consolidated[clean] = {
                    'email': clean,
                    'name': r_name,
                    'google_id': '',
                    'last_seen': r_ts,
                    'created_at': r_ts
                }
            else:
                c = consolidated[clean]
                if not c['name'] and r_name:
                    c['name'] = r_name
                if r_ts:
                    c['last_seen'] = max(c['last_seen'], r_ts)

        return consolidated

    def update_user(self, email, name, google_id):
        if not email:
            return False
        clean = email.strip().lower()
        with self.lock:
            users = self._read(self.users_path)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            is_returning = clean in users
            if is_returning:
                if name:
                    users[clean]['name'] = name
                if google_id:
                    users[clean]['google_id'] = google_id
                users[clean]['last_seen'] = now_str
            else:
                users[clean] = {
                    'name': name or '',
                    'google_id': google_id or '',
                    'last_seen': now_str,
                    'created_at': now_str
                }
            self._write(self.users_path, users)
            return is_returning

    def get_user(self, email):
        if not email:
            return None
        clean = email.strip().lower()
        with self.lock:
            users_map = self._get_consolidated_users_map()
            u = users_map.get(clean)
            if u:
                u_copy = u.copy()
                u_copy['_id'] = clean
                return u_copy
            return None

    def count_users(self):
        with self.lock:
            return len(self._get_consolidated_users_map())

    def get_all_users_paginated(self, page, per_page):
        with self.lock:
            users_map = self._get_consolidated_users_map()
            user_list = list(users_map.values())
            user_list.sort(key=lambda x: str(x.get('last_seen', '')), reverse=True)
            start = (page - 1) * per_page
            return user_list[start:start + per_page]

    def get_all_users_with_resume_counts(self, page, per_page):
        counts = self.get_resume_counts_by_user()
        latest_dates = self.get_latest_resume_dates_by_user()
        with self.lock:
            users_map = self._get_consolidated_users_map()
            user_list = []
            for clean_email, u in users_map.items():
                u_copy = u.copy()
                u_copy['_id'] = clean_email
                u_copy['resume_count'] = counts.get(clean_email, 0)
                u_copy['latest_resume_date'] = latest_dates.get(clean_email, '—')
                user_list.append(u_copy)
            user_list.sort(key=lambda x: str(x.get('last_seen', '')), reverse=True)
            total = len(user_list)
            start = (page - 1) * per_page
            return user_list[start:start + per_page], total

    # ------------------------------------------------------------------
    # Stats Operations
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Logs Operations
    # ------------------------------------------------------------------
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
            return logs[start:start + per_page]

    def cleanup_old_logs(self):
        cutoff = datetime.now() - timedelta(days=15)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            logs = self._read(self.logs_path)
            new_logs = [l for l in logs if l.get('timestamp', '') >= cutoff_str]
            deleted_count = len(logs) - len(new_logs)
            if deleted_count > 0:
                self._write(self.logs_path, new_logs)
            return deleted_count

    # ------------------------------------------------------------------
    # Feedback Operations (Deduplicated Submissions & Clean Management)
    # ------------------------------------------------------------------
    def save_feedback(self, feedback):
        with self.lock:
            feedbacks = self._read(self.feedback_path)
            clean_email = (feedback.get('email') or '').strip().lower()
            rating = str(feedback.get('rating', '5'))
            comment = (feedback.get('comment') or '').strip()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Check if exact identical feedback was submitted recently (within 24 hours) by same user
            duplicate_found = False
            for fb in feedbacks:
                fb_email = (fb.get('email') or '').strip().lower()
                fb_rating = str(fb.get('rating', '5'))
                fb_comment = (fb.get('comment') or '').strip()
                if fb_email == clean_email and fb_rating == rating and fb_comment == comment:
                    try:
                        ts1 = datetime.strptime(fb.get('timestamp', ''), "%Y-%m-%d %H:%M:%S")
                        ts2 = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
                        if abs((ts2 - ts1).total_seconds()) < 86400:
                            fb['timestamp'] = now_str
                            fb['duplicate_count'] = fb.get('duplicate_count', 1) + 1
                            duplicate_found = True
                            break
                    except Exception:
                        pass

            if not duplicate_found:
                feedback['_id'] = uuid.uuid4().hex
                feedback['email'] = clean_email
                feedback['timestamp'] = feedback.get('timestamp') or now_str
                feedback['duplicate_count'] = 1
                feedbacks.append(feedback)

            self._write(self.feedback_path, feedbacks)

    def get_all_feedback(self):
        """
        Return clean, deduplicated feedback records.
        Identifies exact duplicate submissions (same user, rating, comment within 24h)
        while preserving legitimate multiple reviews from different dates or with different ratings/comments.
        Enriches records with current user name.
        """
        with self.lock:
            raw_feedbacks = self._read(self.feedback_path)
            users_map = self._get_consolidated_users_map()

            for fb in raw_feedbacks:
                if not fb.get('_id'):
                    fb['_id'] = uuid.uuid4().hex

            groups = {}
            for fb in raw_feedbacks:
                email = (fb.get('email') or '').strip().lower()
                rating = str(fb.get('rating', '5'))
                comment = (fb.get('comment') or '').strip()
                key = (email, rating, comment)
                groups.setdefault(key, []).append(fb)

            deduped = []
            for key, items in groups.items():
                items.sort(key=lambda x: str(x.get('timestamp', '')))
                clusters = []
                for it in items:
                    ts_str = it.get('timestamp', '')
                    try:
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        ts = datetime.min
                    if not clusters:
                        clusters.append([it])
                    else:
                        prev_ts_str = clusters[-1][-1].get('timestamp', '')
                        try:
                            prev_ts = datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S')
                        except Exception:
                            prev_ts = datetime.min
                        if abs((ts - prev_ts).total_seconds()) < 86400:
                            clusters[-1].append(it)
                        else:
                            clusters.append([it])

                for c in clusters:
                    chosen = c[-1].copy()
                    clean_email = (chosen.get('email') or '').strip().lower()
                    if not chosen.get('name') and clean_email in users_map:
                        chosen['name'] = users_map[clean_email].get('name', '')
                    total_dupes = sum(it.get('duplicate_count', 1) for it in c)
                    chosen['duplicate_count'] = total_dupes
                    deduped.append(chosen)

            deduped.sort(key=lambda x: str(x.get('timestamp', '')), reverse=True)
            return deduped

    def count_feedback(self):
        return len(self.get_all_feedback())

    # ------------------------------------------------------------------
    # Authorized Admin Emails Management
    # ------------------------------------------------------------------
    def get_authorized_admins_data(self):
        with self.lock:
            data = self._read(self.admins_path)
            if not isinstance(data, dict) or 'authorized_emails' not in data:
                data = {
                    "max_allowed": 10,
                    "authorized_emails": list(config.INITIAL_ADMIN_EMAILS)
                }
                self._write(self.admins_path, data)
            return data

    def get_authorized_emails(self):
        data = self.get_authorized_admins_data()
        return [e.strip().lower() for e in data.get('authorized_emails', [])]

    def is_authorized_admin(self, email):
        if not email:
            return False
        return email.strip().lower() in self.get_authorized_emails()

    def add_authorized_email(self, email):
        if not email:
            return False, "Email address cannot be empty."
        email = email.strip().lower()
        if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
            return False, "Invalid email address format."

        with self.lock:
            data = self._read(self.admins_path)
            if not isinstance(data, dict):
                data = {
                    "max_allowed": 10,
                    "authorized_emails": list(config.INITIAL_ADMIN_EMAILS)
                }

            emails = [e.strip().lower() for e in data.get('authorized_emails', [])]
            max_allowed = int(data.get('max_allowed', 10))

            if email in emails:
                return False, f"Email '{email}' is already an authorized administrator."

            if len(emails) >= max_allowed:
                return False, f"Cannot add '{email}': Maximum limit of {max_allowed} authorized administrators reached."

            emails.append(email)
            data['authorized_emails'] = emails
            self._write(self.admins_path, data)
            return True, f"Successfully authorized '{email}'."

    def remove_authorized_email(self, email):
        if not email:
            return False, "Email address cannot be empty."
        email = email.strip().lower()

        with self.lock:
            data = self._read(self.admins_path)
            if not isinstance(data, dict):
                data = {
                    "max_allowed": 10,
                    "authorized_emails": list(config.INITIAL_ADMIN_EMAILS)
                }

            emails = [e.strip().lower() for e in data.get('authorized_emails', [])]

            if len(emails) <= 1:
                return False, "Action denied: You cannot remove the last remaining administrator account. At least one admin is required."

            new_emails = [e for e in emails if e != email]
            if len(new_emails) == len(emails):
                return False, f"Email '{email}' is not in the authorized list."

            data['authorized_emails'] = new_emails
            self._write(self.admins_path, data)
            return True, f"Successfully removed '{email}' from authorized administrators."

    def set_admin_limit(self, limit):
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            return False, "Limit must be a valid positive integer."

        if limit < 1:
            return False, "Limit must be at least 1."

        with self.lock:
            data = self._read(self.admins_path)
            if not isinstance(data, dict):
                data = {
                    "max_allowed": 10,
                    "authorized_emails": list(config.INITIAL_ADMIN_EMAILS)
                }

            current_count = len(data.get('authorized_emails', []))
            if limit < current_count:
                return False, f"Limit cannot be lower than the current number of authorized emails ({current_count})."

            data['max_allowed'] = limit
            self._write(self.admins_path, data)
            return True, f"Authorized email limit updated to {limit}."

    # ------------------------------------------------------------------
    # Storage Monitoring
    # ------------------------------------------------------------------
    def get_storage_info(self):
        files = {
            'resumes': self.resumes_path,
            'users': self.users_path,
            'stats': self.stats_path,
            'feedback': self.feedback_path,
            'logs': self.logs_path,
            'admins': self.admins_path,
        }
        info = {}
        total_bytes = 0
        for name, path in files.items():
            if path.exists():
                size = path.stat().st_size
                total_bytes += size
                info[name] = round(size / (1024 * 1024), 4)
            else:
                info[name] = 0.0

        # Check temporary PDFs directory size
        pdf_bytes = 0
        if config.PDF_TEMP_DIR.exists():
            for f in config.PDF_TEMP_DIR.iterdir():
                if f.is_file():
                    pdf_bytes += f.stat().st_size
        info['temp_pdfs'] = round(pdf_bytes / (1024 * 1024), 4)
        total_bytes += pdf_bytes

        total_mb = round(total_bytes / (1024 * 1024), 4)
        status = 'normal'
        if total_mb >= config.STORAGE_MAX_MB:
            status = 'critical'
        elif total_mb >= config.STORAGE_CRITICAL_MB:
            status = 'warning'
        elif total_mb >= config.STORAGE_WARN_MB:
            status = 'monitor'

        return {
            'files': info,
            'total_mb': total_mb,
            'warn_mb': config.STORAGE_WARN_MB,
            'critical_mb': config.STORAGE_CRITICAL_MB,
            'max_mb': config.STORAGE_MAX_MB,
            'status': status,
        }


# Initialize DB singleton
db = JSONDatabase()


# ===========================================================================
# Email Delivery Function
# ===========================================================================
def send_email_with_attachment(to_email, pdf_bytes, filename, user_name=None):
    """
    Deliver the resume PDF attachment via SMTP email.
    Synchronous execution with retry & timeout.
    Returns True only upon successful delivery.
    """
    if not to_email:
        logger.error("send_email_with_attachment: recipient email is missing")
        return False

    if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
        logger.error("send_email_with_attachment: SMTP credentials not configured")
        return False

    greeting = f"Dear {user_name}," if user_name else "Dear Candidate,"
    subject = (f"Your Professional Resume – {user_name}"
               if user_name else "Your Professional Resume – Resumora")

    body_text = f"""{greeting}

Thank you for choosing Resumora. Your professional, ATS-optimized resume has been successfully generated and is attached to this email.

We wish you the very best in your career pursuits!

Best Regards,
Resumora
AIVONTRAA AUTOMATION PVT. LTD.
Email: {config.EMAIL_FROM_ADDRESS}
Phone: +91 7893772246
"""

    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Professional Resume</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color:#f4f5f7;padding:24px 0;">
    <tr><td align="center">
      <table border="0" cellpadding="0" cellspacing="0" width="100%"
        style="max-width:600px;background-color:#ffffff;border-radius:12px;overflow:hidden;
               box-shadow:0 4px 6px -1px rgba(0,0,0,.08);border:1px solid #e5e7eb;">
        <tr><td style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);padding:36px 30px;text-align:center;">
          <h1 style="color:#ffffff;margin:0;font-size:26px;font-weight:800;letter-spacing:-0.5px;">Resumora</h1>
          <p style="color:#94a3b8;margin:6px 0 0;font-size:14px;">Professional ATS Resume Delivery</p>
        </td></tr>
        <tr><td style="padding:36px 30px;">
          <h2 style="color:#111827;margin-top:0;font-size:19px;font-weight:700;">{greeting}</h2>
          <p style="color:#4b5563;font-size:15px;line-height:1.6;">
            Your professional resume has been successfully generated and compiled into an ATS-compliant PDF document. It is attached directly to this email for your career applications.
          </p>
          <div style="background:#f8fafc;border-left:4px solid #0f172a;border-radius:4px;padding:14px 18px;margin:20px 0;">
            <p style="margin:0;font-size:13px;color:#64748b;font-weight:600;text-transform:uppercase;">Attached Document:</p>
            <p style="margin:4px 0 0;font-size:15px;color:#0f172a;font-family:monospace;font-weight:600;">{filename}</p>
          </div>
          <p style="color:#4b5563;font-size:15px;line-height:1.6;">
            We wish you the greatest success with your applications!
          </p>
          <table border="0" cellpadding="0" cellspacing="0" width="100%"
            style="border-top:1px solid #e5e7eb;padding-top:20px;margin-top:24px;">
            <tr><td>
              <p style="margin:0;color:#9ca3af;font-size:12px;text-transform:uppercase;font-weight:600;">Best Regards,</p>
              <p style="margin:4px 0 0;color:#111827;font-size:16px;font-weight:700;">Resumora</p>
              <p style="margin:2px 0 0;color:#64748b;font-size:13px;">AIVONTRAA AUTOMATION PVT. LTD.</p>
            </td></tr>
          </table>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:20px 30px;text-align:center;border-top:1px solid #e5e7eb;">
          <p style="margin:0;color:#64748b;font-size:13px;">
            Support: <a href="mailto:{config.EMAIL_FROM_ADDRESS}" style="color:#0f172a;font-weight:600;text-decoration:none;">{config.EMAIL_FROM_ADDRESS}</a>
            &nbsp;|&nbsp; +91 7893772246
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    for attempt in range(1, config.SMTP_MAX_RETRIES + 1):
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM_ADDRESS}>"
            msg['To'] = to_email
            msg['Bcc'] = config.EMAIL_FROM_ADDRESS
            msg.set_content(body_text)
            msg.add_alternative(html_content, subtype='html')
            msg.add_attachment(
                pdf_bytes,
                maintype='application',
                subtype='pdf',
                filename=filename
            )

            if config.SMTP_USE_SSL:
                smtp_cls = smtplib.SMTP_SSL
                connect_args = (config.SMTP_HOST, config.SMTP_PORT, None, None, config.SMTP_TIMEOUT)
            else:
                smtp_cls = smtplib.SMTP
                connect_args = (config.SMTP_HOST, config.SMTP_PORT, None, config.SMTP_TIMEOUT)

            with smtp_cls(*connect_args) as smtp:
                if not config.SMTP_USE_SSL:
                    smtp.starttls()
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                smtp.send_message(msg)

            logger.info(f"Resume email successfully delivered to {to_email} (attempt {attempt})")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication failure for {to_email}: {e}")
            return False

        except Exception as e:
            logger.warning(f"Email attempt {attempt}/{config.SMTP_MAX_RETRIES} failed for {to_email}: {e}")
            if attempt < config.SMTP_MAX_RETRIES:
                time.sleep(config.SMTP_RETRY_DELAY)
            else:
                logger.error(f"All {config.SMTP_MAX_RETRIES} email attempts failed for {to_email}: {e}")
                return False

    return False


# ===========================================================================
# Helper Functions
# ===========================================================================
def track_stat(stat_key):
    try:
        db.track_stat(stat_key)
    except Exception as e:
        logger.error(f"Error tracking stat {stat_key}: {e}")


def cleanup_old_logs():
    try:
        deleted = db.cleanup_old_logs()
        logger.info(f"Cleaned up {deleted} old log entries")
    except Exception as e:
        logger.error(f"Error cleaning up logs: {e}")


def log_event(event_type, email, data=None):
    try:
        db.log_event(event_type, email, data)
        if datetime.now().minute % 15 == 0:
            threading.Thread(target=cleanup_old_logs, daemon=True).start()
    except Exception as e:
        logger.error(f"Error logging event: {e}")


def validate_form_data(form_data):
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


# ===========================================================================
# Template Context Processor
# ===========================================================================
@app.context_processor
def inject_global_template_vars():
    return dict(
        ICON_WEBSITE=constants.ICON_WEBSITE,
        ICON_LINKEDIN=constants.ICON_LINKEDIN,
        ICON_GITHUB=constants.ICON_GITHUB,
        ICON_EMAIL=constants.ICON_EMAIL,
        ICON_PHONE=constants.ICON_PHONE,
        ICON_LOCATION=constants.ICON_LOCATION,
        APP_ENV=config.APP_ENV,
        GOOGLE_CLIENT_ID=config.GOOGLE_CLIENT_ID,
    )


# ===========================================================================
# Authentication Decorators
# ===========================================================================
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        admin_email = session.get('admin_email')
        if not session.get('admin_logged_in') or not admin_email:
            flash("Please log in to access the administrator dashboard.", 'error')
            return redirect(url_for('admin_login'))

        # Backend verification: verify that the session email remains authorized
        if not db.is_authorized_admin(admin_email):
            session.pop('admin_logged_in', None)
            session.pop('admin_email', None)
            flash("Your administrator authorization has been revoked.", 'error')
            return redirect(url_for('admin_login'))

        return f(*args, **kwargs)
    return decorated


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_email'):
            return redirect(url_for('index', login='required'))
        return f(*args, **kwargs)
    return decorated


# ===========================================================================
# PWA & Favicon Delivery Endpoints
# ===========================================================================
@app.route('/favicon.ico')
def favicon():
    return send_file(config.BASE_DIR / 'static' / 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/favicon.svg')
def favicon_svg():
    return send_file(config.BASE_DIR / 'static' / 'favicon.svg', mimetype='image/svg+xml')

@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    return send_file(config.BASE_DIR / 'static' / 'apple-touch-icon.png', mimetype='image/png')

@app.route('/icon-192.png')
def icon_192():
    return send_file(config.BASE_DIR / 'static' / 'icon-192.png', mimetype='image/png')

@app.route('/icon-512.png')
def icon_512():
    return send_file(config.BASE_DIR / 'static' / 'icon-512.png', mimetype='image/png')

@app.route('/manifest.json')
@app.route('/site.webmanifest')
def manifest():
    return send_file(config.BASE_DIR / 'static' / 'manifest.json', mimetype='application/manifest+json')


# ===========================================================================
# Public & User Routes
# ===========================================================================
@app.route('/')
def index():
    track_stat("visits")
    return render_template('welcome.html', google_client_id=config.GOOGLE_CLIENT_ID)


@app.route('/dashboard')
def dashboard():
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
    if not session.get('user_email'):
        return redirect(url_for('index', login='required'))
    return render_template('builder.html', resume_id=resume_id)


@app.route('/thank-you')
def thank_you():
    email = request.args.get('email', session.get('user_email', ''))
    return render_template('thank_you.html', email=email)


@app.route('/get_resume/<resume_id>')
def get_resume(resume_id):
    if not session.get('user_email'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        resume = db.get_resume_by_id(resume_id, session.get('user_email'))
        if resume:
            return app.response_class(
                response=json.dumps(resume),
                status=200,
                mimetype='application/json'
            )
        return jsonify({"status": "error", "message": "Resume not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching resume {resume_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/last_resume')
def last_resume():
    if not session.get('user_email'):
        return app.response_class(response='{}', mimetype='application/json')
    try:
        last = db.get_latest_resume(session.get('user_email'))
        return app.response_class(
            response=json.dumps(last or {}),
            mimetype='application/json'
        )
    except Exception as e:
        logger.error(f"Error fetching last resume: {e}")
        return app.response_class(response='{}', mimetype='application/json')


@app.route('/save', methods=['POST'])
def save_data():
    try:
        data = request.get_json()
        resume_id = data.get('resume_id')
        form_data = {
            'personal_details': data.get('personal_details', {}),
            'professional_details': data.get('professional_details', {}),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'user_email': session.get('user_email'),
            'user_name': session.get('user_name'),
        }
        validate_form_data(form_data)
        inserted_id = db.save_resume(resume_id, form_data)
        threading.Thread(target=track_stat, args=("form_starts",), daemon=True).start()
        return jsonify({"status": "success", "message": "Saved", "resume_id": inserted_id})
    except Exception as e:
        logger.error(f"Error in /save: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


# ===========================================================================
# PDF Generation & Email Delivery (No Direct Download / No Public Preview)
# ===========================================================================
@app.route('/generate-and-email', methods=['POST'])
@app.route('/download-pdf', methods=['POST'])  # Intercept legacy POST requests
def generate_and_email_resume():
    """
    Generate resume PDF temporarily on the backend and deliver strictly via email.
    Does not provide direct downloads or browser preview.
    Cleans up temporary PDF files immediately after delivery.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        personal = data.get('personal_details', {})
        prof = data.get('professional_details', {})

        user_email = session.get('user_email') or personal.get('email')
        if not user_email:
            return jsonify({"status": "error", "message": "Destination email address is required"}), 400

        # Save or update resume record
        form_data = {
            'personal_details': personal,
            'professional_details': prof,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'user_email': session.get('user_email', user_email),
            'user_name': session.get('user_name', personal.get('name', 'User')),
        }
        try:
            validate_form_data(form_data)
            resume_id = data.get('resume_id')
            db.save_resume(resume_id, form_data)
        except Exception as e:
            logger.warning(f"Resume validation warning: {e}")

        # Generate PDF bytes
        pdf_bytes = generate_resume_pdf(
            personal, prof,
            render_template_fn=render_template,
            host_url=request.host_url
        )
        if not pdf_bytes:
            return jsonify({"status": "error", "message": "Failed to compile resume PDF"}), 500

        # Save temporarily on server
        temp_id = uuid.uuid4().hex
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', personal.get('name', 'Resume').strip())
        filename = f"{clean_name}_Resume.pdf"
        temp_pdf_path = config.PDF_TEMP_DIR / f"{temp_id}_{filename}"

        try:
            with open(temp_pdf_path, 'wb') as f:
                f.write(pdf_bytes)

            # Read back from temp file to verify file integrity
            with open(temp_pdf_path, 'rb') as f:
                content_to_send = f.read()

            # Synchronous email delivery
            email_sent = send_email_with_attachment(
                to_email=user_email,
                pdf_bytes=content_to_send,
                filename=filename,
                user_name=personal.get('name')
            )

            if not email_sent:
                logger.error(f"Email delivery failed for recipient: {user_email}")
                return jsonify({
                    "status": "error",
                    "message": f"Could not send email to {user_email}. Please verify your email settings or try again."
                }), 500

            # Delivery succeeded
            track_stat("resumes_generated")
            log_event('resume_delivered_email', user_email, {'filename': filename})

            return jsonify({
                "status": "success",
                "message": f"Your resume has been successfully sent to {user_email}!",
                "recipient": user_email
            }), 200

        finally:
            # Clean up temporary PDF file from server
            if temp_pdf_path.exists():
                try:
                    temp_pdf_path.unlink()
                    logger.debug(f"Deleted temporary PDF: {temp_pdf_path}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete temp PDF {temp_pdf_path}: {cleanup_err}")

    except Exception as e:
        logger.error(f"Error in generate_and_email_resume: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/email-resume/<resume_id>', methods=['POST'])
def email_resume_by_id(resume_id):
    """Deliver an existing saved resume to the user's email."""
    user_email = session.get('user_email')
    if not user_email:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        resume = db.get_resume_by_id(resume_id, user_email)
        if not resume:
            return jsonify({"status": "error", "message": "Resume not found"}), 404

        personal = resume.get('personal_details', {})
        prof = resume.get('professional_details', {})

        pdf_bytes = generate_resume_pdf(
            personal, prof,
            render_template_fn=render_template,
            host_url=request.host_url
        )
        if not pdf_bytes:
            return jsonify({"status": "error", "message": "Failed to compile resume PDF"}), 500

        temp_id = uuid.uuid4().hex
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', personal.get('name', 'Resume').strip())
        filename = f"{clean_name}_Resume.pdf"
        temp_pdf_path = config.PDF_TEMP_DIR / f"{temp_id}_{filename}"

        try:
            with open(temp_pdf_path, 'wb') as f:
                f.write(pdf_bytes)

            with open(temp_pdf_path, 'rb') as f:
                content_to_send = f.read()

            email_sent = send_email_with_attachment(
                to_email=user_email,
                pdf_bytes=content_to_send,
                filename=filename,
                user_name=personal.get('name')
            )

            if not email_sent:
                return jsonify({
                    "status": "error",
                    "message": f"Could not deliver email to {user_email}. Please check SMTP configuration."
                }), 500

            track_stat("resumes_generated")
            return jsonify({
                "status": "success",
                "message": f"Resume PDF successfully emailed to {user_email}!"
            }), 200

        finally:
            if temp_pdf_path.exists():
                try:
                    temp_pdf_path.unlink()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Error emailing resume {resume_id}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# Block all direct download and preview URLs
@app.route('/download-pdf', methods=['GET'])
@app.route('/download-pdf/<resume_id>', methods=['GET'])
def download_pdf_disabled(resume_id=None):
    return jsonify({
        "status": "error",
        "message": "Direct PDF downloads and previews are disabled. Resumes are delivered exclusively via email."
    }), 403


@app.route('/delete-resume/<resume_id>', methods=['DELETE'])
def delete_resume(resume_id):
    if not session.get('user_email'):
        return "Unauthorized", 401
    try:
        if db.delete_resume(resume_id, session.get('user_email')):
            return "Success", 200
        return "Not found or unauthorized", 404
    except Exception as e:
        logger.error(f"Error deleting resume {resume_id}: {e}")
        return str(e), 500


# ---------------------------------------------------------------------------
# Google Auth & User Management
# ---------------------------------------------------------------------------
@app.route('/auth/google', methods=['POST'])
def auth_google():
    try:
        data = request.get_json()
        token = data.get('token')
        if not token:
            return jsonify({"status": "error", "message": "No token provided"}), 400

        idinfo = id_token.verify_oauth2_token(
            token, google_auth_requests.Request(), config.GOOGLE_CLIENT_ID
        )

        userid = idinfo['sub']
        email = idinfo.get('email', '').strip().lower()
        name = idinfo.get('name', 'User')

        session['user_id'] = userid
        session['user_email'] = email
        session['user_name'] = name
        session.permanent = True

        # If this email is also an authorized admin, establish admin session
        if db.is_authorized_admin(email):
            session['admin_logged_in'] = True
            session['admin_email'] = email

        existing_user = db.get_user(email)
        is_returning = existing_user is not None

        db.update_user(email, name, userid)
        threading.Thread(target=log_event, args=('login', email), daemon=True).start()

        return jsonify({
            "status": "success",
            "returning": is_returning,
            "redirect": "/dashboard"
        })
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 401


@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('user_name', None)
    flash("You have been logged out", 'success')
    return redirect(url_for('index'))


@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    try:
        data = request.get_json()
        feedback = {
            'email': session.get('user_email', 'guest'),
            'name': session.get('user_name', ''),
            'rating': data.get('rating'),
            'comment': data.get('comment', ''),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        db.save_feedback(feedback)
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Error submitting feedback: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ===========================================================================
# Admin Authentication (Email-Based Authentication Only)
# ===========================================================================
@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    # If already logged in as authorized admin, redirect to dashboard
    if session.get('admin_logged_in') and db.is_authorized_admin(session.get('admin_email')):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email:
            flash("Administrator email address is required.", 'error')
            return render_template('admin_login.html'), 400

        # Validate that email is in the authorized admin list
        if not db.is_authorized_admin(email):
            logger.warning(f"Unauthorized admin login attempt: {email}")
            flash("This email address is not authorized for administrator access.", 'error')
            return render_template('admin_login.html'), 403

        # Validate password
        if password != config.ADMIN_PASSWORD:
            logger.warning(f"Invalid admin password attempt for: {email}")
            flash("Invalid administrator credentials.", 'error')
            return render_template('admin_login.html'), 401

        # Successful email-based admin login
        session['admin_logged_in'] = True
        session['admin_email'] = email
        session.permanent = True
        log_event('admin_login_success', email)
        flash("Welcome to the Admin Dashboard!", 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_login.html')


@app.route('/admin-dashboard')
@admin_required
def admin_dashboard():
    try:
        view_mode = request.args.get('view_mode', 'active')  # 'active' (default deduplicated) or 'history'
        res_page = max(1, int(request.args.get('res_page', 1)))
        u_page = max(1, int(request.args.get('u_page', 1)))
        l_page = max(1, int(request.args.get('l_page', 1)))
        per_page = int(request.args.get('per_page', 25))

        # Synchronized Metrics
        total_unique_users = db.count_users()
        current_resumes_count = db.count_active_resumes()
        total_resumes_generated = db.count_total_resumes_generated()

        # Resumes Pane
        if view_mode == 'history':
            res_total = total_resumes_generated
            resumes_slice = db.get_all_resumes_paginated(res_page, per_page)
            for r in resumes_slice:
                r['account_key'] = db._account_key_for_resume(r)
        else:
            active_all = db.get_unique_active_resumes()
            res_total = len(active_all)
            start = (res_page - 1) * per_page
            resumes_slice = active_all[start:start + per_page]

        # Users Pane
        users_with_counts, u_total = db.get_all_users_with_resume_counts(u_page, per_page)

        # Logs Pane
        l_total = db.count_logs()
        logs = db.get_all_logs_paginated(l_page, per_page)

        # Feedback
        feedbacks = db.get_all_feedback()

        # Storage
        storage = db.get_storage_info()

        # Authorized Admins Data
        admins_data = db.get_authorized_admins_data()
        current_admin = session.get('admin_email', '')

        return render_template(
            'admin_dashboard.html',
            resumes=resumes_slice,
            res_total=res_total,
            res_page=res_page,
            view_mode=view_mode,
            current_resumes_count=current_resumes_count,
            total_resumes_generated=total_resumes_generated,
            total_unique_users=total_unique_users,
            users=users_with_counts,
            u_total=u_total,
            u_page=u_page,
            logs=logs,
            l_total=l_total,
            l_page=l_page,
            feedbacks=feedbacks,
            per_page=per_page,
            storage=storage,
            admins_data=admins_data,
            current_admin=current_admin,
            app_env=config.APP_ENV,
        )
    except Exception as e:
        logger.error(f"Error loading admin dashboard: {e}", exc_info=True)
        flash("Error loading admin dashboard", 'error')
        return redirect(url_for('admin_login'))


# ===========================================================================
# Admin Authorized Email Management Endpoints
# ===========================================================================
@app.route('/admin/authorized-emails', methods=['GET'])
@admin_required
def get_authorized_emails_api():
    try:
        return jsonify(db.get_authorized_admins_data()), 200
    except Exception as e:
        logger.error(f"Error fetching authorized emails: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/admin/authorized-emails/add', methods=['POST'])
@admin_required
def add_authorized_email_api():
    try:
        data = request.get_json(silent=True) or request.form
        email = data.get('email', '').strip()
        success, message = db.add_authorized_email(email)
        status_code = 200 if success else 400
        return jsonify({
            "status": "success" if success else "error",
            "message": message,
            "data": db.get_authorized_admins_data()
        }), status_code
    except Exception as e:
        logger.error(f"Error adding authorized email: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/admin/authorized-emails/remove', methods=['POST'])
@admin_required
def remove_authorized_email_api():
    try:
        data = request.get_json(silent=True) or request.form
        email = data.get('email', '').strip()
        success, message = db.remove_authorized_email(email)
        status_code = 200 if success else 400
        return jsonify({
            "status": "success" if success else "error",
            "message": message,
            "data": db.get_authorized_admins_data()
        }), status_code
    except Exception as e:
        logger.error(f"Error removing authorized email: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/admin/authorized-emails/limit', methods=['POST'])
@admin_required
def set_admin_limit_api():
    try:
        data = request.get_json(silent=True) or request.form
        limit = data.get('limit')
        success, message = db.set_admin_limit(limit)
        status_code = 200 if success else 400
        return jsonify({
            "status": "success" if success else "error",
            "message": message,
            "data": db.get_authorized_admins_data()
        }), status_code
    except Exception as e:
        logger.error(f"Error updating admin limit: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ===========================================================================
# Admin Resume Inspection Endpoints (Complete Viewing & History)
# ===========================================================================
@app.route('/admin/resume/<resume_id>', methods=['GET'])
@admin_required
def get_admin_resume(resume_id):
    """Return complete details for a specific resume, plus its user's version history."""
    try:
        resume = db.get_resume_by_id_admin(resume_id)
        if not resume:
            return jsonify({"status": "error", "message": "Resume not found"}), 404

        account_key = db._account_key_for_resume(resume)
        all_user_resumes = db.get_all_resumes_for_account(account_key)

        version_list = [
            {
                "_id": r.get('_id'),
                "timestamp": r.get('timestamp', ''),
                "name": r.get('personal_details', {}).get('name', 'Resume') if r.get('personal_details') else 'Resume',
                "is_current": (idx == 0)
            }
            for idx, r in enumerate(all_user_resumes)
        ]

        return jsonify({
            "status": "success",
            "resume": resume,
            "account_key": account_key,
            "version_count": len(all_user_resumes),
            "versions": version_list
        }), 200
    except Exception as e:
        logger.error(f"Error retrieving resume {resume_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/admin/user-resumes/<path:account_key>', methods=['GET'])
@admin_required
def get_admin_user_resumes(account_key):
    """Return all historical versions generated by a specific user account."""
    try:
        resumes = db.get_all_resumes_for_account(account_key)
        return jsonify({
            "status": "success",
            "account_key": account_key,
            "total_versions": len(resumes),
            "resumes": resumes
        }), 200
    except Exception as e:
        logger.error(f"Error retrieving user resumes for {account_key}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ===========================================================================
# Admin Statistics & Analytics
# ===========================================================================
@app.route('/admin/stats')
@admin_required
def admin_stats():
    try:
        stats = db.get_all_stats()
        total_visits = sum(s.get('visits', 0) for s in stats)
        total_starts = sum(s.get('form_starts', 0) for s in stats)
        total_unique_users = db.count_users()
        current_resumes = db.count_active_resumes()
        total_resumes_generated = db.count_total_resumes_generated()
        total_feedback = db.count_feedback()
        daily_stats = {s['_id']: s for s in stats}

        return jsonify({
            "total_visits": total_visits,
            "total_starts": total_starts,
            "total_unique_users": total_unique_users,
            "current_resumes": current_resumes,
            "total_resumes_generated": total_resumes_generated,
            "total_feedback": total_feedback,
            "total_users": total_unique_users,
            "total_resumes": current_resumes,
            "total_generated": total_resumes_generated,
            "daily_stats": daily_stats,
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/admin/storage')
@admin_required
def admin_storage():
    try:
        return jsonify(db.get_storage_info())
    except Exception as e:
        logger.error(f"Error getting storage info: {e}")
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# Admin CSV Export (Fixed & Deduplicated)
# ===========================================================================
@app.route('/admin/export-csv')
@admin_required
def export_csv():
    """
    Export resumes CSV without duplicate rows.
    Defaults to exporting unique accounts and their latest active resumes.
    Pass mode=history to export all historical generations.
    """
    try:
        mode = request.args.get('mode', 'active')
        if mode == 'history':
            resumes = db.get_all_resumes_paginated(1, 1_000_000)
            filename = f"resumes_all_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        else:
            resumes = db.get_unique_active_resumes()
            filename = f"resumes_unique_active_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        if not resumes:
            return "No resume records found to export", 404

        output = BytesIO()
        wrapper = TextIOWrapper(output, encoding='utf-8-sig', write_through=True)
        headers = [
            'Account Email',
            'Account Name',
            'Resume Full Name',
            'Contact Email',
            'Contact Phone',
            'Location',
            'Status',
            'Total Versions Generated',
            'Latest Timestamp',
            'Technical Skills',
            'Summary'
        ]
        writer = csv.DictWriter(wrapper, fieldnames=headers)
        writer.writeheader()

        for r in resumes:
            personal = r.get('personal_details', {}) or {}
            prof = r.get('professional_details', {}) or {}
            tech = prof.get('technical_skills', '')
            if isinstance(tech, list):
                tech = ', '.join(str(s) for s in tech)
            tech_clean = re.sub(r'<[^>]+>', ' ', str(tech)).replace('\r', ' ').replace('\n', ' ').strip()
            summary = prof.get('summary') or ''
            summary_clean = re.sub(r'<[^>]+>', ' ', str(summary)).replace('\r', ' ').replace('\n', ' ').strip()

            is_latest = r.get('is_latest', mode != 'history')
            v_count = r.get('version_count', 1)

            writer.writerow({
                'Account Email': r.get('user_email') or personal.get('email', 'N/A'),
                'Account Name': r.get('user_name', 'Guest'),
                'Resume Full Name': personal.get('name', 'N/A'),
                'Contact Email': personal.get('email', ''),
                'Contact Phone': personal.get('phone', ''),
                'Location': personal.get('location', ''),
                'Status': 'Current / Active' if is_latest else 'Historical',
                'Total Versions Generated': v_count,
                'Latest Timestamp': r.get('timestamp', ''),
                'Technical Skills': tech_clean,
                'Summary': summary_clean[:300],
            })
        wrapper.detach()
        output.seek(0)

        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        logger.error(f"Error exporting resumes CSV: {e}", exc_info=True)
        return f"Error exporting resumes CSV: {e}", 500


@app.route('/admin/export-users-csv')
@admin_required
def export_users_csv():
    """Export unique users with their resume count and latest activity."""
    try:
        users_raw, _ = db.get_all_users_with_resume_counts(1, 1_000_000)
        if not users_raw:
            return "No registered users found to export", 404

        output = BytesIO()
        wrapper = TextIOWrapper(output, encoding='utf-8-sig', write_through=True)
        headers = [
            'Account Email',
            'Account Name',
            'Total Resumes Generated',
            'Latest Resume Date',
            'Last Seen',
            'Account Created At',
            'Google ID'
        ]
        writer = csv.DictWriter(wrapper, fieldnames=headers)
        writer.writeheader()

        for u in users_raw:
            writer.writerow({
                'Account Email': u.get('email', ''),
                'Account Name': u.get('name', ''),
                'Total Resumes Generated': u.get('resume_count', 0),
                'Latest Resume Date': u.get('latest_resume_date', '—'),
                'Last Seen': u.get('last_seen', '—'),
                'Account Created At': u.get('created_at', '—'),
                'Google ID': u.get('google_id', ''),
            })
        wrapper.detach()
        output.seek(0)

        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
    except Exception as e:
        logger.error(f"Error exporting users CSV: {e}", exc_info=True)
        return f"Error exporting users CSV: {e}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    app.run(debug=(config.APP_ENV == 'development'), host='0.0.0.0', port=port)
