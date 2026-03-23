#!/usr/bin/env python3
"""
Cloud Results API
=================
A standalone Flask API that scrapes Racing Post race results for a given date.
No local dependencies — designed for deployment to Render.com / Railway / etc.

Endpoint:
    GET /api/results/<date>   →  JSON with all horse positions and SP odds

Example:
    curl https://your-app.onrender.com/api/results/2026-03-23
"""

import os
import re
import time
import requests
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS
from lxml import html

app = Flask(__name__)
CORS(app)

# In-memory cache: {date_str: {timestamp, data}}
CACHE = {}
CACHE_TTL = 300  # 5 minutes

# Random user-agent rotation
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
        'Accept-Encoding': 'gzip, deflate, br',
    }


def normalize_name(name):
    """Normalize horse name for matching."""
    if not name:
        return ''
    name = name.lower().strip()
    name = re.sub(r'\s*\([a-z]+\)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[^a-z0-9]', '', name)
    return name


def get_race_urls(date_str):
    """Get all race result URLs for a date from Racing Post."""
    urls = []
    url = f'https://www.racingpost.com/results/{date_str}'
    
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200:
            return urls
        doc = html.fromstring(resp.content)
        links = doc.xpath('//a[contains(@href, "/results/")]/@href')
        
        for href in links:
            if '/results/' in href and href.count('/') >= 5:
                full = f'https://www.racingpost.com{href}' if not href.startswith('http') else href
                if full not in urls:
                    urls.append(full)
    except Exception as e:
        print(f'[ERROR] get_race_urls: {e}')
    
    return urls


def parse_race(url):
    """Parse a single race result page, returning list of runner dicts."""
    runners = []
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200:
            return runners
        doc = html.fromstring(resp.content)
        
        # Extract course from URL
        parts = url.rstrip('/').split('/')
        course = parts[4].replace('-', ' ').title() if len(parts) > 4 else ''
        
        # Off time
        off_el = doc.xpath('//span[contains(@class, "rp-raceTimeCourseName__time")]')
        off_time = off_el[0].text_content().strip() if off_el else ''
        
        rows = doc.xpath('//tr[contains(@class, "rp-horseTable__mainRow")]')
        
        for row in rows:
            # Position
            pos = 0
            pos_el = row.xpath('.//span[contains(@class, "rp-horseTable__pos__number")]')
            if pos_el:
                pos_text = pos_el[0].text_content().strip()
                m = re.match(r'\d+', pos_text)
                if m:
                    pos = int(m.group())
            
            # Horse name
            horse = ''
            horse_el = row.xpath('.//a[contains(@class, "rp-horseTable__horse__name")]')
            if horse_el:
                horse = horse_el[0].text_content().strip()
            
            # SP odds
            dec = 0
            odds_el = row.xpath('.//span[contains(@class, "rp-horseTable__horse__price")]')
            if odds_el:
                odds_text = odds_el[0].text_content().strip().rstrip('FJCfjc').strip()
                try:
                    if odds_text.lower() in ('evs', 'evens', 'ev'):
                        dec = 2.0
                    elif '/' in odds_text:
                        p = odds_text.split('/')
                        if len(p) == 2:
                            dec = round(float(p[0]) / float(p[1]) + 1, 2)
                except:
                    pass
            
            if horse:
                runners.append({
                    'horse': horse,
                    'pos': pos,
                    'dec': dec,
                    'course': course,
                    'off': off_time,
                })
    
    except Exception as e:
        print(f'[ERROR] parse_race {url}: {e}')
    
    return runners


def scrape_all_results(date_str):
    """Scrape all results for a date, return dict keyed by normalized horse name."""
    results = {}
    
    urls = get_race_urls(date_str)
    if not urls:
        return results
    
    for i, url in enumerate(urls):
        runners = parse_race(url)
        for r in runners:
            norm = normalize_name(r['horse'])
            if norm:
                results[norm] = {
                    'horse': r['horse'],
                    'pos': r['pos'],
                    'dec': r['dec'],
                    'course': r['course'],
                    'off': r['off'],
                }
        time.sleep(0.3)  # Be polite
    
    return results


# ─── API ROUTES ───

@app.route('/')
def index():
    return jsonify({
        'service': 'Q-Tips Results API',
        'usage': 'GET /api/results/<YYYY-MM-DD>',
        'example': '/api/results/2026-03-23',
    })


@app.route('/api/results/<date_str>')
def get_results(date_str):
    """Main endpoint — returns all horse results for a date."""
    # Validate date format
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    # Check cache
    if date_str in CACHE:
        entry = CACHE[date_str]
        if time.time() - entry['timestamp'] < CACHE_TTL:
            return jsonify({
                'date': date_str,
                'count': len(entry['data']),
                'cached': True,
                'results': entry['data'],
            })
    
    # Scrape
    results = scrape_all_results(date_str)
    
    # Cache
    CACHE[date_str] = {'timestamp': time.time(), 'data': results}
    
    return jsonify({
        'date': date_str,
        'count': len(results),
        'cached': False,
        'results': results,
    })


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.utcnow().isoformat()})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5123))
    print(f'Starting Results API on port {port}')
    app.run(host='0.0.0.0', port=port, debug=True)
