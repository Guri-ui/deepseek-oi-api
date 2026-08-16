#!/usr/bin/env python3
"""
DeepSeek Authentication Script using Camoufox.
Launches an anti-detect browser with local binaries to capture user credentials and session tokens.
"""

import os
import sys
import json
import time
from pathlib import Path

# Configure local browser paths strictly inside project directory
BASE_DIR = Path(__file__).parent.resolve()
BROWSERS_DIR = BASE_DIR / "browsers"
BROWSERS_DIR.mkdir(parents=True, exist_ok=True)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
os.environ["XDG_CACHE_HOME"] = str(BROWSERS_DIR)

import camoufox.pkgman as pkgman
import camoufox.multiversion as multiversion

pkgman.INSTALL_DIR = BROWSERS_DIR / "camoufox"
multiversion.CONFIG_DIR = BROWSERS_DIR / "camoufox_config"
multiversion.VERSIONS_DIR = BROWSERS_DIR / "camoufox_versions"

# Ensure Camoufox binaries are installed in local browsers directory
try:
    pkgman.installed_verstr()
except Exception:
    print("[*] Downloading Camoufox browser binaries into local browsers/ directory...")
    pkgman.CamoufoxFetcher().install()

from camoufox.sync_api import Camoufox

TOKEN_FILE = BASE_DIR / "deepseek_token.json"

def save_credentials(token: str, user_info: dict = None, cookies: list = None):
    data = {
        "userToken": token,
        "userInfo": user_info or {},
        "cookies": cookies or [],
        "updatedAt": int(time.time()),
        "authHeader": f"Bearer {token}"
    }
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\n[+] Credentials successfully saved to: {TOKEN_FILE}")
    print(f"[+] Token preview: {token[:12]}...{token[-8:]}")

def run_login():
    print("=========================================================")
    print("           DeepSeek Authentication Helper                ")
    print("=========================================================")
    print(f"[*] Browser binaries directory: {BROWSERS_DIR}")
    print("[*] Launching browser window...")
    print("[*] Please log into your DeepSeek account in the browser.")
    print("    (Email / Password, SMS Code, Google, or Apple login)")
    print("---------------------------------------------------------")

    with Camoufox(headless=False, humanize=True) as browser:
        page = browser.new_page()
        page.goto("https://chat.deepseek.com", wait_until="domcontentloaded")

        print("[*] Waiting for login completion...")
        user_token = None
        user_info = None

        while True:
            try:
                # Check localStorage for userToken
                token_raw = page.evaluate("() => localStorage.getItem('userToken')")
                if token_raw:
                    try:
                        token_obj = json.loads(token_raw)
                        token_val = token_obj.get("value")
                        if token_val and len(token_val) > 10:
                            user_token = token_val
                            user_info_raw = page.evaluate("() => localStorage.getItem('__appKit_userInfo')")
                            if user_info_raw:
                                user_info = json.loads(user_info_raw)
                            break
                    except Exception:
                        pass

                # Check if current URL indicates logged-in chat
                current_url = page.url
                if "/a/chat" in current_url and not user_token:
                    # Retry reading token
                    token_raw = page.evaluate("() => localStorage.getItem('userToken')")
                    if token_raw:
                        token_obj = json.loads(token_raw)
                        user_token = token_obj.get("value")
                        if user_token:
                            break

                time.sleep(1)
            except KeyboardInterrupt:
                print("\n[!] Login cancelled by user.")
                return False
            except Exception as e:
                time.sleep(1)

        print("\n[+] Login detected successfully!")
        cookies = page.context.cookies()
        save_credentials(user_token, user_info, cookies)
        page.wait_for_timeout(1000)
        return True

if __name__ == "__main__":
    success = run_login()
    if not success:
        sys.exit(1)
