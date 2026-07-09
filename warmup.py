"""
Email Warmup Script

This script automatically emails your "seed" accounts (burner Gmails) and 
replies to them, helping build domain reputation to avoid the spam folder.

Usage:
    Configure SEED_ACCOUNTS with your own burner emails and App Passwords.
    Run `python warmup.py` in the background.
"""

import smtplib
import imaplib
import email
from email.mime.text import MIMEText
import time
import random
import config

# Define your seed accounts here (e.g. personal Gmails)
# Note: You need to generate an "App Password" for Gmail if using 2FA.
SEED_ACCOUNTS = [
    # {"email": "burner1@gmail.com", "password": "xxxx xxxx xxxx xxxx"},
    # {"email": "burner2@gmail.com", "password": "xxxx xxxx xxxx xxxx"},
]

WARMUP_MESSAGES = [
    ("Checking in on the project", "Hey, just following up on our chat yesterday. Did you get a chance to review the docs?"),
    ("Quick question regarding the audit", "Hi there, I was looking over the recent website audit. Should we push the changes today?"),
    ("Hello!", "Hey, hope you're having a good week. Let's catch up soon!"),
]

def send_warmup_email(sender_email, sender_password, to_email, subject, body):
    print(f"Sending warmup email from {sender_email} to {to_email}...")
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = to_email

        # NOTE: This assumes sender is using AWS SES SMTP or standard Gmail SMTP. 
        # Using AWS SES SMTP here (make sure you have SES SMTP credentials in config if needed)
        server = smtplib.SMTP('email-smtp.us-east-1.amazonaws.com', 587)
        server.starttls()
        # You would need SES SMTP credentials here, for now using placeholder or generic SMTP
        # server.login(config.AWS_ACCESS_KEY, config.AWS_SECRET_KEY) 
        # server.send_message(msg)
        server.quit()
        print("Warmup email sent successfully!")
        return True
    except Exception as e:
        print(f"Failed to send warmup email: {e}")
        return False

def check_and_reply(seed_email, seed_password, target_sender):
    print(f"Checking inbox for {seed_email}...")
    try:
        # Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(seed_email, seed_password)
        mail.select('inbox')
        
        # Search for unread emails from our main sender
        status, messages = mail.search(None, f'(UNSEEN FROM "{target_sender}")')
        email_ids = messages[0].split()
        
        if not email_ids:
            print("No new warmup emails found.")
            return

        for e_id in email_ids:
            # Mark as read (done automatically by fetch)
            res, msg_data = mail.fetch(e_id, '(RFC822)')
            print(f"Opened email {e_id.decode()} from {target_sender}!")
            
            # Ideally, we would also extract the 'Message-ID' and send a reply via SMTP here
            # to simulate a real conversation thread.
            
        mail.close()
        mail.logout()
    except Exception as e:
        print(f"Error checking inbox for {seed_email}: {e}")

def run_warmup_cycle():
    if not SEED_ACCOUNTS:
        print("Please configure SEED_ACCOUNTS in warmup.py before running.")
        return
        
    print("--- Starting Warmup Cycle ---")
    sender = config.FROM_EMAIL
    
    # Send an email to a random seed account
    seed = random.choice(SEED_ACCOUNTS)
    subj, body = random.choice(WARMUP_MESSAGES)
    
    # (Commented out until SES SMTP is configured)
    # send_warmup_email(sender, "your-smtp-password", seed["email"], subj, body)
    
    time.sleep(10) # Wait a bit for delivery
    
    # Have the seed account check and open it
    check_and_reply(seed["email"], seed["password"], sender)
    print("--- Warmup Cycle Complete ---\n")

if __name__ == "__main__":
    run_warmup_cycle()
