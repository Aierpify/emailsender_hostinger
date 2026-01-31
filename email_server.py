#!/usr/bin/env python3
"""
High-Speed Email Sender Backend
Robust SMTP email sender with concurrent processing and error handling
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import logging

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EmailSender:
    def __init__(self, smtp_server, smtp_port, email, password):
        self.smtp_server = smtp_server
        self.smtp_port = int(smtp_port)
        self.email = email
        self.password = password
        self.use_ssl = self.smtp_port in [465, 587]
        
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
                return {'success': True, 'recipient': to_email}
                
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
        
        # Validate required fields
        if not all([from_email, password, to_email, subject, body]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        # Send email
        sender = EmailSender(smtp_server, smtp_port, from_email, password)
        result = sender.send_email(to_email, subject, body)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in send_single_email: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/send-bulk', methods=['POST'])
def send_bulk_emails():
    """Handle bulk email send request with concurrent processing"""
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
        max_workers = data.get('max_workers', 20)  # High concurrency for bulk operations
        
        # Validate required fields
        if not all([from_email, password, recipients, subject, body]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        if not isinstance(recipients, list) or len(recipients) == 0:
            return jsonify({'success': False, 'error': 'Recipients must be a non-empty list'}), 400
        
        # Initialize sender
        sender = EmailSender(smtp_server, smtp_port, from_email, password)
        
        # Send emails concurrently
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_recipient = {
                executor.submit(sender.send_email, recipient, subject, body): recipient 
                for recipient in recipients
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_recipient):
                result = future.result()
                results.append(result)
        
        # Calculate statistics
        total = len(results)
        successful = sum(1 for r in results if r['success'])
        failed = total - successful
        
        return jsonify({
            'success': True,
            'total': total,
            'successful': successful,
            'failed': failed,
            'results': results
        })
        
    except Exception as e:
        logger.error(f"Error in send_bulk_emails: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
    print("  POST /api/send-email  - Send single email")
    print("  POST /api/send-bulk   - Send bulk emails (concurrent)")
    print("  GET  /api/health      - Health check")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)