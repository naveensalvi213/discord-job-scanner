import sys
import os

# Establish base directory for PyInstaller frozen exe or standard Python script
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

# Set current working directory
os.chdir(base_dir)

# Set persistent pywebview user data directory in AppData to prevent loss of localStorage/settings
lock_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'DiscordJobScanner')
os.makedirs(lock_dir, exist_ok=True)
os.environ['PYWEBVIEW_USER_DATA_DIR'] = lock_dir

import socket
import threading
import time
import requests
import webview

window = None
window_is_open = True

def register_protocol():
    import sys
    if sys.platform != 'win32':
        return
    import winreg
    try:
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
            key_path = r"Software\Classes\jobscanner"
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Job Scanner Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            
            cmd_key = winreg.CreateKey(key, r"shell\open\command")
            winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, f'"{exe_path}" "%1"')
            
            winreg.CloseKey(cmd_key)
            winreg.CloseKey(key)
            print("Protocol registered successfully.")
    except Exception as e:
        print(f"Error registering protocol: {e}")

def find_free_port():
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def start_flask(port):
    from app import app
    from flask import request
    
    @app.route('/api/focus', methods=['POST'])
    def focus_window():
        global window, window_is_open
        try:
            if window and window_is_open:
                window.show()
                window.restore()
                
                data = request.json or {}
                url_param = data.get("url", "")
                if url_param.startswith("jobscanner://"):
                    msg_id = url_param.replace("jobscanner://", "").strip("/")
                    if msg_id:
                        def trigger_highlight():
                            time.sleep(0.5)
                            window.evaluate_js(f"if (typeof highlightMessage === 'function') highlightMessage('{msg_id}');")
                        threading.Thread(target=trigger_highlight, daemon=True).start()
                return {"status": "success", "window_alive": True}
        except Exception as e:
            print(f"Error focusing window: {e}")
        return {"status": "success", "window_alive": False}

    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    register_protocol()
    
    # Clean the WebView2 cache to force new templates/css/js to load
    import shutil
    eb_webview_path = os.path.join(lock_dir, 'EBWebView')
    if os.path.exists(eb_webview_path):
        try:
            shutil.rmtree(eb_webview_path)
        except Exception:
            pass
            
    lock_file = os.path.join(lock_dir, 'app.lock')
    existing_port = None
    
    run_flask = True
    
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                existing_port = int(f.read().strip())
        except Exception:
            pass
            
    if existing_port:
        # Check if existing server is responsive and focus it
        try:
            res = requests.get(f'http://127.0.0.1:{existing_port}/api/watcher/status', timeout=0.5)
            if res.status_code == 200:
                url_arg = sys.argv[1] if len(sys.argv) > 1 else ""
                focus_res = requests.post(f'http://127.0.0.1:{existing_port}/api/focus', json={"url": url_arg}, timeout=1.0)
                if focus_res.json().get("window_alive"):
                    sys.exit(0)
                else:
                    # Background process is running. Attach new GUI to it.
                    port = existing_port
                    run_flask = False
        except Exception:
            # Shutdown the old server if it is zombie/unresponsive to release port
            try:
                requests.post(f'http://127.0.0.1:{existing_port}/api/shutdown', timeout=0.5)
                time.sleep(0.3)
            except Exception:
                pass
            run_flask = True
            port = find_free_port()
    else:
        run_flask = True
        port = find_free_port()

    if run_flask:
        try:
            with open(lock_file, 'w') as f:
                f.write(str(port))
        except Exception as e:
            print(f"Error writing lock file: {e}")
            
        # Start Flask in a background daemon thread
        flask_thread = threading.Thread(target=start_flask, args=(port,), daemon=True)
        flask_thread.start()
        
        # Wait for the Flask server to be fully active and responsive (up to 8 seconds)
        server_ready = False
        for _ in range(80):
            try:
                res = requests.get(f'http://127.0.0.1:{port}/api/watcher/status', timeout=0.2)
                if res.status_code == 200:
                    server_ready = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
            
        if not server_ready:
            print("Warning: Flask server did not start within 8 seconds.")
    
    # Create the webview window pointing to the active port
    window = webview.create_window(
        title='Discord Job Scanner & Poster',
        url=f'http://127.0.0.1:{port}',
        width=1250,
        height=880,
        resizable=True,
        min_size=(900, 600)
    )
    
    # Start webview loop (blocks until window is closed)
    webview.start()
    window_is_open = False
    
    # If the real-time watcher is currently active, we keep the server running in the background
    if run_flask:
        try:
            res = requests.get(f'http://127.0.0.1:{port}/api/watcher/status', timeout=1.0)
            status = res.json()
            if status.get('is_running'):
                print("Watcher is active. Background server will continue running.")
                while True:
                    time.sleep(5)
                    try:
                        chk = requests.get(f'http://127.0.0.1:{port}/api/watcher/status', timeout=1.0)
                        if not chk.json().get('is_running'):
                            break
                    except Exception:
                        break
        except Exception:
            pass

    os._exit(0)
