#!/usr/bin/env python3
"""
Collect TLDR newsletters using Gmail API

Strategy:
1. Gmail API to search for TLDR emails in date range
2. Parse HTML email body to extract individual news items
3. For TLDR main, filter for AI-related keywords
4. Output normalized JSON format

Outputs:
- .tmp/raw_tldr_ai.json
- .tmp/raw_tldr_main.json
"""

import os
import sys
import json
import argparse
import base64
import re
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, validate_date_range

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client beautifulsoup4")
    sys.exit(1)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# AI keywords for filtering TLDR main newsletter
AI_KEYWORDS = [
    'ai', 'artificial intelligence', 'machine learning', 'ml', 'gpt',
    'llm', 'large language model', 'chatgpt', 'openai', 'claude',
    'neural network', 'deep learning', 'generative ai', 'computer vision',
    'nlp', 'natural language processing', 'robotics', 'autonomous',
    'anthropic', 'google gemini', 'meta ai', 'mistral'
]

# Promotional content to filter out (case-insensitive)
PROMOTIONAL_FILTERS = [
    'get a demo', 'view online', 'claim your free', 'learn more',
    'advertise with us', 'apply here', 'track your referrals',
    'create your own role', 'sign up', 'subscribe', 'unsubscribe',
    'sponsor', 'sponsored', 'advertisement', 'ad:', 'get started',
    'try it now', 'download now', 'register now', 'join us'
]

# Section filters for each newsletter type
# TLDR AI: Only Headlines & Launches (~3-6 articles/day)
# TLDR Main: Big Tech & Startups + Miscellaneous (~4-6 articles/day)
TLDR_AI_SECTIONS = ['headlines & launches']
TLDR_MAIN_SECTIONS = ['big tech & startups']

# All known sections (used to detect section boundaries)
ALL_KNOWN_SECTIONS = [
    'headlines & launches', 'deep dives', 'deep dives & analysis',
    'engineering & research', 'research & innovation', 'opinion & tutorials',
    'miscellaneous', 'quick links',
    'big tech & startups', 'science & futuristic technology',
    'programming, design & data science', 'launches', 'headlines'
]


def get_gmail_service():
    """
    Authenticate and return Gmail API service.

    Token resolution order:
      1. GMAIL_TOKEN_JSON env var (used on Railway / any server deployment)
      2. token.json file on disk (used locally)
    """
    import tempfile

    creds = None

    # ── Load token ────────────────────────────────────────────────────────────
    token_json_env = os.environ.get("GMAIL_TOKEN_JSON")

    if token_json_env:
        # Write env var content to a temp file so Credentials can read it
        try:
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
            tmp.write(token_json_env)
            tmp.close()
            creds = Credentials.from_authorized_user_file(tmp.name, SCOPES)
            os.unlink(tmp.name)
            print("  Loaded Gmail token from GMAIL_TOKEN_JSON env var")
        except Exception as e:
            print(f"ERROR: Could not parse GMAIL_TOKEN_JSON: {e}")
            sys.exit(1)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        print("  Loaded Gmail token from token.json")
    else:
        print("ERROR: No Gmail token found.")
        print("  Locally: run the app once to complete OAuth — token.json will be created.")
        print("  On Railway: set the GMAIL_TOKEN_JSON environment variable.")
        sys.exit(1)

    # ── Refresh if expired ────────────────────────────────────────────────────
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing expired Gmail token...")
            creds.refresh(Request())

            # Persist refreshed token back to disk (local) or env hint (server)
            if not token_json_env and os.path.exists('token.json'):
                with open('token.json', 'w') as f:
                    f.write(creds.to_json())
            else:
                print("  NOTE: Token refreshed in memory. Update GMAIL_TOKEN_JSON")
                print("  on Railway with this new value to avoid re-refreshing:")
                print(f"  {creds.to_json()}")
        else:
            print("ERROR: Gmail token is invalid and cannot be refreshed.")
            print("  Delete token.json locally, re-authenticate, then update GMAIL_TOKEN_JSON on Railway.")
            sys.exit(1)

    return build('gmail', 'v1', credentials=creds)


def extract_email_body(message):
    """
    Extract HTML body from Gmail message

    Args:
        message: Gmail message object

    Returns:
        HTML body string
    """
    try:
        # Check for parts (multipart message)
        if 'parts' in message['payload']:
            for part in message['payload']['parts']:
                if part['mimeType'] == 'text/html':
                    body_data = part['body'].get('data', '')
                    if body_data:
                        return base64.urlsafe_b64decode(body_data).decode('utf-8')

                # Check nested parts
                if 'parts' in part:
                    for nested_part in part['parts']:
                        if nested_part['mimeType'] == 'text/html':
                            body_data = nested_part['body'].get('data', '')
                            if body_data:
                                return base64.urlsafe_b64decode(body_data).decode('utf-8')

        # Single part message
        body_data = message['payload']['body'].get('data', '')
        if body_data:
            return base64.urlsafe_b64decode(body_data).decode('utf-8')

    except Exception as e:
        print(f"WARNING: Failed to extract email body: {e}")

    return ''


def parse_tldr_html(html_body: str, newsletter_type: str) -> list:
    """
    Parse TLDR email HTML to extract news items from specific sections

    Args:
        html_body: HTML email body
        newsletter_type: 'ai' or 'main'

    Returns:
        List of news item dicts
    """
    soup = BeautifulSoup(html_body, 'html.parser')
    items = []

    # Determine which sections to extract
    if newsletter_type == 'ai':
        target_sections = TLDR_AI_SECTIONS
    else:
        target_sections = TLDR_MAIN_SECTIONS

    # Strategy: Find h1 section headers and extract links between them
    # TLDR uses h1 tags for section headers
    h1_elements = soup.find_all('h1')

    # Build list of (section_name, h1_element) for all known sections
    section_headers = []
    for h1 in h1_elements:
        text = clean_text(h1.get_text()).lower()
        for section_name in ALL_KNOWN_SECTIONS:
            if section_name in text:
                section_headers.append((section_name, h1))
                break

    # Extract links from target sections only
    for i, (section_name, h1) in enumerate(section_headers):
        # Skip if this is not a target section
        if not any(target in section_name for target in target_sections):
            continue

        # Find the next section header (boundary)
        next_section_h1 = None
        if i + 1 < len(section_headers):
            next_section_h1 = section_headers[i + 1][1]

        # Extract links between this h1 and the next section h1
        for elem in h1.find_all_next():
            # Stop if we hit the next section
            if next_section_h1 and elem == next_section_h1:
                break

            # Only process anchor tags
            if elem.name != 'a' or not elem.get('href'):
                continue

            href = elem['href']
            title = clean_text(elem.get_text())

            # Skip navigation links, social media, etc.
            if not title or len(title) < 10:
                continue
            if any(skip in href.lower() for skip in ['unsubscribe', 'tldr.tech/preferences', 'twitter.com', 'linkedin.com', 'facebook.com', 'refer.tldr.tech']):
                continue

            # Filter out promotional content
            title_lower = title.lower()
            if any(promo in title_lower for promo in PROMOTIONAL_FILTERS):
                continue

            # Get description (usually in next sibling or parent's text)
            description = ''
            # Try to find description text after the link
            parent = elem.parent
            if parent:
                # Get all text in parent, excluding the link text
                parent_text = clean_text(parent.get_text())
                if parent_text and len(parent_text) > len(title) + 20:
                    # Description is the part after the title
                    desc_start = parent_text.find(title)
                    if desc_start >= 0:
                        description = parent_text[desc_start + len(title):].strip()
                        # Clean up common patterns
                        if description.startswith('(') or description.startswith('-'):
                            description = description.lstrip('(- ')

            # Also filter promotional descriptions
            if description and any(promo in description.lower() for promo in PROMOTIONAL_FILTERS):
                continue

            # For TLDR main, filter for AI keywords
            if newsletter_type == 'main':
                combined_text = f"{title} {description}".lower()
                if not any(keyword in combined_text for keyword in AI_KEYWORDS):
                    continue

            items.append({
                'title': title,
                'url': href,
                'description': description,
                'section': section_name
            })

    # If section-based extraction didn't work, fall back to extracting all links
    if not items and not section_headers:
        print("  WARNING: Could not identify sections, extracting all links")
        # Fallback logic - extract all article-like links
        for link in soup.find_all('a', href=True):
            href = link['href']
            title = clean_text(link.get_text())
            if title and len(title) > 15 and 'minute read' in title.lower():
                items.append({
                    'title': title,
                    'url': href,
                    'description': '',
                    'section': 'unknown'
                })

    # Deduplicate by URL (TLDR sometimes has duplicate links)
    seen_urls = set()
    unique_items = []
    for item in items:
        if item['url'] not in seen_urls:
            seen_urls.add(item['url'])
            unique_items.append(item)

    return unique_items


def fetch_tldr_from_gmail(service, newsletter_type: str, start_date: str, end_date: str) -> list:
    """
    Fetch TLDR newsletters from Gmail

    Args:
        service: Gmail API service
        newsletter_type: 'ai' or 'main'
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD

    Returns:
        List of normalized articles
    """
    # Both newsletters come from the same email address
    sender = 'dan@tldrnewsletter.com'
    source_name = 'TLDR AI' if newsletter_type == 'ai' else 'TLDR'

    # Build Gmail search query - fetch all TLDR emails, filter later
    query = f'from:{sender} after:{start_date} before:{end_date}'

    print(f"Searching Gmail for {source_name} emails...")
    print(f"Query: {query}")

    try:
        # Search for messages
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            print(f"WARNING: No TLDR emails found in date range")
            print("Check: 1) Date range is correct 2) Emails not in spam 3) Subscription active")
            return []

        print(f"Found {len(messages)} total TLDR emails")

        all_articles = []
        filtered_count = 0

        # Process each email
        for i, msg in enumerate(messages):
            print(f"Processing email {i+1}/{len(messages)}...")

            # Get full message
            message = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()

            # Check "From" header to determine which newsletter
            headers = message['payload']['headers']
            from_header = next((h['value'] for h in headers if h['name'] == 'From'), '')

            # Filter based on newsletter type
            # TLDR AI has "TLDR AI" in the From display name
            # TLDR main has just "TLDR" (without AI) in the From display name
            if newsletter_type == 'ai':
                if 'TLDR AI' not in from_header:
                    continue  # Skip non-AI emails
            else:
                if 'TLDR AI' in from_header:
                    continue  # Skip AI emails when collecting main
                # Also check it's actually TLDR (not some other sender)
                if 'TLDR' not in from_header:
                    continue

            filtered_count += 1
            print(f"  Matched {source_name} newsletter")

            # Extract date from headers (already have headers from above)
            date_str = next((h['value'] for h in headers if h['name'] == 'Date'), '')

            # Parse date (email date format is complex, use simple extraction)
            try:
                # Extract date from "Day, DD Mon YYYY HH:MM:SS" format
                date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', date_str)
                if date_match:
                    date_obj = datetime.strptime(date_match.group(1), '%d %b %Y')
                    published_at = date_obj.isoformat() + 'Z'
                else:
                    published_at = datetime.now().isoformat() + 'Z'
            except:
                published_at = datetime.now().isoformat() + 'Z'

            # Extract HTML body
            html_body = extract_email_body(message)

            if not html_body:
                print(f"WARNING: Could not extract body from email")
                continue

            # Parse HTML to extract news items
            news_items = parse_tldr_html(html_body, newsletter_type)

            # Convert to normalized format
            for item in news_items:
                all_articles.append({
                    'source': source_name,
                    'title': item['title'],
                    'description': item['description'],
                    'url': item['url'],
                    'published_at': published_at,
                    'content': item['description'],  # Use description as content
                    'raw': {
                        'email_date': date_str,
                        'message_id': msg['id']
                    }
                })

        print(f"Filtered to {filtered_count} {source_name} emails")
        return all_articles

    except Exception as e:
        print(f"ERROR: Failed to fetch from Gmail: {e}")
        if '403' in str(e):
            print("\nTry deleting token.json and re-authenticating")
        return []


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Collect TLDR newsletters via Gmail')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--output_dir', default='.tmp', help='Output directory')
    args = parser.parse_args()

    # Validate dates
    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Get Gmail service
    gmail_service = get_gmail_service()

    # Collect both newsletters
    print("\n=== Collecting TLDR AI ===")
    tldr_ai = fetch_tldr_from_gmail(gmail_service, 'ai', args.start_date, args.end_date)

    print("\n=== Collecting TLDR Main (AI-filtered) ===")
    tldr_main = fetch_tldr_from_gmail(gmail_service, 'main', args.start_date, args.end_date)

    # Save outputs
    os.makedirs(args.output_dir, exist_ok=True)

    ai_output = os.path.join(args.output_dir, 'raw_tldr_ai.json')
    with open(ai_output, 'w', encoding='utf-8') as f:
        json.dump(tldr_ai, f, ensure_ascii=False, indent=2)

    main_output = os.path.join(args.output_dir, 'raw_tldr_main.json')
    with open(main_output, 'w', encoding='utf-8') as f:
        json.dump(tldr_main, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(tldr_ai)} TLDR AI articles")
    print(f"✓ Saved to {ai_output}")
    print(f"✓ Collected {len(tldr_main)} TLDR main articles (AI-filtered)")
    print(f"✓ Saved to {main_output}")


if __name__ == "__main__":
    main()
