import requests
import time
import json
import os
from datetime import datetime, timedelta
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError

# === Configuration ===
BASE_URL = "https://charcoaleatstraining.frappe.cloud"
API_KEY = "d021e4abe9699fa"
API_SECRET = "360bc4bc0e1f2d2"

HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

EXCLUDE_USERS = {"Administrator", "Guest"}
BATCH_SIZE = 3
DELAY_BETWEEN_BATCHES = 15 * 60  # 15 minutes in seconds
REQUEST_TIMEOUT = 25  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries
STATE_FILE = "user_batch_state.json"

# === Helpers to save/load state ===
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    # Default state structure
    return {
        "last_index": 0,
        "email_sent_log": {}  # email => last_sent_iso_str
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# === Fetch all users ===
def fetch_all_users():
    print("📥 Fetching all users...")
    url = f"{BASE_URL}/api/resource/User"
    params = {
        "fields": '["name","email","enabled","last_password_reset_date"]',
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

    # Filter users: exclude admin/guest and those with password already set
    filtered_users = [
        u for u in users
        if u.get("email") and
        u.get("name") not in EXCLUDE_USERS and
        not u.get("last_password_reset_date")  # no recent reset
    ]

    total_users = len(filtered_users)
    if total_users == 0:
        print("No eligible users found.")
        return

    now = datetime.utcnow()

    # Split users into 2 categories:
    # 1. Users who had email sent before but less than 72 hours ago (wait)
    # 2. Users who had email sent before and 72+ hours passed (ready)
    # 3. Users who never had email sent (new)
    wait_users = []
    ready_users = []
    new_users = []

    for user in filtered_users:
        email = user["email"]
        last_sent_str = email_sent_log.get(email)
        if last_sent_str:
            try:
                last_sent = datetime.fromisoformat(last_sent_str)
            except Exception:
                last_sent = None

            if last_sent and (now - last_sent) < timedelta(hours=72):
                wait_users.append(user)  # not ready yet
            else:
                ready_users.append(user)  # ready for next email
        else:
            new_users.append(user)  # never sent before

    # Priority: send to ready_users first, then new_users
    candidates = ready_users + new_users

    if last_index >= len(candidates):
        print("Reached end of candidates list, resetting index to 0.")
        last_index = 0

    batch_users = candidates[last_index:last_index + BATCH_SIZE]
    if not batch_users:
        print("No users to process in this batch.")
        return

    print(f"Processing users {last_index + 1} to {last_index + len(batch_users)} out of {len(candidates)}.")

    for user in batch_users:
        email = user["email"]
        print(f"✅ Sending reset link to: {email}")
        success = send_reset_link(email)
        if success:
            email_sent_log[email] = now.isoformat()
        time.sleep(2)

    # Save updated state
    new_index = last_index + BATCH_SIZE
    if new_index >= len(candidates):
        new_index = 0  # wrap around

    state["last_index"] = new_index
    state["email_sent_log"] = email_sent_log
    save_state(state)

    print(f"Batch complete. Processed {len(batch_users)} users.")

if __name__ == "__main__":
    process_users()
