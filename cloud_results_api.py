#!/usr/bin/env python3
"""
Cloud Results API v2
====================
Standalone Flask API that scrapes Racing Post race results.
Uses threading for fast parallel scraping (fits within Render's 30s timeout).
Background scraping + polling for cold-start resilience.

Endpoints:
    GET /api/results/<date>  →  JSON with horse positions and SP odds
    GET /api/health          →  Health check
    GET /                    →  Usage info
"""

import os
import re
import time
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, request as flask_request
from flask_cors import CORS
from lxml import html
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

# In-memory cache: {date_str: {status, data, timestamp, progress}}
CACHE = {}
LOCK = threading.Lock()

# User-agent rotation
UA_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]

import random
def get_headers():
    return {
        'User-Agent': random.choice(UA_LIST),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.9',
    }


def normalize_name(name):
    if not name: return ''
    name = name.lower().strip()
    name = re.sub(r'\s*\([a-z]+\)$', '', name, flags=re.IGNORECASE)
    return re.sub(r'[^a-z0-9]', '', name)


def get_race_urls(date_str):
    """Get all race result URLs for a date."""
    urls = []
    url = f'https://www.racingpost.com/results/{date_str}'
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200: return urls
        doc = html.fromstring(resp.content)
        links = doc.xpath('//a[contains(@href, "/results/")]/@href')
        for href in links:
            if '/results/' in href and href.count('/') >= 5:
                full = f'https://www.racingpost.com{href}' if not href.startswith('http') else href
                if full not in urls: urls.append(full)
    except Exception as e:
        print(f'[ERROR] get_race_urls: {e}')
    return urls


def parse_race(url):
    """Parse a single race result page."""
    runners = []
    try:
        resp = requests.get(url, headers=get_headers(), timeout=12)
        if resp.status_code != 200: return runners
        doc = html.fromstring(resp.content)
        parts = url.rstrip('/').split('/')
        course = parts[4].replace('-', ' ').title() if len(parts) > 4 else ''
        off_el = doc.xpath('//span[contains(@class, "rp-raceTimeCourseName__time")]')
        off_time = off_el[0].text_content().strip() if off_el else ''
        rows = doc.xpath('//tr[contains(@class, "rp-horseTable__mainRow")]')
        for row in rows:
            pos = 0
            pos_el = row.xpath('.//span[contains(@class, "rp-horseTable__pos__number")]')
            if pos_el:
                m = re.match(r'\d+', pos_el[0].text_content().strip())
                if m: pos = int(m.group())
            horse = ''
            horse_el = row.xpath('.//a[contains(@class, "rp-horseTable__horse__name")]')
            if horse_el: horse = horse_el[0].text_content().strip()
            dec = 0
            odds_el = row.xpath('.//span[contains(@class, "rp-horseTable__horse__price")]')
            if odds_el:
                odds_text = odds_el[0].text_content().strip().rstrip('FJCfjc').strip()
                try:
                    if odds_text.lower() in ('evs', 'evens', 'ev'): dec = 2.0
                    elif '/' in odds_text:
                        p = odds_text.split('/')
                        if len(p) == 2: dec = round(float(p[0]) / float(p[1]) + 1, 2)
                except: pass
            if horse:
                runners.append({'horse': horse, 'pos': pos, 'dec': dec, 'course': course, 'off': off_time})
    except Exception as e:
        print(f'[ERROR] parse_race {url}: {e}')
    return runners


def scrape_background(date_str):
    """Scrape all results in background using parallel threads."""
    with LOCK:
        if date_str in CACHE and CACHE[date_str].get('status') == 'scraping':
            return  # Already scraping
        CACHE[date_str] = {'status': 'scraping', 'data': {}, 'timestamp': time.time(), 'progress': 'Finding races...'}

    try:
        urls = get_race_urls(date_str)
        if not urls:
            with LOCK:
                CACHE[date_str] = {'status': 'done', 'data': {}, 'timestamp': time.time(), 'progress': 'No races found'}
            return

        with LOCK:
            CACHE[date_str]['progress'] = f'Scraping {len(urls)} races...'

        results = {}
        completed = 0

        # Parallel scraping with 8 threads (fast but polite)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_race, url): url for url in urls}
            for future in as_completed(futures):
                runners = future.result()
                for r in runners:
                    norm = normalize_name(r['horse'])
                    if norm:
                        results[norm] = {
                            'horse': r['horse'], 'pos': r['pos'], 'dec': r['dec'],
                            'course': r['course'], 'off': r['off'],
                        }
                completed += 1
                with LOCK:
                    CACHE[date_str]['data'] = dict(results)
                    CACHE[date_str]['progress'] = f'{completed}/{len(urls)} races done ({len(results)} horses)'

        with LOCK:
            CACHE[date_str] = {
                'status': 'done', 'data': results,
                'timestamp': time.time(),
                'progress': f'Complete: {len(results)} horses from {len(urls)} races'
            }
        print(f'[DONE] {date_str}: {len(results)} horses from {len(urls)} races')

    except Exception as e:
        print(f'[ERROR] scrape_background: {e}')
        with LOCK:
            CACHE[date_str] = {'status': 'error', 'data': {}, 'timestamp': time.time(), 'progress': str(e)}


# ─── API ROUTES ───

@app.route('/')
def index():
    return jsonify({
        'service': 'Q-Tips Results API v2',
        'usage': 'GET /api/results/<YYYY-MM-DD>',
        'example': '/api/results/2026-03-23',
    })


@app.route('/api/results/<date_str>')
def get_results(date_str):
    """Returns all horse results for a date. Triggers background scrape if needed."""
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    with LOCK:
        entry = CACHE.get(date_str)

    # If we have completed results and they're less than 5 min old, return them
    if entry and entry['status'] == 'done' and len(entry['data']) > 0:
        age = time.time() - entry['timestamp']
        return jsonify({
            'date': date_str, 'count': len(entry['data']),
            'status': 'done', 'cached': age < 300,
            'progress': entry['progress'],
            'results': entry['data'],
        })

    # If currently scraping, return partial results
    if entry and entry['status'] == 'scraping':
        return jsonify({
            'date': date_str, 'count': len(entry.get('data', {})),
            'status': 'scraping',
            'progress': entry.get('progress', 'Working...'),
            'results': entry.get('data', {}),
        })

    # Start background scrape
    t = threading.Thread(target=scrape_background, args=(date_str,), daemon=True)
    t.start()

    return jsonify({
        'date': date_str, 'count': 0,
        'status': 'scraping',
        'progress': 'Starting scrape...',
        'results': {},
    })


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.utcnow().isoformat()})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5123))
    print(f'Starting Results API v2 on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
