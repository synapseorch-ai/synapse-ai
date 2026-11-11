import os
import json
import base64
import hmac
import secrets
import time
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from email.mime.text import MIMEText

#: Store keys. Deliberately not settings rows: load_settings() returns one dict
#: that is read on every turn and handed to a dozen call sites, and a refresh
#: token has no business in it. The account *email* is a setting, because it is
#: not a secret and one sync caller needs it — see core/tools.py.
_CLIENT_KEY = "google_client"
_TOKEN_KEY = "google_token"
EMAIL_SETTING = "google_account_email"

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


def google_credentials_dir() -> Path:
    """The directory `workspace-mcp` is pointed at.

    A real directory, because it is handed to a subprocess through an
    environment variable and the subprocess opens files in it — but process
    scratch rather than durable state. It is materialised from the stored rows
    immediately before the server is spawned, so it can be thrown away with the
    container and there is nothing in it that is not in the database.
    """
    from core.storage.scratch import scratch_dir

    return scratch_dir("google-credentials")


class UnauthenticatedError(Exception):
    pass

#: The Google sign-ins waiting for their callback, by their own random `state` (F1535): each keeps its Flow, and with
#: it the PKCE code_verifier Google checks, for `PENDING_S`. Only the callback carrying that state may use it, once:
#: one process-wide slot used to hold the waiting sign-in, the callback ignored `state`, and with the slot empty it
#: fell back to a fresh flow with no verifier, so a code from anyone's sign-in to this client was redeemed and kept
#: as the install's Google connection (login CSRF). A wrong state uses nothing up.
PENDING_S = 600
_PENDING_MAX = 8
_pending: dict[str, tuple[Flow, float]] = {}
NOT_STARTED_HERE = ("This Google sign-in did not start here, or took more than 10 minutes, so nothing was kept. "
                    "Connect Google again from Settings → Integrations.")


class SignInRefused(Exception):
    """A Google callback that is not the callback of a sign-in started here: nothing is kept (F1535)."""


def _now() -> float:
    return time.monotonic()


def _forget_expired(now: float) -> None:
    for state in [s for s, (_flow, until) in _pending.items() if until <= now]:
        _pending.pop(state, None)


def _take(state: str | None) -> Flow:
    """The waiting sign-in this `state` belongs to, taken so it cannot be used again; `SignInRefused` otherwise."""
    _forget_expired(_now())
    given = (state or "").encode()
    for known in list(_pending):
        if given and hmac.compare_digest(known.encode(), given):
            return _pending.pop(known)[0]
    raise SignInRefused(NOT_STARTED_HERE)

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


async def load_client_config() -> dict | None:
    """The uploaded `credentials.json` contents, or None."""
    from core.store import collections

    return await collections.load_one(_CLIENT_KEY) or None


async def save_client_config(data: dict) -> None:
    from core.store import collections

    await collections.save_one(_CLIENT_KEY, data)


async def load_token() -> dict | None:
    """The stored OAuth token document, or None."""
    from core.store import collections

    return await collections.load_one(_TOKEN_KEY) or None


async def save_token(token_data: dict) -> None:
    """Persist the token and publish the account email as a setting.

    The email is the one part of this a synchronous caller needs
    (``build_system_prompt`` tells the model which address to pass to the
    Workspace tools), so it is stored separately as a non-secret. That is what
    keeps the refresh token out of the settings dict entirely.
    """
    from core.store import collections
    from core.store.settings import save_setting

    await collections.save_one(_TOKEN_KEY, token_data)

    email = _email_from_token_data(token_data)
    if email:
        await save_setting(EMAIL_SETTING, email)
        from core import settings_runtime
        await settings_runtime.refresh()


async def materialise_mcp_dir() -> Path | None:
    """Write the stored credentials into the workspace-mcp scratch directory.

    Called immediately before the subprocess is spawned. Returns None when
    there is nothing to write, which is the signal not to start the server.
    """
    client = await load_client_config()
    if not client:
        return None

    token_data = await load_token() or {}
    directory = google_credentials_dir()
    try:
        (directory / "client_secret.json").write_text(
            json.dumps(client, indent=2), encoding="utf-8")
        if token_data:
            (directory / "token.json").write_text(
                json.dumps(token_data, indent=2), encoding="utf-8")
            email = _email_from_token_data(token_data)
            if email:
                (directory / f"{email}.json").write_text(
                    json.dumps(token_data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"Warning: could not materialise workspace-mcp credentials: {e}")
        return None
    return directory


async def get_google_credentials(refresh: bool = True):
    """Returns valid credentials or None.

    `refresh=False` asks whether the stored token is *currently* valid without
    performing a network refresh or a write. GET /api/config uses it: a status
    poll that refreshes a token and writes three files is a read endpoint with
    side effects, and the UI polls it on every visit to the integrations tab.
    """
    token_data = await load_token()
    if not token_data:
        return None

    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except ValueError as e:
        # A partial token — uploaded by hand, or written by an older version —
        # is "not connected", not an error. The contract here is valid
        # credentials or None, and /api/config renders the difference.
        print(f"Warning: stored Google token is not usable: {e}")
        return None

    if creds and creds.valid:
        return creds
    if not refresh:
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            refreshed = json.loads(creds.to_json())
            # Preserve fields like `email` that aren't part of creds.to_json()
            merged = {**token_data, **refreshed}
            email = token_data.get("email") or _email_from_token_data(refreshed)
            if email:
                merged["email"] = email
            await save_token(merged)
            return creds
        except Exception as e:
            print(f"Warning: Token refresh failed: {e}")

    return None


async def get_auth_url(redirect_uri):
    """The Google sign-in's address, with a random `state` of its own; its flow waits for that state's callback."""
    client_config = await load_client_config()
    if not client_config:
        raise FileNotFoundError(
            "Google credentials have not been configured. "
            "Please upload credentials.json via Settings → Integrations."
        )

    state = secrets.token_urlsafe(32)
    flow = Flow.from_client_config(
        client_config, SCOPES, redirect_uri=redirect_uri
    )
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=state,
    )
    now = _now()
    _forget_expired(now)
    while len(_pending) >= _PENDING_MAX:  # the oldest goes; a stranger cannot start one (F1535, /auth/login)
        _pending.pop(next(iter(_pending)))
    _pending[state] = (flow, now + PENDING_S)
    return auth_url

async def finish_auth(code, state):
    """Exchanges the code with the flow of the sign-in `state` belongs to (its code_verifier), and keeps the tokens.
    `SignInRefused`, before anything is exchanged or kept, for any other callback: no state, a wrong or used one, one
    past its 10 minutes, or none waiting. There is no fresh flow to fall back to (F1535)."""
    flow = _take(state)

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

    await save_token(token_data)
    await materialise_mcp_dir()
    return creds

async def get_service(api, version):
    """Returns an authorized service instance or raises UnauthenticatedError."""
    creds = await get_google_credentials()
    if not creds:
        raise UnauthenticatedError("User is not authenticated.")
    return build(api, version, credentials=creds)

async def get_gmail_service():
    """Returns an authorized Gmail API service instance."""
    return await get_service('gmail', 'v1')

async def get_drive_service():
    """Returns an authorized Drive API service instance."""
    return await get_service('drive', 'v3')

async def get_calendar_service():
    """Returns an authorized Calendar API service instance."""
    return await get_service('calendar', 'v3')

# --- Helper Functions ---

async def list_messages(query=None, limit=5):
    """Lists messages from the user's mailbox.
    
    Args:
        query: String query to filter messages (e.g., 'subject:insurance').
        limit: Max number of messages to return.
    """
    print(f"DEBUG: list_messages called with query='{query}', limit={limit} (type: {type(limit)})")
    try:
        service = await get_gmail_service()
        
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

async def get_message(message_id):
    """Get the full content of a message."""
    try:
        service = await get_gmail_service()
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

async def send_email(to, subject, body, cc=None, bcc=None):
    """Sends an email message.

    Args:
        to: Recipient email address.
        subject: Email subject.
        body: Email body text.
        cc: Optional CC recipient(s).
        bcc: Optional BCC recipient(s).
    """
    try:
        service = await get_gmail_service()
        
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
