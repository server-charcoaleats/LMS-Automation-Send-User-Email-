import requests
import time
import json
import os
from datetime import datetime, timedelta
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError

# === Configuration ===
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

EXCLUDE_USERS = {"Administrator", "Guest"}
BATCH_SIZE = 3
REQUEST_TIMEOUT = 25  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

STATE_DIR = ".state"
STATE_FILE = os.path.join(STATE_DIR, "user_batch_state.json")

# === Helpers to save/load state ===
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {
        "last_index": 0,
        "email_sent_log": {}
    }

def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# === Fetch all users ===
def fetch_all_users():
    print("📥 Fetching all users...")
    url = f"{BASE_URL}/api/resource/User"
    params = {
        "fields": '["name","email","enabled","last_password_reset_date","new_password"]',
        "limit_page_length": 1000
    }
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        return res.json().get("data", [])
    except Exception as e:
        print(f"❌ Failed to fetch users: {e}")
        return []

# === Send reset link with retry logic ===
def send_reset_link(email):
    url = f"{BASE_URL}/api/method/frappe.core.doctype.user.user.reset_password"
    data = {"user": email}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=HEADERS, json=data, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            print(f"📧 Reset link sent to '{email}'")
            return True
        except (ReadTimeout, ConnectionError) as net_err:
            print(f"⚠ Network issue on attempt {attempt} for {email}: {net_err}")
        except HTTPError as http_err:
            if response.status_code == 429:
                print(f"⛔ Rate limit hit for {email}. Waiting {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"❌ HTTP error for {email}: {http_err}")
                break
        except Exception as e:
            print(f"❌ Unexpected error for {email}: {e}")
            break

        if attempt < MAX_RETRIES:
            print(f"🔁 Retrying in {RETRY_DELAY} seconds...")
            time.sleep(RETRY_DELAY)

    print(f"❌ Failed to send reset link to '{email}' after {MAX_RETRIES} attempts.")
    return False

# === Main process ===
def process_users():
    state = load_state()
    last_index = state.get("last_index", 0)
    email_sent_log = state.get("email_sent_log", {})

    users = fetch_all_users()
    if not users:
        print("No users fetched, exiting.")
        return

    now = datetime.utcnow()

    # Filter users: must have email, not excluded, and no password set
    filtered_users = [
        u for u in users
        if u.get("email")
        and u.get("name") not in EXCLUDE_USERS
        and not u.get("new_password")  # <- user should not already have a password
    ]

    if not filtered_users:
        print("No eligible users found.")
        return

    wait_users = []
    ready_users = []
    new_users = []

    for user in filtered_users:
        email = user["email"]
        last_sent_str = email_sent_log.get(email)
        if last_sent_str:
            try:
                last_sent = datetime.fromisoformat(last_sent_str)
                if (now - last_sent) < timedelta(hours=72):
                    wait_users.append(user)
                else:
                    ready_users.append(user)
            except Exception:
                ready_users.append(user)
        else:
            new_users.append(user)

    candidates = ready_users + new_users

    if last_index >= len(candidates):
        print("Reached end of candidate list. Resetting index to 0.")
        last_index = 0

    batch_users = candidates[last_index:last_index + BATCH_SIZE]
    if not batch_users:
        print("No users to process in this batch.")
        return

    print(f"📦 Processing users {last_index + 1} to {last_index + len(batch_users)} out of {len(candidates)}")

    for user in batch_users:
        email = user["email"]
        print(f"✅ Sending reset link to: {email}")
        if send_reset_link(email):
            email_sent_log[email] = now.isoformat()
        time.sleep(2)

    new_index = last_index + BATCH_SIZE
    if new_index >= len(candidates):
        new_index = 0

    state["last_index"] = new_index
    state["email_sent_log"] = email_sent_log
    save_state(state)

    print(f"✅ Batch complete. Processed {len(batch_users)} users.")

if __name__ == "__main__":
    process_users()
