#!/usr/bin/env python3
"""
High-Speed Email Sender Backend
Robust SMTP email sender with concurrent processing and error handling
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import smtplib
import imaplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
import ssl
import time
import logging
import csv
import io
import random
import threading
import uuid
import json
import os
import datetime
import hashlib
import socket

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- Daily send quota ----
# ponytail: one JSON file + one lock, fine for a single process. Move to a
# real DB/Redis if this ever runs multiple workers or needs to survive
# concurrent writers.
QUOTA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_send_counts.json')
DEFAULT_DAILY_LIMIT = 30
# Each mailbox starts at 30 emails/day and can be configured up to 100.
HARD_DAILY_CAP = 100
_quota_lock = threading.Lock()

def _load_quota():
    try:
        with open(QUOTA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def reserve_daily_quota(from_email, count, daily_limit=None):
    """Check + reserve `count` sends against today's quota for from_email.
    Returns (ok, error_message_or_None)."""
    daily_limit = min(max(int(daily_limit or DEFAULT_DAILY_LIMIT), 1), HARD_DAILY_CAP)
    key = f"{from_email.strip().lower()}:{datetime.date.today().isoformat()}"
    with _quota_lock:
        data = _load_quota()
        used = data.get(key, 0)
        if used + count > daily_limit:
            return False, (f"Daily send limit reached: {used}/{daily_limit} already used today, "
                            f"only {max(daily_limit - used, 0)} remaining, but {count} requested.")
        data[key] = used + count
        with open(QUOTA_FILE, 'w') as f:
            json.dump(data, f)
        return True, None

def release_daily_quota(from_email, count):
    """Give back `count` reserved-but-unused sends (e.g. a cancelled job) to
    today's quota for from_email."""
    if count <= 0:
        return
    key = f"{from_email.strip().lower()}:{datetime.date.today().isoformat()}"
    with _quota_lock:
        data = _load_quota()
        data[key] = max(data.get(key, 0) - count, 0)
        with open(QUOTA_FILE, 'w') as f:
            json.dump(data, f)

# ---- Persistent send log (who got an email, and why not if they didn't) ----
SEND_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'send_log.jsonl')
_log_lock = threading.Lock()

def _append_send_log(job_id, from_email, entry):
    """entry: {'to', 'success', 'error'} - one line per attempted send."""
    record = {'job_id': job_id, 'from': from_email, 'time': datetime.datetime.now().isoformat(), **entry}
    with _log_lock:
        with open(SEND_LOG_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')

def _message_key(to_email, subject, body):
    """A stable identifier for one exact message to one recipient.
    Sender is intentionally not part of this key: changing mailbox must not
    bypass duplicate protection."""
    raw = '\x1f'.join([
        to_email.strip().lower(), subject.strip(), body.strip()
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _already_sent_keys():
    """Return keys for messages that were actually accepted by SMTP before."""
    keys = set()
    try:
        with open(SEND_LOG_FILE) as f:
            for line in f:
                try:
                    record = json.loads(line)
                    if record.get('success') and record.get('message_key'):
                        keys.add(record['message_key'])
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return keys

def split_duplicate_messages(messages):
    """Return (sendable, skipped results), including duplicates in this run."""
    known_keys = _already_sent_keys()
    seen_keys = set()
    sendable, skipped = [], []
    for item in messages:
        key = _message_key(item['to'], item['subject'], item['body'])
        if key in known_keys or key in seen_keys:
            skipped.append({
                'success': False,
                'skipped': True,
                'recipient': item['to'],
                'error': 'Duplicate email: this exact message was already sent to this recipient.',
                'message_key': key,
            })
            continue
        seen_keys.add(key)
        sendable.append({**item, 'message_key': key})
    return sendable, skipped

def smtp_is_reachable(sender):
    """Check the network against the SMTP server that the job will use."""
    try:
        with socket.create_connection((sender.smtp_server, sender.smtp_port), timeout=8):
            return True
    except OSError:
        return False

def wait_for_network(job_id, sender):
    """Pause a job while offline and retry every 15 seconds until it returns."""
    while not smtp_is_reachable(sender):
        if job_id:
            with _jobs_lock:
                job = JOBS[job_id]
                if job['cancelled'] or job['paused']:
                    return False
                job['next_send_at'] = None
                job['status'] = 'network unavailable — checking again in 15 seconds'
        time.sleep(15)
    return True

# ---- Background send jobs (needed once sends are spaced out by minutes) ----
JOBS = {}
_jobs_lock = threading.Lock()
# Context needed to resume a paused job (sender/items/from_email). Kept out
# of JOBS so it never gets serialized back in a /api/job-status response.
_JOB_CONTEXT = {}

def next_delay(index):
    """Random gap before the next send. Minimum 5 minutes, with the range
    widening as the run goes on so a long batch doesn't look robotic.
    ponytail: linear ramp, 20-min ceiling - both arbitrary, tune as needed."""
    ceiling = min(300 + index * 30, 1200)
    return random.uniform(300, ceiling)

def _run_job(job_id, sender, items, from_email, start_index=0):
    """items: list of {'to', 'subject', 'body'} dicts, sent one at a time
    with a random delay between each (see next_delay), starting at
    start_index (0 for a fresh job, len(results) when resuming a paused
    one). Checks the job's 'cancelled'/'paused' flags before each
    send/wait so Stop/Pause can interrupt it. Stop releases any unsent
    quota; Pause leaves it reserved since the job can still finish."""
    with _jobs_lock:
        results = list(JOBS[job_id]['results'])
        total = JOBS[job_id]['total']
    queued_total = len(items)
    for i in range(start_index, queued_total):
        item = items[i]
        with _jobs_lock:
            job = JOBS[job_id]
            cancelled, paused = job['cancelled'], job['paused']
        if cancelled or paused:
            break
        if i > 0:
            delay = next_delay(i)
            with _jobs_lock:
                JOBS[job_id]['status'] = f'waiting ~{int(delay / 60)} min before next send'
                JOBS[job_id]['next_send_at'] = time.time() + delay
            # Sleep in small slices so a cancel/pause mid-wait takes effect quickly
            slept = 0
            while slept < delay:
                with _jobs_lock:
                    if JOBS[job_id]['cancelled'] or JOBS[job_id]['paused']:
                        break
                time.sleep(min(2, delay - slept))
                slept += 2
            with _jobs_lock:
                cancelled, paused = JOBS[job_id]['cancelled'], JOBS[job_id]['paused']
            if cancelled or paused:
                break
        with _jobs_lock:
            JOBS[job_id]['next_send_at'] = None
        if not wait_for_network(job_id, sender):
            break
        result = sender.send_email(item['to'], item['subject'], item['body'])
        result['message_key'] = item['message_key']
        results.append(result)
        _append_send_log(job_id, from_email, result)
        with _jobs_lock:
            JOBS[job_id]['results'] = results
            JOBS[job_id]['attempted_send_count'] += 1
            JOBS[job_id]['sent'] = sum(1 for r in results if r['success'] and not r.get('skipped'))
            JOBS[job_id]['failed'] = sum(1 for r in results if not r['success'] and not r.get('skipped'))
            JOBS[job_id]['status'] = f'sent {len(results)}/{total}'

    with _jobs_lock:
        job = JOBS[job_id]
        unsent = queued_total - job['attempted_send_count']
        cancelled, paused = job['cancelled'], job['paused']
        job['next_send_at'] = None
        if cancelled:
            job['done'] = True
            job['status'] = 'stopped by user'
        elif paused:
            job['status'] = f'paused ({len(results)}/{total} sent)'
        else:
            job['done'] = True
            job['status'] = 'complete'
    if cancelled and unsent > 0:
        release_daily_quota(from_email, unsent)
        _JOB_CONTEXT.pop(job_id, None)
    elif not paused:
        _JOB_CONTEXT.pop(job_id, None)

def start_job(sender, items, from_email, skipped_results=None):
    job_id = str(uuid.uuid4())
    skipped_results = skipped_results or []
    JOBS[job_id] = {
        'total': len(items) + len(skipped_results), 'sent': 0, 'failed': 0,
        'skipped': len(skipped_results), 'results': skipped_results,
        'attempted_send_count': 0,
        'done': False, 'cancelled': False, 'paused': False, 'status': 'starting',
        'next_send_at': None,
    }
    _JOB_CONTEXT[job_id] = {'sender': sender, 'items': items, 'from_email': from_email}
    threading.Thread(target=_run_job, args=(job_id, sender, items, from_email), daemon=True).start()
    return job_id

def cancel_job(job_id):
    with _jobs_lock:
        job = JOBS.get(job_id)
        if not job:
            return False
        if job['done']:
            return False
        job['cancelled'] = True
        if job['paused']:
            # No thread is running to notice this flag — the job's loop
            # already exited when it paused. Finalize it here instead.
            unsent = len(_JOB_CONTEXT.get(job_id, {}).get('items', [])) - job['attempted_send_count']
            from_email = _JOB_CONTEXT.get(job_id, {}).get('from_email')
            job['done'] = True
            job['paused'] = False
            job['status'] = 'stopped by user'
            job['next_send_at'] = None
        else:
            unsent = 0
            from_email = None
    if unsent > 0 and from_email:
        release_daily_quota(from_email, unsent)
    _JOB_CONTEXT.pop(job_id, None)
    return True

def pause_job(job_id):
    with _jobs_lock:
        job = JOBS.get(job_id)
        if not job:
            return False, 'Job not found'
        if job['done']:
            return False, 'Job already finished'
        if job['paused']:
            return False, 'Job already paused'
        job['paused'] = True
        return True, None

def resume_job(job_id):
    with _jobs_lock:
        job = JOBS.get(job_id)
        if not job:
            return False, 'Job not found'
        if job['done']:
            return False, 'Job already finished'
        if not job['paused']:
            return False, 'Job is not paused'
        job['paused'] = False
        job['status'] = 'resuming'
        start_index = job['attempted_send_count']
    ctx = _JOB_CONTEXT.get(job_id)
    if not ctx:
        return False, 'Lost the context needed to resume this job (server may have restarted)'
    threading.Thread(
        target=_run_job,
        args=(job_id, ctx['sender'], ctx['items'], ctx['from_email'], start_index),
        daemon=True
    ).start()
    return True, None


def guess_imap_server(smtp_server):
    """Best-effort default: most providers use imap.<domain> alongside smtp.<domain>"""
    if smtp_server.startswith('smtp.'):
        return 'imap.' + smtp_server[len('smtp.'):]
    return smtp_server


class EmailSender:
    def __init__(self, smtp_server, smtp_port, email, password, imap_server=None, imap_port=993, save_to_sent=True):
        self.smtp_server = smtp_server
        self.smtp_port = int(smtp_port)
        self.email = email
        self.password = password
        self.use_ssl = self.smtp_port in [465, 587]
        self.imap_server = imap_server or guess_imap_server(smtp_server)
        self.imap_port = int(imap_port)
        self.save_to_sent = save_to_sent

    def _save_copy_to_sent(self, message):
        """Append a copy of the sent message to the account's IMAP Sent folder.
        Raw SMTP sending never does this automatically - webmail only shows a
        'Sent' entry when something explicitly copies it there via IMAP."""
        # Common folder names across providers (Hostinger/Dovecot, Gmail, Outlook, etc.)
        candidate_folders = ['Sent', 'INBOX.Sent', 'Sent Items', 'Sent Messages', '[Gmail]/Sent Mail']
        try:
            with imaplib.IMAP4_SSL(self.imap_server, self.imap_port, timeout=20) as imap:
                imap.login(self.email, self.password)
                for folder in candidate_folders:
                    try:
                        status, _ = imap.select(folder, readonly=False)
                        if status == 'OK':
                            imap.append(
                                folder,
                                '\\Seen',
                                imaplib.Time2Internaldate(time.time()),
                                message.as_bytes()
                            )
                            return {'saved': True, 'folder': folder}
                    except imaplib.IMAP4.error:
                        continue
            return {'saved': False, 'error': 'No writable Sent folder found on this account'}
        except Exception as e:
            return {'saved': False, 'error': str(e)}

    def verify_credentials(self):
        """Log in to the SMTP server without sending anything, just to
        confirm the email/password are correct before a job (or single
        send) commits to anything. Returns (ok, error_message)."""
        try:
            if self.smtp_port == 465:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=15) as server:
                    server.login(self.email, self.password)
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15) as server:
                    if self.smtp_port == 587:
                        context = ssl.create_default_context()
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                        server.starttls(context=context)
                    server.login(self.email, self.password)
            return True, None
        except smtplib.SMTPAuthenticationError:
            return False, 'Incorrect email or password for this account. Please re-enter the correct password.'
        except Exception as e:
            return False, f'Could not verify credentials: {str(e)}'

    def send_email(self, to_email, subject, body):
        """Send a single email with retry logic"""
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                # Create message
                message = MIMEMultipart('alternative')
                message['From'] = self.email
                message['To'] = to_email
                message['Subject'] = subject
                message['Date'] = formatdate(localtime=True)
                message['Message-ID'] = make_msgid()
                
                # Convert plain text to HTML with proper formatting
                # Replace line breaks with <br> tags and wrap in HTML
                html_body = body.replace('\n', '<br>\n')
                html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
    </style>
</head>
<body>
    {html_body}
</body>
</html>
"""
                
                # Add both plain text and HTML versions
                text_part = MIMEText(body, 'plain', 'utf-8')
                html_part = MIMEText(html_content, 'html', 'utf-8')
                message.attach(text_part)
                message.attach(html_part)
                
                # Connect and send
                if self.smtp_port == 465:
                    # SSL connection
                    context = ssl.create_default_context()
                    # Allow self-signed certificates (common with some SMTP providers)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30) as server:
                        server.login(self.email, self.password)
                        server.send_message(message)
                else:
                    # TLS connection
                    with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                        if self.smtp_port == 587:
                            context = ssl.create_default_context()
                            # Allow self-signed certificates
                            context.check_hostname = False
                            context.verify_mode = ssl.CERT_NONE
                            server.starttls(context=context)
                        server.login(self.email, self.password)
                        server.send_message(message)
                
                logger.info(f"Email sent successfully to {to_email}")

                sent_folder_result = {'saved': False}
                if self.save_to_sent:
                    sent_folder_result = self._save_copy_to_sent(message)
                    if sent_folder_result.get('saved'):
                        logger.info(f"Saved copy to '{sent_folder_result['folder']}' folder")
                    else:
                        logger.warning(f"Could not save copy to Sent folder: {sent_folder_result.get('error')}")

                return {
                    'success': True,
                    'recipient': to_email,
                    'saved_to_sent': sent_folder_result.get('saved', False),
                    'sent_folder_note': None if sent_folder_result.get('saved') else sent_folder_result.get('error')
                }
                
            except smtplib.SMTPAuthenticationError as e:
                logger.error(f"Authentication failed: {str(e)}")
                return {'success': False, 'recipient': to_email, 'error': 'Authentication failed. Check email/password'}
                
            except smtplib.SMTPRecipientsRefused as e:
                logger.error(f"Recipient refused for {to_email}: {str(e)}")
                return {'success': False, 'recipient': to_email, 'error': 'Recipient address rejected'}
                
            except smtplib.SMTPException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"SMTP error for {to_email}, retrying... ({attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"SMTP error for {to_email}: {str(e)}")
                    return {'success': False, 'recipient': to_email, 'error': f'SMTP error: {str(e)}'}
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Error sending to {to_email}, retrying... ({attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Failed to send to {to_email}: {str(e)}")
                    return {'success': False, 'recipient': to_email, 'error': str(e)}
        
        return {'success': False, 'recipient': to_email, 'error': 'Max retries exceeded'}

@app.route('/api/send-email', methods=['POST'])
def send_single_email():
    """Handle single email send request"""
    try:
        data = request.json
        
        # Extract parameters
        from_email = data.get('from')
        password = data.get('password')
        to_email = data.get('to')
        subject = data.get('subject')
        body = data.get('body')
        smtp_server = data.get('smtp_server', 'smtp.hostinger.com')
        smtp_port = data.get('smtp_port', '465')
        imap_server = data.get('imap_server')
        imap_port = data.get('imap_port', 993)
        save_to_sent = data.get('save_to_sent', True)
        
        # Validate required fields
        if not all([from_email, password, to_email, subject, body]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        message_key = _message_key(to_email, subject, body)
        if message_key in _already_sent_keys():
            result = {
                'success': False, 'skipped': True, 'recipient': to_email,
                'error': 'Duplicate email: this exact message was already sent to this recipient.',
                'message_key': message_key,
            }
            _append_send_log('single', from_email, result)
            return jsonify(result)

        ok, quota_error = reserve_daily_quota(from_email, 1, data.get('daily_limit'))
        if not ok:
            return jsonify({'success': False, 'error': quota_error}), 429

        # Send email
        sender = EmailSender(smtp_server, smtp_port, from_email, password, imap_server, imap_port, save_to_sent)

        cred_ok, cred_error = sender.verify_credentials()
        if not cred_ok:
            release_daily_quota(from_email, 1)
            return jsonify({'success': False, 'error': cred_error, 'auth_error': True}), 401

        result = sender.send_email(to_email, subject, body)
        result['message_key'] = message_key
        _append_send_log('single', from_email, result)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in send_single_email: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/send-bulk', methods=['POST'])
def send_bulk_emails():
    """Queue a bulk send. Emails go out one at a time with a random 5+ minute
    gap between each (see next_delay) - poll /api/job-status/<job_id> for progress."""
    try:
        data = request.json
        
        # Extract parameters
        from_email = data.get('from')
        password = data.get('password')
        recipients = data.get('recipients', [])
        subject = data.get('subject')
        body = data.get('body')
        smtp_server = data.get('smtp_server', 'smtp.hostinger.com')
        smtp_port = data.get('smtp_port', '465')
        imap_server = data.get('imap_server')
        imap_port = data.get('imap_port', 993)
        save_to_sent = data.get('save_to_sent', True)
        
        # Validate required fields
        if not all([from_email, password, recipients, subject, body]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        if not isinstance(recipients, list) or len(recipients) == 0:
            return jsonify({'success': False, 'error': 'Recipients must be a non-empty list'}), 400

        requested_items = [{'to': r, 'subject': subject, 'body': body} for r in recipients]
        items, skipped_results = split_duplicate_messages(requested_items)

        if not items:
            job_id = start_job(None, [], from_email, skipped_results)
            for result in skipped_results:
                _append_send_log(job_id, from_email, result)
            return jsonify({'success': True, 'job_id': job_id, 'total': len(requested_items),
                            'skipped_duplicates': len(skipped_results)})

        ok, quota_error = reserve_daily_quota(from_email, len(items), data.get('daily_limit'))
        if not ok:
            return jsonify({'success': False, 'error': quota_error}), 429

        sender = EmailSender(smtp_server, smtp_port, from_email, password, imap_server, imap_port, save_to_sent)

        cred_ok, cred_error = sender.verify_credentials()
        if not cred_ok:
            release_daily_quota(from_email, len(items))
            return jsonify({'success': False, 'error': cred_error, 'auth_error': True}), 401

        job_id = start_job(sender, items, from_email, skipped_results)
        for result in skipped_results:
            _append_send_log(job_id, from_email, result)

        return jsonify({'success': True, 'job_id': job_id, 'total': len(requested_items),
                        'skipped_duplicates': len(skipped_results)})
        
    except Exception as e:
        logger.error(f"Error in send_bulk_emails: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/send-personalized', methods=['POST'])
def send_personalized_emails():
    """Send different subject/body content to different recipients in one request.
    Body shape: { from, password, smtp_server, smtp_port, max_workers,
                   messages: [ {to, subject, body}, ... ] }"""
    try:
        data = request.json

        from_email = data.get('from')
        password = data.get('password')
        messages = data.get('messages', [])
        smtp_server = data.get('smtp_server', 'smtp.hostinger.com')
        smtp_port = data.get('smtp_port', '465')
        imap_server = data.get('imap_server')
        imap_port = data.get('imap_port', 993)
        save_to_sent = data.get('save_to_sent', True)

        if not all([from_email, password]):
            return jsonify({'success': False, 'error': 'Missing from/password'}), 400

        if not isinstance(messages, list) or len(messages) == 0:
            return jsonify({'success': False, 'error': 'messages must be a non-empty list of {to, subject, body}'}), 400

        # Validate each entry up front so one bad row doesn't silently get skipped
        for i, m in enumerate(messages):
            if not all([m.get('to'), m.get('subject'), m.get('body')]):
                return jsonify({'success': False, 'error': f'messages[{i}] is missing to/subject/body'}), 400

        items, skipped_results = split_duplicate_messages(messages)

        if not items:
            job_id = start_job(None, [], from_email, skipped_results)
            for result in skipped_results:
                _append_send_log(job_id, from_email, result)
            return jsonify({'success': True, 'job_id': job_id, 'total': len(messages),
                            'skipped_duplicates': len(skipped_results)})

        ok, quota_error = reserve_daily_quota(from_email, len(items), data.get('daily_limit'))
        if not ok:
            return jsonify({'success': False, 'error': quota_error}), 429

        sender = EmailSender(smtp_server, smtp_port, from_email, password, imap_server, imap_port, save_to_sent)

        cred_ok, cred_error = sender.verify_credentials()
        if not cred_ok:
            release_daily_quota(from_email, len(items))
            return jsonify({'success': False, 'error': cred_error, 'auth_error': True}), 401

        job_id = start_job(sender, items, from_email, skipped_results)
        for result in skipped_results:
            _append_send_log(job_id, from_email, result)

        return jsonify({'success': True, 'job_id': job_id, 'total': len(messages),
                        'skipped_duplicates': len(skipped_results)})

    except Exception as e:
        logger.error(f"Error in send_personalized_emails: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/job-status/<job_id>', methods=['GET'])
def job_status(job_id):
    """Poll progress/results for a job started by send-bulk or send-personalized."""
    with _jobs_lock:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Unknown job_id'}), 404
        return jsonify({'success': True, **job})

@app.route('/api/job-cancel/<job_id>', methods=['POST'])
def job_cancel(job_id):
    """Stop a running bulk/personalized job. Already-sent emails stay sent;
    unsent recipients have their reserved daily quota released."""
    ok = cancel_job(job_id)
    if not ok:
        return jsonify({'success': False, 'error': 'Job not found or already finished'}), 404
    return jsonify({'success': True})

@app.route('/api/job-pause/<job_id>', methods=['POST'])
def job_pause(job_id):
    """Pause a running job before its next send. Unsent recipients stay
    queued (quota stays reserved) so /api/job-resume/<id> can pick up
    where it left off."""
    ok, error = pause_job(job_id)
    if not ok:
        return jsonify({'success': False, 'error': error}), 404
    return jsonify({'success': True})

@app.route('/api/job-resume/<job_id>', methods=['POST'])
def job_resume(job_id):
    """Resume a paused job, continuing from the first unsent recipient."""
    ok, error = resume_job(job_id)
    if not ok:
        return jsonify({'success': False, 'error': error}), 404
    return jsonify({'success': True})

@app.route('/api/send-log', methods=['GET'])
def send_log():
    """Return recent send-attempt log entries (who got an email, and why not
    if they didn't). Optional ?limit=N, default 200, most recent last."""
    limit = request.args.get('limit', 200, type=int)
    entries = []
    try:
        with open(SEND_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return jsonify({'success': True, 'entries': entries[-limit:]})

@app.route('/api/parse-csv', methods=['POST'])
def parse_csv():
    """Extract recipient rows from an uploaded CSV (multipart field 'file').
    Any header containing 'email' or 'mail' is used as the address column."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded (expected multipart field "file")'}), 400

    try:
        text = request.files['file'].read().decode('utf-8-sig')
    except UnicodeDecodeError:
        return jsonify({'success': False, 'error': 'CSV must be UTF-8 encoded'}), 400

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return jsonify({'success': False, 'error': 'CSV has no header row'}), 400

    email_col = next((c for c in reader.fieldnames if 'mail' in c.lower()), reader.fieldnames[0])
    subject_col = next((c for c in reader.fieldnames if 'subject' in c.lower()), None)
    body_col = next((c for c in reader.fieldnames if c != email_col and ('body' in c.lower() or 'message' in c.lower())), None)

    rows = []
    for row in reader:
        row = {k: (v or '').strip() for k, v in row.items()}
        if row.get(email_col):
            row['_email'] = row[email_col]
            if subject_col:
                row['_subject'] = row.get(subject_col, '')
            if body_col:
                row['_body'] = row.get(body_col, '')
            rows.append(row)

    return jsonify({
        'success': True,
        'columns': reader.fieldnames,
        'email_column': email_col,
        'subject_column': subject_col,
        'body_column': body_col,
        'rows': rows
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'service': 'email-sender'})

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 High-Speed Email Sender Backend")
    print("=" * 60)
    print("Server starting on http://localhost:5000")
    print("\nEndpoints:")
    print("  POST /api/send-email        - Send single email")
    print("  POST /api/send-bulk         - Queue bulk send (spaced 5+ min apart)")
    print("  POST /api/send-personalized - Queue personalized send (spaced 5+ min apart)")
    print("  GET  /api/job-status/<id>   - Poll a bulk/personalized job")
    print("  POST /api/job-cancel/<id>   - Stop a running job (releases unused quota)")
    print("  POST /api/job-pause/<id>    - Pause a running job (keeps quota reserved)")
    print("  POST /api/job-resume/<id>   - Resume a paused job")
    print("  GET  /api/send-log          - Recent send attempts (who/why not)")
    print("  POST /api/parse-csv         - Extract recipient rows from a CSV upload")
    print("  GET  /api/health            - Health check")
    print(f"\nDaily send limit: default {DEFAULT_DAILY_LIMIT}, hard cap {HARD_DAILY_CAP} per sender/day")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)