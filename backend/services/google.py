import os
import json
import base64
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from email.mime.text import MIMEText

from core.config import TOKEN_FILE, CREDENTIALS_FILE

# If modifying these scopes, delete the file token.json.
# Make sure to delete the old token.json whenever you modify these scopes!
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    # Gmail
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.settings.basic',
    # Drive
    'https://www.googleapis.com/auth/drive',
    # Calendar
    'https://www.googleapis.com/auth/calendar',
    # Docs
    'https://www.googleapis.com/auth/documents',
    # Sheets
    'https://www.googleapis.com/auth/spreadsheets',
    # Slides
    'https://www.googleapis.com/auth/presentations',
    # Forms (workspace-mcp uses forms.body scopes, not the legacy auth/forms)
    'https://www.googleapis.com/auth/forms.body',
    'https://www.googleapis.com/auth/forms.body.readonly',
    'https://www.googleapis.com/auth/forms.responses.readonly',
    # Tasks
    'https://www.googleapis.com/auth/tasks',
    # Contacts
    'https://www.googleapis.com/auth/contacts',
]

# Mirror server.py's pattern: always derive from __file__ so the path is
# anchored to the backend directory regardless of SYNAPSE_DATA_DIR being
# a relative string (which would otherwise resolve against CWD).
_BACKEND_ROOT = Path(__file__).resolve().parent.parent  # backend/services/google.py → backend/
_project_root = _BACKEND_ROOT.parent
_data_dir_env = os.getenv("SYNAPSE_DATA_DIR", "")
if _data_dir_env:
    _data_dir_p = Path(_data_dir_env)
    _DATA_DIR = _data_dir_p if _data_dir_p.is_absolute() else _project_root / _data_dir_p
else:
    _DATA_DIR = _BACKEND_ROOT / "data"
GOOGLE_CREDENTIALS_DIR = _DATA_DIR / "google-credentials"


class UnauthenticatedError(Exception):
    pass

# Module-level storage: keeps the Flow object alive between get_auth_url() and
# finish_auth() so that the internally-generated code_verifier (PKCE) and state
# are preserved. Without this, a fresh Flow in finish_auth loses the verifier
# and Google returns "Missing code verifier".
_pending_flow = None

def _email_from_token_data(token_data: dict) -> str | None:
    """Best-effort extract of the user's email from a token dict."""
    email = token_data.get("email")
    if email:
        return email
    id_token = token_data.get("id_token")
    if id_token and "." in id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload.get("email")
        except Exception:
            return None
    return None


def _sync_token_to_mcp(token_data: dict, email: str | None = None) -> None:
    """Mirror token.json into google-credentials/ so workspace-mcp sees fresh creds."""
    try:
        os.makedirs(GOOGLE_CREDENTIALS_DIR, exist_ok=True)
        with open(GOOGLE_CREDENTIALS_DIR / "token.json", "w") as f:
            json.dump(token_data, f, indent=2)
        if email:
            with open(GOOGLE_CREDENTIALS_DIR / f"{email}.json", "w") as f:
                json.dump(token_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Failed to sync token to workspace-mcp dir: {e}")


def get_google_credentials():
    """Returns valid credentials or None."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Preserve fields like `email` that aren't part of creds.to_json()
            try:
                with open(TOKEN_FILE, "r") as f:
                    prior = json.load(f)
            except Exception:
                prior = {}
            refreshed = json.loads(creds.to_json())
            email = prior.get("email") or _email_from_token_data(refreshed)
            merged = {**prior, **refreshed}
            if email:
                merged["email"] = email
            with open(TOKEN_FILE, "w") as token:
                json.dump(merged, token, indent=2)
            _sync_token_to_mcp(merged, email)
            return creds
        except Exception as e:
            print(f"Warning: Token refresh failed: {e}")

    return None

def get_auth_url(redirect_uri):
    """Generates the OAuth2 authorization URL and stores the flow for later use."""
    global _pending_flow

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Please upload it via Settings → Integrations."
        )

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE, SCOPES, redirect_uri=redirect_uri
    )
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
    )
    # Keep the flow alive so finish_auth can reuse it (preserves code_verifier)
    _pending_flow = flow
    print(f"DEBUG: Auth URL generated — flow stored for callback.")
    return auth_url

def finish_auth(code, redirect_uri):
    """Exchanges the auth code using the stored flow (preserves code_verifier)."""
    global _pending_flow

    # Reuse the stored flow to keep the code_verifier intact
    if _pending_flow is not None:
        flow = _pending_flow
        _pending_flow = None
        print("DEBUG: Using stored flow for token exchange.")
    else:
        # Fallback: create fresh flow (may fail if PKCE was involved)
        print("WARNING: No stored flow found — creating fresh flow (may fail with PKCE).")
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE, SCOPES, redirect_uri=redirect_uri
        )

    # Allow Google to return extra/different scopes without raising an error
    os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Build the token data
    token_data = json.loads(creds.to_json())

    # Fetch user email via Userinfo API
    email = None
    try:
        import urllib.request as _urlreq
        req = _urlreq.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"}
        )
        with _urlreq.urlopen(req, timeout=5) as r:
            userinfo = json.loads(r.read().decode())
            print(f"DEBUG: Userinfo response: {userinfo}")
            email = userinfo.get("email")
            if email:
                token_data["email"] = email
                print(f"DEBUG: Saved user email: {email}")
    except Exception as e:
        print(f"Warning: Could not fetch user email after OAuth: {e}")

    with open(TOKEN_FILE, "w") as token:
        json.dump(token_data, token, indent=2)

    print(f"DEBUG: token.json saved to {TOKEN_FILE}")

    # --- Sync to workspace-mcp's default directory ---
    try:
        mcp_cred_dir = GOOGLE_CREDENTIALS_DIR
        os.makedirs(mcp_cred_dir, exist_ok=True)
        # 1. Copy our credentials.json as client_secret.json
        import shutil
        shutil.copy2(CREDENTIALS_FILE, os.path.join(mcp_cred_dir, "client_secret.json"))
        # 2. Save the token with the email formatted name
        if email:
            mcp_token_path = os.path.join(mcp_cred_dir, f"{email}.json")
            with open(mcp_token_path, "w") as token:
                json.dump(token_data, token, indent=2)
            print(f"DEBUG: Synchronized token to workspace-mcp via {mcp_token_path}")
        # 3. Always save generic token.json just in case
        mcp_generic_token = os.path.join(mcp_cred_dir, "token.json")
        with open(mcp_generic_token, "w") as token:
            json.dump(token_data, token, indent=2)
            
    except Exception as e:
        print(f"Warning: Failed to sync tokens to workspace-mcp dir: {e}")

    return creds

def get_service(api, version):
    """Returns an authorized service instance or raises UnauthenticatedError."""
    creds = get_google_credentials()
    if not creds:
        raise UnauthenticatedError("User is not authenticated.")
    return build(api, version, credentials=creds)

def get_gmail_service():
    """Returns an authorized Gmail API service instance."""
    return get_service('gmail', 'v1')

def get_drive_service():
    """Returns an authorized Drive API service instance."""
    return get_service('drive', 'v3')

def get_calendar_service():
    """Returns an authorized Calendar API service instance."""
    return get_service('calendar', 'v3')

# --- Helper Functions ---

def list_messages(query=None, limit=5):
    """Lists messages from the user's mailbox.
    
    Args:
        query: String query to filter messages (e.g., 'subject:insurance').
        limit: Max number of messages to return.
    """
    print(f"DEBUG: list_messages called with query='{query}', limit={limit} (type: {type(limit)})")
    try:
        service = get_gmail_service()
        
        results = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        messages = results.get("messages", [])
        
        email_summaries = []
        if not messages:
            return []

        for msg in messages:
            # Get full details for snippet and headers
            full_msg = service.users().messages().get(userId="me", id=msg['id'], format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            
            headers = full_msg.get("payload", {}).get("headers", [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
            
            email_summaries.append({
                "id": msg['id'],
                "snippet": full_msg.get("snippet", ""),
                "subject": subject,
                "sender": sender
            })
            
        return email_summaries

    except UnauthenticatedError:
        raise
    except Exception as e:
        print(f"An error occurred: {e}")
        return []

def get_message(message_id):
    """Get the full content of a message."""
    try:
        service = get_gmail_service()
        message = service.users().messages().get(userId="me", id=message_id, format='full').execute()
        
        payload = message.get('payload', {})
        headers = payload.get("headers", [])
        
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
        date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
        
        # Decode body
        parts = payload.get('parts')
        body_text = ""
        body_html = None
        
        # Helper to recursively extract parts
        def parse_parts(parts):
            text = ""
            html = None
            for part in parts:
                mime_type = part.get('mimeType')
                body = part.get('body', {})
                data = body.get('data')
                
                if part.get('parts'):
                    # Recursive call
                    nested_text, nested_html = parse_parts(part.get('parts'))
                    text += nested_text
                    if nested_html and not html:
                        html = nested_html
                
                if mime_type == 'text/plain' and data:
                    text += base64.urlsafe_b64decode(data).decode('utf-8')
                elif mime_type == 'text/html' and data:
                    html = base64.urlsafe_b64decode(data).decode('utf-8')
            return text, html

        if parts:
            body_text, body_html = parse_parts(parts)
        else:
            # Single part message
            data = payload.get('body', {}).get('data', '')
            if data:
                body_text = base64.urlsafe_b64decode(data).decode('utf-8')
        
        # If no text found but html exists, use BS to strip tags for text body
        if not body_text and body_html:
             soup = BeautifulSoup(body_html, 'html.parser')
             body_text = soup.get_text()

        text = body_text if body_text else "(No body content found)"

        return {
            "id": message['id'],
            "subject": subject,
            "sender": sender,
            "date": date,
            "body": text,
            "html_body": body_html
        }

    except UnauthenticatedError:
        raise
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

def send_email(to, subject, body, cc=None, bcc=None):
    """Sends an email message.

    Args:
        to: Recipient email address.
        subject: Email subject.
        body: Email body text.
        cc: Optional CC recipient(s).
        bcc: Optional BCC recipient(s).
    """
    try:
        service = get_gmail_service()
        
        message = MIMEText(body)
        message['to'] = to
        if cc:
            message['Cc'] = cc
        if bcc:
            message['Bcc'] = bcc
        message['subject'] = subject
        
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {'raw': raw}

        message = service.users().messages().send(userId="me", body=body).execute()
        print(f"DEBUG: Message Sent. Id: {message['id']}")
        return message
    except UnauthenticatedError:
        raise
    except Exception as e:
        print(f"An error occurred sending email: {e}")
        return None
