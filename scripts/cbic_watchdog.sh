#!/bin/bash
cd /home/shikhar/openclaw-workspace/Projects/Git_for_Law
while true; do
    echo "$(date): starting scraper..." >> /tmp/cbic_watchdog.log
    python3 -u scripts/scrape_cbic_tax_portal.py --delay 60 --retries 1 --what notifications,circulars,orders,instructions >> /tmp/cbic_scrape.log 2>&1
    EXIT=$?
    echo "$(date): scraper exited (code=$EXIT), restarting in 10s..." >> /tmp/cbic_watchdog.log
    sleep 10
done
