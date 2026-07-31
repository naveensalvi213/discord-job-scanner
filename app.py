import os
import sys

# Establish base directory for PyInstaller frozen exe or standard Python script
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

# Change current working directory to base directory so relative paths resolve to the app folder
os.chdir(base_dir)

# Prevent crash and capture tracebacks when running in background/packaged mode
if "pythonw" in sys.executable.lower() or getattr(sys, 'frozen', False):
    try:
        log_file = open("discord_scanner.log", "a", encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception as e:
        pass

import time
import datetime
import threading
import requests
import json
import subprocess
from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv

load_dotenv()

# Configure Flask template/static folders when running as a frozen executable
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

import re

def content_matches_keyword(content, keyword):
    content_lower = content.lower()
    kw_lower = keyword.lower()
    
    # 1. Direct substring match (fast path)
    if kw_lower in content_lower:
        return True
        
    # 2. Match words in any order, allowing extra words in between
    #    e.g. "looking for editor" matches "looking for a professional youtube editor"
    kw_words = [w.strip() for w in kw_lower.split() if w.strip()]
    if kw_words:
        all_words_present = True
        for word in kw_words:
            # Allow word with optional trailing 's', 'ed', 'ing', 'er' suffixes
            pattern = r"(?:^|\W)" + re.escape(word) + r"(?:s|ed|ing|er)?(?:$|\W)"
            if not re.search(pattern, content_lower):
                all_words_present = False
                break
        if all_words_present:
            return True
    
    # 3. Extract core nouns (non-stop-words) and check if ANY of them appear
    #    This ensures posts like "Hiring Video Editor" match when user set "need editor"
    stop_words = {"looking", "for", "need", "hiring", "i", "am", "a", "an", "the", "to", "want", "of", "in", "we", "are"}
    core_words = [w for w in kw_words if w not in stop_words]
    if core_words:
        # All core nouns must be present (e.g. "editor" from "looking for editor")
        all_core_present = True
        for word in core_words:
            pattern = r"(?:^|\W)" + re.escape(word) + r"(?:s|ed|ing|er)?(?:$|\W)"
            if not re.search(pattern, content_lower):
                all_core_present = False
                break
        if all_core_present:
            # At least one action word should also be present (hiring/need/looking etc.)
            action_words = {"looking", "need", "hiring", "hire", "seeking", "wanted", "want", "searching", "find"}
            has_action = any(re.search(r"(?:^|\W)" + re.escape(aw) + r"(?:s|ed|ing|er)?(?:$|\W)", content_lower) for aw in action_words)
            if has_action:
                return True
            
    return False

def show_windows_notification(title, message, launch_url="jobscanner://"):
    def run_notification():
        try:
            ps_script = f'''
            [void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms")
            $xml = @"
<toast launch="{launch_url}">
    <visual>
        <binding template="ToastGeneric">
            <text>{title}</text>
            <text>{message}</text>
        </binding>
    </visual>
</toast>
"@
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
            $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
            $doc.LoadXml($xml)
            $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{{1AC14E77-C6E7-43C8-99F4-2B854F90003D}}\\WindowsPowerShell\\v1.0\\powershell.exe")
            $toast = New-Object Windows.UI.Notifications.ToastNotification()
            $toast.Content = $doc
            $notifier.Show($toast)
            '''
            subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            print(f"Failed to show Windows notification: {e}")
    threading.Thread(target=run_notification, daemon=True).start()


def set_startup_shortcut(enable=True):
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return False
        startup_dir = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
        shortcut_path = os.path.join(startup_dir, "Discord Job Scanner Startup.lnk")
        if not enable:
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
            return True
        
        bat_path = r"c:\Users\navee\Downloads\discord\Run_Job_Scanner.bat"
        work_dir = r"c:\Users\navee\Downloads\discord"
        ps_script = f"""
        $WshShell = New-Object -ComObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
        $Shortcut.TargetPath = "{bat_path}"
        $Shortcut.WorkingDirectory = "{work_dir}"
        $Shortcut.IconLocation = "shell32.dll,23"
        $Shortcut.Save()
        """
        subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except Exception as e:
        print(f"Error setting startup shortcut: {e}")
        return False

# Global state to keep track of current scan progress and results
scan_state = {
    "is_running": False,
    "progress": "",
    "results": [],
    "total_servers": 0,
    "current_server_index": 0,
    "cancel_requested": False,
    "error": None
}

# Try to load last scan results from disk on startup
if os.path.exists("last_scan.json"):
    try:
        with open("last_scan.json", "r", encoding="utf-8") as f:
            scan_state["results"] = json.load(f)
    except Exception as e:
        print(f"Error loading last scan: {e}")

def save_last_scan(results):
    try:
        with open("last_scan.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
    except Exception as e:
        print(f"Error saving last scan: {e}")

def filter_and_deduplicate_results(results, hours_limit=24, deduplicate_authors=True):
    now = datetime.datetime.now(datetime.timezone.utc)
    filtered = []
    
    # 1. Filter by hours limit
    for item in results:
        ts_str = item.get("timestamp")
        if not ts_str:
            continue
        try:
            clean_ts = ts_str.replace("Z", "+00:00")
            msg_time = datetime.datetime.fromisoformat(clean_ts)
            age = now - msg_time
            if age.total_seconds() <= hours_limit * 3600:
                filtered.append(item)
        except Exception as e:
            print(f"Error parsing timestamp {ts_str}: {e}")
            filtered.append(item)

    if not deduplicate_authors:
        return filtered

    # 2. Deduplicate by author (keep latest post per author)
    author_latest = {}
    for item in filtered:
        author_id = item.get("author_id")
        if not author_id:
            continue
        existing = author_latest.get(author_id)
        if not existing:
            author_latest[author_id] = item
        else:
            try:
                exist_time = datetime.datetime.fromisoformat(existing.get("timestamp").replace("Z", "+00:00"))
                curr_time = datetime.datetime.fromisoformat(item.get("timestamp").replace("Z", "+00:00"))
                if curr_time > exist_time:
                    author_latest[author_id] = item
            except Exception:
                pass
                
    return list(author_latest.values())

KEYWORDS = [
    "LOOKING FOR EDITOR", "HIRING EDITOR", "NEED EDITOR", 
    "LOOKING FOR VIDEO EDITOR", "HIRING VIDEO EDITOR", "NEED VIDEO EDITOR", 
    "NEED THUMBNAIL", "LOOKING FOR THUMBNAIL", "HIRING THUMBNAIL"
]

def save_to_history(results):
    history_file = "scan_history.json"
    existing_history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                existing_history = json.load(f)
        except Exception as e:
            print(f"Error loading history: {e}")
            
    existing_ids = {item.get("message_id") for item in existing_history if item.get("message_id")}
    
    new_entries = []
    for item in results:
        if item.get("message_id") not in existing_ids:
            item["scanned_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            new_entries.append(item)
            
    if new_entries:
        existing_history.extend(new_entries)
        try:
            with open(history_file, "w") as f:
                json.dump(existing_history, f, indent=4)
        except Exception as e:
            print(f"Error saving history: {e}")

def get_optimized_search_queries(selected_keywords):
    stop_words = {"looking", "for", "need", "hiring", "i", "am", "a", "an", "the", "to", "want", "of", "in", "we", "are"}
    queries = set()
    for kw in selected_keywords:
        words = kw.lower().split()
        filtered = [w for w in words if w not in stop_words]
        if filtered:
            # We search the last word (usually the noun, like 'editor', 'thumbnail')
            queries.add(filtered[-1].upper())
        else:
            queries.add(kw.upper())
    return list(queries)

def get_headers(token):
    clean_token = token.strip().strip('"').strip("'") if token else ""
    return {
        "Authorization": clean_token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Connection": "close"
    }

# Watcher Global State
watcher_state = {
    "is_running": False,
    "thread": None,
    "token": "",
    "channels": [],      # List of strings
    "keywords": [],      # List of strings
    "excludes": [],      # List of strings
    "alerts": [],        # Queue of alert dicts for client retrieval
    "seen_message_ids": set()
}

def load_watcher_history():
    history_file = "watcher_seen_ids.json"
    data = {"initialized_channels": [], "seen_ids": []}
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data["initialized_channels"] = loaded.get("initialized_channels", [])
                    data["seen_ids"] = loaded.get("seen_ids", [])
        except Exception as e:
            print(f"Error loading watcher seen database: {e}")
    return data

def save_watcher_history(initialized_channels, seen_ids):
    history_file = "watcher_seen_ids.json"
    try:
        capped_seen_ids = list(seen_ids)[-1000:]
        data = {
            "initialized_channels": list(initialized_channels),
            "seen_ids": capped_seen_ids
        }
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving watcher seen database: {e}")

def load_author_cooldowns():
    cooldowns = {}
    history_file = "scan_history.json"
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
                for item in history:
                    author_id = item.get("author_id")
                    scanned_at_str = item.get("scanned_at")
                    if author_id and scanned_at_str:
                        try:
                            clean_ts = scanned_at_str.replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(clean_ts)
                            ts = dt.timestamp()
                            if author_id not in cooldowns or ts > cooldowns[author_id]:
                                cooldowns[author_id] = ts
                        except Exception:
                            pass
        except Exception as e:
            print(f"Error loading author cooldowns: {e}")
    return cooldowns

def send_discord_dm(token, recipient_id, content):
    clean_token = token.strip().strip('"').strip("'") if token else ""
    headers = {
        "Authorization": clean_token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Connection": "close"
    }
    try:
        # Step 1: Create or open direct DM channel
        dm_url = "https://discord.com/api/v9/users/@me/channels"
        dm_res = requests.post(dm_url, headers=headers, json={"recipient_id": str(recipient_id)}, timeout=10.0)
        
        if dm_res.status_code in [200, 201]:
            channel_id = dm_res.json().get("id")
            if channel_id:
                # Step 2: Send DM message
                msg_url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                msg_res = requests.post(msg_url, headers=headers, json={"content": content}, timeout=10.0)
                if msg_res.status_code in [200, 201]:
                    print(f"Successfully sent DM to {recipient_id}")
                else:
                    print(f"Failed to send DM message. Code: {msg_res.status_code}, Response: {msg_res.text}")
            else:
                print("Failed to retrieve DM channel ID from Discord response.")
        else:
            print(f"Failed to open DM channel with {recipient_id}. Code: {dm_res.status_code}, Response: {dm_res.text}")
    except Exception as e:
        print(f"Exception during send_discord_dm: {e}")

def watcher_worker():
    global watcher_state
    
    print("Real-time Watcher thread started.")
    
    # Load seen state from disk
    db = load_watcher_history()
    initialized_channels = set(db.get("initialized_channels", []))
    watcher_state["seen_message_ids"] = set(db.get("seen_ids", []))
    author_cooldowns = load_author_cooldowns()
    
    while watcher_state["is_running"]:
        channels_to_check = list(watcher_state["channels"])
        token = watcher_state["token"]
        keywords = list(watcher_state["keywords"])
        excludes = list(watcher_state["excludes"])
        
        if not token or not channels_to_check or not keywords:
            time.sleep(2)
            continue
            
        headers = get_headers(token)
        db_changed = False
        
        for channel_id in channels_to_check:
            if not watcher_state["is_running"]:
                break
                
            try:
                # Fetch up to 15 recent messages to catch offline matches
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=15"
                res = requests.get(url, headers=headers)
                
                if res.status_code == 429:
                    retry_after = res.json().get("retry_after", 3.0)
                    time.sleep(retry_after)
                    continue
                    
                if res.status_code != 200:
                    continue
                    
                messages = res.json()
                
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                
                for msg in messages:
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                        
                    if msg_id in watcher_state["seen_message_ids"]:
                        continue
                        
                    # Mark message as seen
                    watcher_state["seen_message_ids"].add(msg_id)
                    db_changed = True
                    
                    # Parse message timestamp — skip messages older than 30 minutes (1800s)
                    msg_ts_str = msg.get("timestamp")
                    if msg_ts_str:
                        try:
                            clean_ts = msg_ts_str.replace("Z", "+00:00")
                            msg_dt = datetime.datetime.fromisoformat(clean_ts)
                            if msg_dt.tzinfo is None:
                                msg_dt = msg_dt.replace(tzinfo=datetime.timezone.utc)
                            if (now_utc - msg_dt).total_seconds() > 1800:
                                continue
                        except Exception:
                            pass
                        
                    # Skip reply threads/comments to keep notifications clean
                    if msg.get("type") == 19 or msg.get("referenced_message") is not None:
                        continue
                        
                    content = msg.get("content", "")
                    content_lower = content.lower()
                    
                    matched_keyword = None
                    for kw in keywords:
                        if content_matches_keyword(content, kw.strip()):
                            matched_keyword = kw.strip()
                            break
                            
                    if not matched_keyword:
                        continue
                        
                    has_exclude = False
                    for ex in excludes:
                        ex_clean = ex.strip()
                        if len(ex_clean) < 2:
                            continue  # Skip single-char excludes like '?' that are too broad
                        if ex_clean.lower() in content_lower:
                            has_exclude = True
                            break
                            
                    if has_exclude:
                        continue
                        
                    guild_name = "Guild Channel"
                    channel_name = f"Channel {channel_id}"
                    guild_id = None
                    try:
                        ch_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}", headers=headers)
                        if ch_res.status_code == 200:
                            ch_data = ch_res.json()
                            guild_id = ch_data.get("guild_id")
                            channel_name = f"#{ch_data.get('name')}" if ch_data.get('name') else channel_name
                            if guild_id:
                                g_res = requests.get(f"https://discord.com/api/v9/guilds/{guild_id}", headers=headers)
                                if g_res.status_code == 200:
                                    guild_name = g_res.json().get("name", guild_name)
                            else:
                                guild_name = "Direct Message"
                    except Exception as e:
                        print(f"Error fetching channel/guild names: {e}")
                        
                    author = msg.get("author", {})
                    username = author.get("username", "Unknown")
                    discriminator = author.get("discriminator", "0000")
                    full_username = f"{username}#{discriminator}" if discriminator != "0" else username
                    author_id = author.get("id")
                    
                    # 24-hour duplicate author rate limit filter
                    if author_id:
                        now_ts = time.time()
                        if author_id in author_cooldowns:
                            time_elapsed = now_ts - author_cooldowns[author_id]
                            if time_elapsed < 86400:
                                # Skip match to avoid repeating the same person twice in 24 hours
                                continue
                    
                    alert = {
                        "server_id": guild_id or "@me",
                        "server_name": guild_name,
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "username": full_username,
                        "author_id": author_id,
                        "message_id": msg_id,
                        "content": content,
                        "timestamp": msg.get("timestamp"),
                        "keyword": matched_keyword,
                        "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }
                    
                    watcher_state["alerts"].append(alert)
                    save_to_history([alert])
                    
                    if author_id:
                        author_cooldowns[author_id] = time.time()
                    
                    notification_text = f"From {full_username} in {channel_name} ({guild_name}): {content[:80]}..."
                    show_windows_notification(f"Match: {matched_keyword}", notification_text, launch_url=f"jobscanner://{msg_id}")
                    
                    friend_id = watcher_state.get("friend_id", "1491339066053230673")
                    if friend_id:
                        dm_text = (
                            f"🚨 **New Job Match!**\n"
                            f"**Server:** {guild_name}\n"
                            f"**Channel:** {channel_name}\n"
                            f"**Posted By:** {full_username}\n"
                            f"**Keyword:** {matched_keyword}\n\n"
                            f"**Post Details:**\n{content}"
                        )
                        if len(dm_text) > 1950:
                            over_limit = len(dm_text) - 1950
                            trimmed_content = content[:-over_limit] + "..."
                            dm_text = (
                                f"🚨 **New Job Match!**\n"
                                f"**Server:** {guild_name}\n"
                                f"**Channel:** {channel_name}\n"
                                f"**Posted By:** {full_username}\n"
                                f"**Keyword:** {matched_keyword}\n\n"
                                f"**Post Details:**\n{trimmed_content}"
                            )
                        threading.Thread(target=send_discord_dm, args=(token, friend_id, dm_text), daemon=True).start()
                    
            except Exception as e:
                print(f"Watcher error scanning channel {channel_id}: {e}")
                
            time.sleep(1.0)
            
        if db_changed:
            save_watcher_history(initialized_channels, watcher_state["seen_message_ids"])
            
        time.sleep(5.0)

def scan_worker(token, selected_keywords, exclude_keywords, channel_ids=None, hours_limit=24):
    global scan_state
    scan_state["is_running"] = True
    scan_state["progress"] = "Starting scan..."
    scan_state["results"] = []
    scan_state["cancel_requested"] = False
    scan_state["error"] = None
    
    headers = get_headers(token)
    seen_message_ids = set()
    
    # Get current date in UTC
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    
    # If channel_ids are specified, scan only those channels directly
    if channel_ids:
        scan_state["total_servers"] = len(channel_ids)
        scan_state["current_server_index"] = 0
        
        for idx, channel_id in enumerate(channel_ids):
            if scan_state["cancel_requested"]:
                scan_state["progress"] = "Scan stopped by user."
                break
                
            scan_state["current_server_index"] = idx + 1
            
            # Resolve name and guild name locally or via API if possible
            channel_name = f"Channel {channel_id}"
            guild_name = "Guild"
            guild_id = None
            
            try:
                ch_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}", headers=headers)
                if ch_res.status_code == 401:
                    scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                    scan_state["progress"] = "Scan failed."
                    scan_state["is_running"] = False
                    return
                if ch_res.status_code == 200:
                    ch_info = ch_res.json()
                    guild_id = ch_info.get("guild_id")
                    channel_name = f"#{ch_info.get('name')}" if ch_info.get('name') else channel_name
                    if guild_id:
                        g_res = requests.get(f"https://discord.com/api/v9/guilds/{guild_id}", headers=headers)
                        if g_res.status_code == 401:
                            scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                            scan_state["progress"] = "Scan failed."
                            scan_state["is_running"] = False
                            return
                        if g_res.status_code == 200:
                            guild_name = g_res.json().get("name", guild_name)
            except Exception as e:
                print(f"Error fetching channel metadata for {channel_id}: {e}")
                
            # Try local lookup as fallback
            if guild_name == "Guild" or channel_name == f"Channel {channel_id}":
                local_info = find_channel_details_locally(channel_id)
                if local_info:
                    channel_name = local_info.get("channel_name", channel_name)
                    guild_name = local_info.get("guild_name", guild_name)
                    guild_id = local_info.get("guild_id", guild_id)

            scan_state["progress"] = f"Scanning '{channel_name}' in '{guild_name}' ({idx + 1}/{len(channel_ids)})..."
            
            try:
                time.sleep(1.0)
                msg_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=100", headers=headers)
                
                if msg_res.status_code == 401:
                    scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                    scan_state["progress"] = "Scan failed."
                    scan_state["is_running"] = False
                    return
                    
                if msg_res.status_code == 429:
                    retry_after = msg_res.json().get("retry_after", 3.0)
                    scan_state["progress"] = f"Rate limited. Waiting {retry_after}s..."
                    time.sleep(retry_after + 1.0)
                    msg_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=100", headers=headers)
                    if msg_res.status_code == 401:
                        scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                        scan_state["progress"] = "Scan failed."
                        scan_state["is_running"] = False
                        return
                    
                if msg_res.status_code != 200:
                    continue
                    
                messages = msg_res.json()
                for msg in messages:
                    if msg.get("type") == 19 or msg.get("referenced_message") is not None:
                        continue
                        
                    content = msg.get("content", "")
                    content_lower = content.lower()
                    
                    matched_keyword = None
                    for kw in selected_keywords:
                        if content_matches_keyword(content, kw):
                            matched_keyword = kw
                            break
                            
                    if not matched_keyword:
                        continue
                        
                    has_exclude = False
                    for ex_kw in exclude_keywords:
                        ex_clean = ex_kw.strip()
                        if len(ex_clean) < 2:
                            continue  # Skip single-char excludes like '?' that are too broad
                        if ex_clean.lower() in content_lower:
                            has_exclude = True
                            break
                    if has_exclude:
                        continue
                        
                    ts_str = msg.get("timestamp")
                    if not ts_str:
                        continue
                        
                    try:
                        author = msg.get("author", {})
                        username = author.get("username", "Unknown")
                        discriminator = author.get("discriminator", "0000")
                        full_username = f"{username}#{discriminator}" if discriminator != "0" else username
                        author_id = author.get("id")
                        
                        scan_state["results"].append({
                            "server_id": guild_id or "@me",
                            "server_name": guild_name,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "username": full_username,
                            "author_id": author_id,
                            "message_id": msg.get("id"),
                            "content": content,
                            "timestamp": ts_str,
                            "keyword": matched_keyword
                        })
                    except Exception as e:
                        print(f"Error parsing message {msg.get('id')}: {e}")
            except Exception as e:
                print(f"Error direct scanning channel {channel_id}: {e}")
                    
        filtered = filter_and_deduplicate_results(scan_state["results"], hours_limit)
        scan_state["results"] = filtered
        save_last_scan(filtered)
        save_to_history(filtered)
        print(f"Scan completed! Found {len(filtered)} results (before dedup: {len(scan_state['results'])} raw results)")
        scan_state["progress"] = f"Scan completed! Found {len(filtered)} results."
        scan_state["is_running"] = False
        return

    # 1. Fetch user's servers (guilds)
    try:
        scan_state["progress"] = "Fetching servers..."
        res = requests.get("https://discord.com/api/v9/users/@me/guilds", headers=headers)
        if res.status_code == 401:
            scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
            scan_state["progress"] = "Scan failed."
            scan_state["is_running"] = False
            return
        elif res.status_code != 200:
            scan_state["error"] = f"Failed to fetch servers. API status: {res.status_code}"
            scan_state["progress"] = "Scan failed."
            scan_state["is_running"] = False
            return
        
        guilds = res.json()
    except Exception as e:
        scan_state["progress"] = f"Error fetching servers: {str(e)}"
        scan_state["is_running"] = False
        return

    scan_state["total_servers"] = len(guilds)
    scan_state["current_server_index"] = 0
    
    for idx, guild in enumerate(guilds):
        if scan_state["cancel_requested"]:
            scan_state["progress"] = "Scan stopped by user."
            break

        scan_state["current_server_index"] = idx + 1
        guild_id = guild["id"]
        guild_name = guild["name"]
        scan_state["progress"] = f"Scanning '{guild_name}' ({idx + 1}/{len(guilds)})..."
        
        for keyword in selected_keywords:
            if scan_state["cancel_requested"]:
                scan_state["progress"] = "Scan stopped by user."
                break

        for query in get_optimized_search_queries(selected_keywords):
            if scan_state["cancel_requested"]:
                break

            try:
                # Add a sleep to prevent aggressive scraping and rate limits (429)
                time.sleep(1.5)
                
                search_url = f"https://discord.com/api/v9/guilds/{guild_id}/messages/search"
                params = {
                    "content": query,
                    "sort_by": "timestamp",
                    "sort_order": "desc"
                }
                search_res = requests.get(search_url, headers=headers, params=params)
                if search_res.status_code == 401:
                    scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                    scan_state["progress"] = "Scan failed."
                    scan_state["is_running"] = False
                    return
                
                if search_res.status_code == 429:
                    retry_after = search_res.json().get("retry_after", 5.0)
                    scan_state["progress"] = f"Rate limited. Waiting {retry_after}s..."
                    time.sleep(retry_after + 1.0)
                    search_res = requests.get(search_url, headers=headers, params=params)
                    if search_res.status_code == 401:
                        scan_state["error"] = "Discord Token is invalid or expired (401). Please check your account token."
                        scan_state["progress"] = "Scan failed."
                        scan_state["is_running"] = False
                        return
                    
                if search_res.status_code != 200:
                    continue
                
                data = search_res.json()
                for message_group in data.get("messages", []):
                    for msg in message_group:
                        if msg.get("hit") is True:
                            msg_id = msg.get("id")
                            if msg_id in seen_message_ids:
                                continue
                                
                            # 1. Skip comments / replies (type 19 is REPLY, or if referenced_message is set)
                            if msg.get("type") == 19 or msg.get("referenced_message") is not None:
                                continue
                                
                            # 2. Check if content contains any excluded keywords (case-insensitive)
                            content = msg.get("content", "")
                            content_lower = content.lower()
                            
                            # Verify local match of user phrases
                            matched_keyword = None
                            for kw in selected_keywords:
                                if content_matches_keyword(content, kw):
                                    matched_keyword = kw
                                    break
                            
                            if not matched_keyword:
                                continue
                                
                            has_exclude = False
                            for ex_kw in exclude_keywords:
                                ex_clean = ex_kw.strip()
                                if len(ex_clean) < 2:
                                    continue  # Skip single-char excludes like '?' that are too broad
                                if ex_clean.lower() in content_lower:
                                    has_exclude = True
                                    break
                            if has_exclude:
                                continue

                            ts_str = msg.get("timestamp")
                            if not ts_str:
                                continue
                            try:
                                channel_id = msg.get("channel_id")
                                author = msg.get("author", {})
                                username = author.get("username", "Unknown")
                                discriminator = author.get("discriminator", "0000")
                                full_username = f"{username}#{discriminator}" if discriminator != "0" else username
                                author_id = author.get("id")
                                
                                scan_state["results"].append({
                                    "server_id": guild_id,
                                    "server_name": guild_name,
                                    "channel_id": channel_id,
                                    "channel_name": f"Channel ID: {channel_id}",
                                    "username": full_username,
                                    "author_id": author_id,
                                    "message_id": msg.get("id"),
                                    "content": content,
                                    "timestamp": ts_str,
                                    "keyword": matched_keyword
                                })
                            except Exception as e:
                                print(f"Error parsing guild msg: {e}")
                                
            except Exception as e:
                print(f"Error searching query '{query}' in guild '{guild_name}': {e}")
                
    filtered = filter_and_deduplicate_results(scan_state["results"], hours_limit)
    scan_state["results"] = filtered
    save_last_scan(filtered)
    save_to_history(filtered)
    scan_state["progress"] = "Scan completed!"
    scan_state["is_running"] = False
    scan_state["cancel_requested"] = False
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scan", methods=["POST"])
def start_scan():
    global scan_state
    if scan_state["is_running"]:
        return jsonify({"status": "error", "message": "A scan is already in progress."}), 400
        
    data = request.json or {}
    token = data.get("token")
    if not token:
        return jsonify({"status": "error", "message": "Token is required."}), 400
        
    selected_keywords = data.get("keywords", KEYWORDS)
    exclude_keywords = data.get("exclude_keywords", [])
    channel_ids = data.get("channel_ids", [])
    
    # Save the configuration to config.json for daily automation tasks
    try:
        config_data = {
            "token": token,
            "keywords": selected_keywords,
            "exclude_keywords": exclude_keywords,
            "channel_ids": channel_ids
        }
        with open("config.json", "w") as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")
        
    # Start scanning in background thread
    hours_limit = data.get("hours_limit", 24)
    threading.Thread(target=scan_worker, args=(token, selected_keywords, exclude_keywords, channel_ids, hours_limit), daemon=True).start()
    
    return jsonify({"status": "success", "message": "Scan started."})

@app.route("/api/stop", methods=["POST"])
def stop_scan():
    global scan_state
    if scan_state["is_running"]:
        scan_state["cancel_requested"] = True
        scan_state["progress"] = "Stopping scan..."
        return jsonify({"status": "success", "message": "Cancellation request received."})
    return jsonify({"status": "error", "message": "No scan is currently running."}), 400

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify(scan_state)



@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    history_file = "scan_history.json"
    try:
        if os.path.exists(history_file):
            os.remove(history_file)
        return jsonify({"status": "success", "message": "History cleared."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    print("Shutting down scanner server on request...")
    # Clean shutdown using a background thread exit
    def term():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=term, daemon=True).start()
    return jsonify({"status": "success", "message": "Server shutting down."})

def find_channel_details_locally(channel_id):
    channel_id_str = str(channel_id).strip()
    
    # 1. Search scan_history.json
    if os.path.exists("scan_history.json"):
        try:
            with open("scan_history.json", "r", encoding="utf-8") as f:
                history = json.load(f)
                for item in history:
                    if str(item.get("channel_id")).strip() == channel_id_str:
                        ch_name = item.get("channel_name")
                        g_name = item.get("server_name")
                        if ch_name and g_name:
                            return {"channel_name": ch_name.replace("#", ""), "guild_name": g_name}
        except Exception as e:
            print(f"Error searching scan_history: {e}")

    # 2. Search poster_history.json
    if os.path.exists("poster_history.json"):
        try:
            with open("poster_history.json", "r", encoding="utf-8") as f:
                history = json.load(f)
                for item in history:
                    if str(item.get("channel_id")).strip() == channel_id_str:
                        ch_name = item.get("channel_name")
                        g_name = item.get("guild_name")
                        if ch_name and g_name:
                            return {"channel_name": ch_name.replace("#", ""), "guild_name": g_name}
        except Exception as e:
            print(f"Error searching poster_history: {e}")

    # 3. Search data/backup_storage.json (other accounts' channels)
    backup_file = os.path.join("data", "backup_storage.json")
    if os.path.exists(backup_file):
        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                storage = json.load(f)
                for ac in storage.get("accounts", []):
                    for ch in ac.get("poster_channels", []):
                        if str(ch.get("id")).strip() == channel_id_str:
                            ch_name = ch.get("name")
                            g_name = ch.get("guild_name")
                            if ch_name and g_name:
                                return {"channel_name": ch_name.replace("#", ""), "guild_name": g_name}
                    for ch in ac.get("watcher_channels", []):
                        if isinstance(ch, dict) and str(ch.get("id")).strip() == channel_id_str:
                            ch_name = ch.get("name")
                            g_name = ch.get("guild_name")
                            if ch_name and g_name:
                                return {"channel_name": ch_name.replace("#", ""), "guild_name": g_name}
        except Exception as e:
            print(f"Error searching backup_storage: {e}")

    return None

@app.route("/api/channel/<channel_id>", methods=["GET"])
def get_channel_info(channel_id):
    token = request.headers.get("Authorization")
    if not token:
        return jsonify({"status": "error", "message": "Token is required in Authorization header."}), 400
        
    headers = get_headers(token)
    try:
        res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}", headers=headers)
        if res.status_code == 200:
            channel = res.json()
            guild_id = channel.get("guild_id")
            guild_name = "Direct Message / Unknown Server"
            if guild_id:
                g_res = requests.get(f"https://discord.com/api/v9/guilds/{guild_id}", headers=headers)
                if g_res.status_code == 200:
                    guild_name = g_res.json().get("name", "Unknown Server")
            
            return jsonify({
                "status": "success",
                "id": channel_id,
                "name": channel.get("name", "Unnamed Channel"),
                "type": channel.get("type", 0), # 0 = Text, 15 = Forum
                "guild_id": guild_id,
                "guild_name": guild_name,
                "available_tags": channel.get("available_tags", [])
            })
        else:
            # Try to resolve channel details from local history
            local_info = find_channel_details_locally(channel_id)
            if local_info:
                return jsonify({
                    "status": "success",
                    "id": channel_id,
                    "name": local_info["channel_name"],
                    "type": 0,
                    "guild_id": None,
                    "guild_name": local_info["guild_name"],
                    "available_tags": [],
                    "is_fallback": True,
                    "restored_from_history": True
                })
                
            status_text = "Forbidden (403)" if res.status_code == 403 else ("Not Found (404)" if res.status_code == 404 else f"Error ({res.status_code})")
            return jsonify({
                "status": "success",
                "id": channel_id,
                "name": f"Channel {channel_id} ({status_text})",
                "type": 0,
                "guild_id": None,
                "guild_name": "Inaccessible / External Server",
                "available_tags": [],
                "is_fallback": True,
                "fallback_error": f"Failed to fetch details from Discord API. Code: {res.status_code}"
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def find_last_user_thread(token, channel_id):
    headers = get_headers(token)
    user_id = None
    try:
        me_res = requests.get("https://discord.com/api/v9/users/@me", headers=headers)
        if me_res.status_code == 200:
            user_id = me_res.json().get("id")
    except Exception as e:
        print(f"Error resolving self in user thread lookup: {e}")
        
    if not user_id:
        return None

    try:
        th_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}/threads/active", headers=headers)
        if th_res.status_code == 200:
            for thread in th_res.json().get("threads", []):
                if thread.get("owner_id") == user_id:
                    return thread.get("id")
    except Exception as e:
        print(f"Error fetching active threads for {channel_id}: {e}")
    return None

def find_last_user_message(token, channel_id):
    headers = get_headers(token)
    user_id = None
    try:
        me_res = requests.get("https://discord.com/api/v9/users/@me", headers=headers)
        if me_res.status_code == 200:
            user_id = me_res.json().get("id")
    except Exception as e:
        print(f"Error resolving self in user message lookup: {e}")
        
    if not user_id:
        return None

    try:
        msg_res = requests.get(f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=50", headers=headers)
        if msg_res.status_code == 200:
            for msg in msg_res.json():
                if msg.get("author", {}).get("id") == user_id:
                    return msg.get("id")
    except Exception as e:
        print(f"Error fetching messages in {channel_id}: {e}")
    return None

def save_poster_history_record(record):
    history_file = "poster_history.json"
    existing_history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                existing_history = json.load(f)
        except Exception as e:
            print(f"Error loading poster history: {e}")
            
    record["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    existing_history.append(record)
    
    if len(existing_history) > 200:
        existing_history = existing_history[-200:]
        
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(existing_history, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving poster history: {e}")

@app.route("/api/poster/history", methods=["GET"])
def get_poster_history():
    history_file = "poster_history.json"
    data = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error reading poster history: {e}")
    return jsonify(list(reversed(data)))

@app.route("/api/poster/history/clear", methods=["POST"])
def clear_poster_history():
    history_file = "poster_history.json"
    try:
        if os.path.exists(history_file):
            os.remove(history_file)
        return jsonify({"status": "success", "message": "Poster campaign history cleared."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def format_discord_error(error_detail):
    if not isinstance(error_detail, dict):
        return str(error_detail)
    
    code = error_detail.get("code")
    msg = error_detail.get("message", "")
    
    if code == 50013:
        return "Missing Permissions (Cannot write in this channel)"
    elif code == 50001:
        return "Missing Access (Not in this server/channel)"
    elif code == 10008:
        return "Target post/message has been deleted from Discord"
    elif code == 10003:
        return "Target channel was deleted or archived"
    elif code == 40007:
        return "Cannot link/reply to a message in a different channel"
    elif code == 40002:
        return "Account is rate limited/locked or verification required"
    
    return f"{msg} (Code {code})" if code else msg

@app.route("/api/bump", methods=["POST"])
def bump_ad():
    token = request.form.get("token") or (request.json.get("token") if request.json else None)
    channel_id = request.form.get("channel_id") or (request.json.get("channel_id") if request.json else None)
    bump_text = request.form.get("bump_text", "bump") or (request.json.get("bump_text", "bump") if request.json else "bump")
    last_posted_id = request.form.get("last_posted_id") or (request.json.get("last_posted_id") if request.json else None)
    is_forum_str = request.form.get("is_forum", "false") or (request.json.get("is_forum", "false") if request.json else "false")
    is_forum = str(is_forum_str).lower() == "true"
    
    # Metadata for logs
    account_name = request.form.get("account_name") or (request.json.get("account_name") if request.json else None) or "Unknown Account"
    channel_name = request.form.get("channel_name") or (request.json.get("channel_name") if request.json else None) or f"Channel {channel_id}"
    guild_name = request.form.get("guild_name") or (request.json.get("guild_name") if request.json else None) or "Guild"

    if not token or not channel_id:
        return jsonify({"status": "error", "message": "Token and Channel ID are required."}), 400

    headers = get_headers(token)

    target_id = last_posted_id
    if not target_id:
        if is_forum:
            target_id = find_last_user_thread(token, channel_id)
        else:
            target_id = find_last_user_message(token, channel_id)

    if not target_id:
        record = {
            "account_name": account_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "guild_name": guild_name,
            "mode": "bump",
            "status": "failed",
            "message_id": "",
            "error_reason": "Could not locate your previous post/thread to bump"
        }
        save_poster_history_record(record)
        return jsonify({
            "status": "error", 
            "message": "Could not locate your previous post/thread to bump. Please publish the ad first."
        }), 404

    try:
        if is_forum:
            url = f"https://discord.com/api/v9/channels/{target_id}/messages"
            payload = {"content": bump_text}
            res = requests.post(url, headers=headers, json=payload)
        else:
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
            payload = {
                "content": bump_text,
                "message_reference": {
                    "channel_id": channel_id,
                    "message_id": target_id
                }
            }
            res = requests.post(url, headers=headers, json=payload)

        if res.status_code in [200, 201]:
            record = {
                "account_name": account_name,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "guild_name": guild_name,
                "mode": "bump",
                "status": "success",
                "message_id": target_id,
                "error_reason": ""
            }
            save_poster_history_record(record)
            return jsonify({"status": "success", "message": "Post bumped successfully!", "id": target_id})
        else:
            try:
                err = res.json()
            except:
                err = res.text
                
            error_reason = format_discord_error(err)
                
            record = {
                "account_name": account_name,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "guild_name": guild_name,
                "mode": "bump",
                "status": "failed",
                "message_id": "",
                "error_reason": error_reason
            }
            save_poster_history_record(record)
            return jsonify({"status": "error", "message": f"Discord error: {res.status_code}", "detail": err}), res.status_code
    except Exception as e:
        record = {
            "account_name": account_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "guild_name": guild_name,
            "mode": "bump",
            "status": "failed",
            "message_id": "",
            "error_reason": str(e)
        }
        save_poster_history_record(record)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/post", methods=["POST"])
def post_ad():
    token = request.form.get("token")
    channel_id = request.form.get("channel_id")
    content = request.form.get("content")
    is_forum_str = request.form.get("is_forum", "false")
    is_forum = is_forum_str.lower() == "true"
    
    # Metadata for logs
    account_name = request.form.get("account_name", "Unknown Account")
    channel_name = request.form.get("channel_name", f"Channel {channel_id}")
    guild_name = request.form.get("guild_name", "Guild")
    mode = request.form.get("mode", "global")
    
    # Forum parameters
    title = request.form.get("title", "")
    tags_str = request.form.get("tags", "")
    applied_tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
    
    if not token or not channel_id:
        return jsonify({"status": "error", "message": "Token and Channel ID are required."}), 400
        
    headers = {
        "Authorization": token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    preset_name = request.form.get("preset_name")
    use_preset_files = request.form.get("use_preset_files", "false") == "true"
    use_preset_cover = request.form.get("use_preset_cover", "false") == "true"
    use_channel_preset_files = request.form.get("use_channel_preset_files", "false") == "true"
    
    files_dict = {}
    idx = 0
    
    # 1. First, load cover files if enabled (so they appear first and serve as the cover card image)
    if use_preset_cover and preset_name:
        cover_dir = os.path.join("data", "presets", preset_name, "cover")
        if os.path.exists(cover_dir):
            for fname in os.listdir(cover_dir):
                fpath = os.path.join(cover_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        mimetype = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
                        files_dict[f"files[{idx}]"] = (fname, f.read(), mimetype)
                        idx += 1
                        
    for f in request.files.getlist("cover_files"):
        if f and f.filename:
            files_dict[f"files[{idx}]"] = (f.filename, f.read(), f.mimetype)
            idx += 1
            
    # 2. Next, load global or channel preset files
    if use_channel_preset_files and preset_name:
        ch_dir = os.path.join("data", "presets", preset_name, "channels", str(channel_id))
        if os.path.exists(ch_dir):
            for fname in os.listdir(ch_dir):
                fpath = os.path.join(ch_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        mimetype = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
                        files_dict[f"files[{idx}]"] = (fname, f.read(), mimetype)
                        idx += 1
    elif use_preset_files and preset_name:
        global_dir = os.path.join("data", "presets", preset_name, "global")
        if os.path.exists(global_dir):
            for fname in os.listdir(global_dir):
                fpath = os.path.join(global_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        mimetype = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
                        files_dict[f"files[{idx}]"] = (fname, f.read(), mimetype)
                        idx += 1
                        
    # Add any newly uploaded request files
    for key in ["file", "files"]:
        for f in request.files.getlist(key):
            if f and f.filename:
                files_dict[f"files[{idx}]"] = (f.filename, f.read(), f.mimetype)
                idx += 1
                
    try:
        if is_forum:
            url = f"https://discord.com/api/v9/channels/{channel_id}/threads"
            payload = {
                "name": title or "Advertisement",
                "applied_tags": applied_tags,
                "message": {
                    "content": content or ""
                }
            }
            if files_dict:
                res = requests.post(url, headers=headers, data={"payload_json": json.dumps(payload)}, files=files_dict)
            else:
                headers["Content-Type"] = "application/json"
                res = requests.post(url, headers=headers, json=payload)
        else:
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
            payload = {
                "content": content or "",
                "tts": False
            }
            if files_dict:
                res = requests.post(url, headers=headers, data={"payload_json": json.dumps(payload)}, files=files_dict)
            else:
                headers["Content-Type"] = "application/json"
                res = requests.post(url, headers=headers, json=payload)
                
        if res.status_code in [200, 201]:
            res_data = res.json()
            record = {
                "account_name": account_name,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "guild_name": guild_name,
                "mode": mode,
                "status": "success",
                "message_id": res_data.get("id"),
                "error_reason": ""
            }
            save_poster_history_record(record)
            return jsonify({"status": "success", "message": "Ad posted successfully!", "id": res_data.get("id"), "response": res_data})
        else:
            try:
                error_detail = res.json()
            except:
                error_detail = res.text
                
            error_reason = format_discord_error(error_detail)
                
            record = {
                "account_name": account_name,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "guild_name": guild_name,
                "mode": mode,
                "status": "failed",
                "message_id": "",
                "error_reason": error_reason
            }
            save_poster_history_record(record)
            return jsonify({"status": "error", "message": f"Discord returned error: {res.status_code}", "detail": error_detail}), res.status_code
            
    except Exception as e:
        record = {
            "account_name": account_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "guild_name": guild_name,
            "mode": mode,
            "status": "failed",
            "message_id": "",
            "error_reason": str(e)
        }
        save_poster_history_record(record)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/storage/save", methods=["POST"])
def save_storage():
    try:
        data = request.get_json()
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "backup_storage.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return jsonify({"status": "success", "message": "Storage backup saved to disk successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/storage/load", methods=["GET"])
def load_storage():
    try:
        path = os.path.join("data", "backup_storage.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return jsonify({"status": "success", "data": data})
        else:
            return jsonify({"status": "success", "data": None})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/presets", methods=["GET"])
def list_presets():
    try:
        presets_dir = os.path.join("data", "presets")
        if not os.path.exists(presets_dir):
            return jsonify({"status": "success", "presets": []})
            
        presets_list = []
        for name in os.listdir(presets_dir):
            p_path = os.path.join(presets_dir, name)
            if os.path.isdir(p_path):
                meta_file = os.path.join(p_path, "preset.json")
                if os.path.exists(meta_file):
                    with open(meta_file, "r", encoding="utf-8") as f:
                        preset_data = json.load(f)
                    presets_list.append(preset_data)
        return jsonify({"status": "success", "presets": presets_list})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/presets/save", methods=["POST"])
def save_preset():
    try:
        name = request.form.get("name")
        if not name:
            return jsonify({"status": "error", "message": "Preset name is required"}), 400
            
        draft_content = request.form.get("draft_content", "")
        draft_forum_title = request.form.get("draft_forum_title", "")
        poster_channels_json = request.form.get("poster_channels", "[]")
        poster_channels = json.loads(poster_channels_json)
        
        # File retention logic
        keep_global = json.loads(request.form.get("keep_global_files", "[]"))
        keep_cover = json.loads(request.form.get("keep_cover_files", "[]"))
        keep_channels = json.loads(request.form.get("keep_channel_files", "{}")) # ch_id -> [filenames]
        
        preset_dir = os.path.join("data", "presets", name)
        
        # We will backup files we want to keep before deleting the directory
        import shutil
        temp_keep_dir = os.path.join("data", "presets_temp_keep")
        if os.path.exists(temp_keep_dir):
            shutil.rmtree(temp_keep_dir)
        os.makedirs(temp_keep_dir, exist_ok=True)
        
        # Move kept global files to temp
        global_src = os.path.join(preset_dir, "global")
        if os.path.exists(global_src):
            for fname in keep_global:
                src_file = os.path.join(global_src, fname)
                if os.path.exists(src_file):
                    os.makedirs(os.path.join(temp_keep_dir, "global"), exist_ok=True)
                    shutil.copy2(src_file, os.path.join(temp_keep_dir, "global", fname))
                    
        # Move kept cover files to temp
        cover_src = os.path.join(preset_dir, "cover")
        if os.path.exists(cover_src):
            for fname in keep_cover:
                src_file = os.path.join(cover_src, fname)
                if os.path.exists(src_file):
                    os.makedirs(os.path.join(temp_keep_dir, "cover"), exist_ok=True)
                    shutil.copy2(src_file, os.path.join(temp_keep_dir, "cover", fname))
                    
        # Move kept channel files to temp
        for ch_id_str, fnames in keep_channels.items():
            ch_src = os.path.join(preset_dir, "channels", ch_id_str)
            if os.path.exists(ch_src):
                for fname in fnames:
                    src_file = os.path.join(ch_src, fname)
                    if os.path.exists(src_file):
                        os.makedirs(os.path.join(temp_keep_dir, "channels", ch_id_str), exist_ok=True)
                        shutil.copy2(src_file, os.path.join(temp_keep_dir, "channels", ch_id_str, fname))
                        
        # Delete old preset dir completely and recreate
        if os.path.exists(preset_dir):
            shutil.rmtree(preset_dir)
        os.makedirs(preset_dir, exist_ok=True)
        
        # 1. Save global files
        global_dir = os.path.join(preset_dir, "global")
        os.makedirs(global_dir, exist_ok=True)
        global_filenames = list(keep_global)
        
        # Restore kept global files from temp
        temp_global_dir = os.path.join(temp_keep_dir, "global")
        if os.path.exists(temp_global_dir):
            for fname in os.listdir(temp_global_dir):
                shutil.move(os.path.join(temp_global_dir, fname), os.path.join(global_dir, fname))
                
        # Save newly uploaded global files
        for f in request.files.getlist("global_files"):
            if f and f.filename:
                f.save(os.path.join(global_dir, f.filename))
                if f.filename not in global_filenames:
                    global_filenames.append(f.filename)
                    
        # 2. Save cover files
        cover_dir = os.path.join(preset_dir, "cover")
        os.makedirs(cover_dir, exist_ok=True)
        cover_filenames = list(keep_cover)
        
        # Restore kept cover files from temp
        temp_cover_dir = os.path.join(temp_keep_dir, "cover")
        if os.path.exists(temp_cover_dir):
            for fname in os.listdir(temp_cover_dir):
                shutil.move(os.path.join(temp_cover_dir, fname), os.path.join(cover_dir, fname))
                
        # Save newly uploaded cover files
        for f in request.files.getlist("cover_files"):
            if f and f.filename:
                f.save(os.path.join(cover_dir, f.filename))
                if f.filename not in cover_filenames:
                    cover_filenames.append(f.filename)
                    
        # 3. Save channel custom files
        for ch in poster_channels:
            ch_id = str(ch.get("id"))
            ch_dir = os.path.join(preset_dir, "channels", ch_id)
            os.makedirs(ch_dir, exist_ok=True)
            
            # Get existing filenames that we want to keep
            ch_filenames = list(keep_channels.get(ch_id, []))
            
            # Restore kept channel files from temp
            temp_ch_dir = os.path.join(temp_keep_dir, "channels", ch_id)
            if os.path.exists(temp_ch_dir):
                for fname in os.listdir(temp_ch_dir):
                    shutil.move(os.path.join(temp_ch_dir, fname), os.path.join(ch_dir, fname))
                    
            # Save newly uploaded channel files
            for f in request.files.getlist(f"channel_files_{ch_id}"):
                if f and f.filename:
                    f.save(os.path.join(ch_dir, f.filename))
                    if f.filename not in ch_filenames:
                        ch_filenames.append(f.filename)
            ch["preset_filenames"] = ch_filenames
            
        # Clean up temp keep dir
        if os.path.exists(temp_keep_dir):
            shutil.rmtree(temp_keep_dir)
            
        # Create metadata
        preset_meta = {
            "name": name,
            "draft_content": draft_content,
            "draft_forum_title": draft_forum_title,
            "global_filenames": global_filenames,
            "cover_filenames": cover_filenames,
            "poster_channels": poster_channels
        }
        
        with open(os.path.join(preset_dir, "preset.json"), "w", encoding="utf-8") as f:
            json.dump(preset_meta, f, indent=4)
            
        return jsonify({"status": "success", "preset": preset_meta})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/presets/delete", methods=["POST"])
def delete_preset():
    try:
        data = request.get_json() or {}
        name = data.get("name")
        if not name:
            return jsonify({"status": "error", "message": "Preset name is required"}), 400
            
        preset_dir = os.path.join("data", "presets", name)
        import shutil
        if os.path.exists(preset_dir):
            shutil.rmtree(preset_dir)
            return jsonify({"status": "success", "message": f"Preset '{name}' deleted successfully."})
        else:
            return jsonify({"status": "error", "message": f"Preset '{name}' not found."}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/open_link", methods=["POST"])
def open_link():
    try:
        data = request.get_json() or {}
        url = data.get("url")
        browser = data.get("browser", "default")
        
        if not url:
            return jsonify({"status": "error", "message": "URL is required"}), 400
            
        if not (url.startswith("http://") or url.startswith("https://")):
            return jsonify({"status": "error", "message": "Invalid URL protocol"}), 400
            
        import subprocess
        if browser == "discord_app":
            app_url = url.replace("https://", "discord://").replace("http://", "discord://")
            os.startfile(app_url)
        elif browser == "edge":
            os.startfile(f"microsoft-edge:{url}")
        elif browser == "chrome":
            chrome_paths = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            ]
            launched = False
            for path in chrome_paths:
                if os.path.exists(path):
                    subprocess.Popen([path, url])
                    launched = True
                    break
            if not launched:
                subprocess.Popen(['cmd.exe', '/c', f'start chrome "{url}"'], shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import webbrowser
            webbrowser.open(url)
            
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/watcher/start", methods=["POST"])
def start_watcher():
    global watcher_state
    data = request.get_json() or {}
    token = data.get("token")
    channels = data.get("channels", [])
    keywords = data.get("keywords", [])
    excludes = data.get("excludes", [])
    friend_id = data.get("friend_id", "1491339066053230673")
    
    if not token:
        return jsonify({"status": "error", "message": "Token is required."}), 400
        
    watcher_state["token"] = token
    watcher_state["channels"] = channels
    watcher_state["keywords"] = keywords
    watcher_state["excludes"] = excludes
    watcher_state["friend_id"] = friend_id
    
    if not watcher_state["is_running"]:
        watcher_state["is_running"] = True
        watcher_state["alerts"] = []
        watcher_state["thread"] = threading.Thread(target=watcher_worker, daemon=True)
        watcher_state["thread"].start()
        
    return jsonify({"status": "success", "message": "Watcher started/updated successfully."})

@app.route("/api/watcher/stop", methods=["POST"])
def stop_watcher():
    global watcher_state
    watcher_state["is_running"] = False
    return jsonify({"status": "success", "message": "Watcher stop requested."})

@app.route("/api/watcher/status", methods=["GET"])
def watcher_status():
    global watcher_state
    return jsonify({
        "status": "success",
        "is_running": watcher_state["is_running"]
    })

@app.route("/api/watcher/alerts", methods=["GET"])
def get_watcher_alerts():
    global watcher_state
    current_alerts = list(watcher_state["alerts"])
    watcher_state["alerts"] = []
    return jsonify({
        "status": "success",
        "alerts": current_alerts
    })

@app.route("/api/sync-config", methods=["POST"])
def sync_config():
    global watcher_state
    try:
        data = request.get_json() or {}
        token = data.get("token")
        channels = data.get("channels", [])
        keywords = data.get("keywords", [])
        excludes = data.get("excludes", [])
        
        if not token:
            return jsonify({"status": "error", "message": "Token is required"}), 400
            
        watcher_state["token"] = token
        watcher_state["channels"] = [str(c) for c in channels]
        watcher_state["keywords"] = keywords
        watcher_state["excludes"] = excludes
        
        config_data = {
            "token": token,
            "channel_ids": watcher_state["channels"],
            "keywords": keywords,
            "exclude_keywords": excludes
        }
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
            
        thread = watcher_state.get("thread")
        thread_alive = thread is not None and thread.is_alive()
        if not watcher_state["is_running"] or not thread_alive:
            watcher_state["is_running"] = True
            watcher_state["alerts"] = []
            t = threading.Thread(target=watcher_worker, daemon=True)
            watcher_state["thread"] = t
            t.start()
            
        return jsonify({"status": "success", "message": "Configuration synchronized and watcher started."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/alerts", methods=["GET"])
def get_api_alerts():
    if not watcher_state.get("is_running") or watcher_state.get("thread") is None or not watcher_state["thread"].is_alive():
        check_and_auto_start_watcher()

    since_str = request.args.get("since")
    limit_str = request.args.get("limit")
    
    history_file = "scan_history.json"
    alerts = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                alerts = json.load(f)
        except Exception as e:
            print(f"Error loading scan history: {e}")
            
    if since_str:
        try:
            since_ts = float(since_str)
        except ValueError:
            try:
                since_ts = datetime.datetime.fromisoformat(since_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                since_ts = None
                
        if since_ts is not None:
            filtered = []
            for item in alerts:
                scanned_at_str = item.get("scanned_at")
                if scanned_at_str:
                    try:
                        item_ts = datetime.datetime.fromisoformat(scanned_at_str.replace("Z", "+00:00")).timestamp()
                        if item_ts > since_ts:
                            filtered.append(item)
                    except Exception:
                        pass
            alerts = filtered
            
    if limit_str:
        try:
            limit = int(limit_str)
            alerts = alerts[-limit:]
        except ValueError:
            pass
            
    return jsonify({"status": "success", "alerts": alerts})

def check_and_auto_start_watcher():
    # Skip duplicate startup in Flask debug reloader's parent process
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    # Don't start if thread is already alive
    thread = watcher_state.get("thread")
    if thread is not None and thread.is_alive():
        return
        
    config_file = "config.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            token = cfg.get("token")
            channels = cfg.get("channel_ids") or cfg.get("channels", [])
            keywords = cfg.get("keywords", [])
            excludes = cfg.get("exclude_keywords") or cfg.get("excludes", [])
            friend_id = cfg.get("friend_id", "1491339066053230673")
            
            if token and channels and keywords:
                print("Auto-starting real-time watcher from config.json...")
                watcher_state["token"] = token
                watcher_state["channels"] = [str(c) for c in channels]
                watcher_state["keywords"] = keywords
                watcher_state["excludes"] = excludes
                watcher_state["friend_id"] = friend_id
                watcher_state["is_running"] = True
                watcher_state["alerts"] = []
                watcher_state["thread"] = threading.Thread(target=watcher_worker, daemon=True)
                watcher_state["thread"].start()
                return
        except Exception as e:
            print(f"Error auto-starting watcher from config.json: {e}")

    backup_file = os.path.join("data", "backup_storage.json")
    if os.path.exists(backup_file):
        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            accounts = data.get("accounts", [])
            for ac in accounts:
                if ac.get("auto_start_watcher") and ac.get("token") and ac.get("watcher_channels"):
                    print(f"Auto-starting real-time watcher for account: {ac.get('name')}")
                    watcher_state["token"] = ac.get("token")
                    
                    plain_channels = []
                    for ch in ac.get("watcher_channels", []):
                        if isinstance(ch, dict):
                            plain_channels.append(ch.get("id"))
                        else:
                            plain_channels.append(str(ch))
                            
                    watcher_state["channels"] = plain_channels
                    watcher_state["keywords"] = ac.get("watcher_keywords", [])
                    watcher_state["excludes"] = ac.get("watcher_excludes", [])
                    watcher_state["friend_id"] = ac.get("watcher_friend_id", "1491339066053230673")
                    watcher_state["is_running"] = True
                    watcher_state["alerts"] = []
                    watcher_state["thread"] = threading.Thread(target=watcher_worker, daemon=True)
                    watcher_state["thread"].start()
                    break
        except Exception as e:
            print(f"Error auto-starting watcher: {e}")

@app.route("/api/startup/status", methods=["GET"])
def get_startup_status():
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return jsonify({
                "status": "success",
                "enabled": False
            })
        startup_dir = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
        shortcut_path = os.path.join(startup_dir, "Discord Job Scanner Startup.lnk")
        return jsonify({
            "status": "success",
            "enabled": os.path.exists(shortcut_path)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/startup/toggle", methods=["POST"])
def toggle_startup():
    try:
        data = request.get_json() or {}
        enable = data.get("enable", False)
        success = set_startup_shortcut(enable)
        if success:
            return jsonify({"status": "success", "message": f"Startup shortcut {'created' if enable else 'removed'} successfully."})
        else:
            return jsonify({"status": "error", "message": "Failed to update startup shortcut."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def check_deleted_posts_worker():
    # Run slightly delayed on startup, then every 60 seconds
    time.sleep(10)
    while True:
        try:
            history_file = "poster_history.json"
            if not os.path.exists(history_file):
                time.sleep(60)
                continue
                
            # Load accounts to get tokens
            tokens = {}
            backup_file = os.path.join("data", "backup_storage.json")
            if os.path.exists(backup_file):
                try:
                    with open(backup_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        for ac in data.get("accounts", []):
                            if ac.get("name") and ac.get("token"):
                                tokens[ac["name"]] = ac["token"]
                except Exception as e:
                    print(f"Error loading accounts in deleted posts checker: {e}")
            
            # Load history
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception as e:
                print(f"Error loading history in deleted posts checker: {e}")
                time.sleep(60)
                continue
                
            changed = False
            now = datetime.datetime.now(datetime.timezone.utc)
            
            for record in history:
                if record.get("status") == "success" and record.get("message_id") and record.get("channel_id"):
                    # Only check posts from the last 24 hours
                    ts_str = record.get("timestamp")
                    if ts_str:
                        try:
                            # Parse ISO format. Handle Z timezone offset
                            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=datetime.timezone.utc)
                            if (now - ts).total_seconds() > 86400: # older than 24 hours
                                continue
                        except Exception as e:
                            continue
                    
                    account_name = record.get("account_name")
                    token = tokens.get(account_name) or watcher_state.get("token")
                    if not token:
                        continue
                        
                    headers = get_headers(token)
                    channel_id = record["channel_id"]
                    message_id = record["message_id"]
                    
                    url = f"https://discord.com/api/v9/channels/{channel_id}/messages/{message_id}"
                    try:
                        time.sleep(1.0) # sleep to prevent aggressive API hits
                        res = requests.get(url, headers=headers)
                        if res.status_code == 404:
                            record["status"] = "deleted"
                            record["error_reason"] = "Message deleted on Discord (or channel/permissions removed)"
                            changed = True
                            print(f"Detected deleted post: message {message_id} in channel {channel_id}")
                        elif res.status_code == 403:
                            record["status"] = "deleted"
                            record["error_reason"] = "Message inaccessible (403 Forbidden / Missing Permissions)"
                            changed = True
                            print(f"Detected inaccessible post: message {message_id} in channel {channel_id}")
                    except Exception as e:
                        print(f"Error verifying message {message_id}: {e}")
                        
            if changed:
                try:
                    with open(history_file, "w", encoding="utf-8") as f:
                        json.dump(history, f, ensure_ascii=False, indent=4)
                except Exception as e:
                    print(f"Error saving history in deleted posts checker: {e}")
                    
            time.sleep(60)
        except Exception as e:
            print(f"Error in deleted posts checker loop: {e}")
            time.sleep(60)

# Start the background deleted posts checker
threading.Thread(target=check_deleted_posts_worker, daemon=True).start()

# Trigger watcher check on load
check_and_auto_start_watcher()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
