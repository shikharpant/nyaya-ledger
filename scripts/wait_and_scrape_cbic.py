#!/usr/bin/env python3
"""Wait for CBIC server to unblock our IP, then start scraper at gentle rate."""

import json
import subprocess
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()

BASE = "https://taxinformation.cbic.gov.in"
CHECK_INTERVAL = 300  # 5 min between checks
TARGET_SUCCESS = 10   # need 10 consecutive successes before starting
SCRAPER_DELAY = 1.0   # 1s between requests (original working rate)


def test_connection():
    try:
        r = requests.post(f"{BASE}/api/authenticate-token", json={}, verify=False, timeout=15)
        token = r.json()["id_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        # Test 1 API call
        r = requests.get(f"{BASE}/api/cbic-form-msts/1000001", headers=headers, verify=False, timeout=15)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    return False


def main():
    print(f"Waiting for CBIC server to unblock us...")
    print(f"Checking every {CHECK_INTERVAL}s, need {TARGET_SUCCESS} consecutive successes")
    print(f"Will start scraper at --delay {SCRAPER_DELAY}s")
    
    consecutive = 0
    attempt = 0
    
    while consecutive < TARGET_SUCCESS:
        attempt += 1
        t = time.strftime("%H:%M:%S")
        
        if test_connection():
            consecutive += 1
            print(f"  [{t}] attempt #{attempt}: OK ({consecutive}/{TARGET_SUCCESS})", flush=True)
            if consecutive < TARGET_SUCCESS:
                time.sleep(15)  # 15s between successful checks
        else:
            if consecutive > 0:
                print(f"  [{t}] attempt #{attempt}: FAILED (reset from {consecutive})", flush=True)
            else:
                print(f"  [{t}] attempt #{attempt}: blocked", flush=True)
            consecutive = 0
            time.sleep(CHECK_INTERVAL)
    
    print(f"\nServer is stable! Starting scraper at {time.strftime('%H:%M:%S')}")
    
    cmd = [
        sys.executable, "-u", "scripts/scrape_cbic_tax_portal.py",
        "--delay", str(SCRAPER_DELAY),
        "--retries", "8",
        "--what", "forms,orders,instructions,notifications,circulars",
    ]
    print(f"Running: {' '.join(cmd)}")
    subprocess.execvp(sys.executable, cmd)


if __name__ == "__main__":
    main()
