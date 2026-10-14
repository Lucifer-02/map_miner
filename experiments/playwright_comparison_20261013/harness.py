import argparse
import asyncio
import contextvars
import csv
import datetime
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
from pathlib import Path
import random
import time

from geopy.point import Point
from playwright.async_api import async_playwright
import map_miner.scraper as scraper

parser = argparse.ArgumentParser()
parser.add_argument('--id', required=True)
parser.add_argument('--cache', choices=['on', 'off'], required=True)
args = parser.parse_args()
output = Path('/data/IMPORTANT/map_miner/experiments/playwright_comparison_20261013') / args.id
output.mkdir(parents=True, exist_ok=True)
temporary = Path('/tmp/map_miner_pw_comparison') / args.id
temporary.mkdir(parents=True, exist_ok=True)
os.chdir(temporary)
query_context = contextvars.ContextVar('query', default='global')
metrics = {}
events = {}

class Observer(logging.Filter):
    def filter(self, record):
        record.query = query_context.get()
        events.setdefault(record.query, []).append(record.getMessage())
        return True

handler = logging.FileHandler(output / 'debug.log')
handler.addFilter(Observer())
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] [query=%(query)s] %(message)s'))
logging.basicConfig(level=logging.DEBUG, handlers=[handler])
# Keep third-party HTTP diagnostics out of the artifact while retaining scraper DEBUG.
for name in ['aiohttp', 'urllib3', 'httpx', 'httpcore']:
    logging.getLogger(name).setLevel(logging.WARNING)

original = scraper._scrape_query_spa
async def observed(*a, **kw):
    q = kw['query']
    token = query_context.set(q)
    start = time.monotonic()
    try:
        return await original(*a, **kw)
    finally:
        metrics[q] = {'elapsed_seconds': round(time.monotonic() - start, 3)}
        query_context.reset(token)
scraper._scrape_query_spa = observed

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=scraper.LAUNCH_ARGS)
        version = {'playwright': importlib.metadata.version('playwright'), 'chromium': browser.version, 'executable_path': p.chromium.executable_path}
        package = Path(importlib.util.find_spec('playwright').submodule_search_locations[0])
        bundled = json.loads((package/'driver/package/browsers.json').read_text())
        shell = next(b for b in bundled['browsers'] if b['name'] == 'chromium-headless-shell')
        version['headless_shell_revision'] = shell['revision']
        version['headless_shell_executable'] = str(Path.home()/'.cache/ms-playwright'/f"chromium_headless_shell-{shell['revision']}"/'chrome-headless-shell-linux64/chrome-headless-shell')
        await browser.close()
    random.seed(20261013)
    queries = {'cafe', 'atm', 'hospital', 'gym'}
    summary = {'run_id': args.id, 'cache': args.cache, 'versions': version, 'started_utc': datetime.datetime.now(datetime.UTC).isoformat(), 'query_order': list(queries), 'pythonhashseed': os.environ.get('PYTHONHASHSEED'), 'source_sha256': {str(f.relative_to(Path('/data/IMPORTANT/map_miner'))): hashlib.sha256(f.read_bytes()).hexdigest() for f in Path('/data/IMPORTANT/map_miner/src').rglob('*.py')}, 'parameters': {'max_places': 50, 'lang': 'en', 'coordinates': [21.018785,105.830415], 'zoom':18,'n_semaphore':8,'use_spa':True,'query_timeout':300,'preview_timeout':10000,'range_limit':25000,'stagger_delay':[1.5,3.5],'static_cache':'fresh per run','disk_cache':'fresh per run' if args.cache == 'on' else None}}
    (output / 'setup.json').write_text(json.dumps(summary, indent=2))
    start = time.monotonic()
    try:
        frame = await scraper.scrape_google_maps(queries=queries, geo_coordinates=Point(21.018785,105.830415), zoom=18, max_places=50, lang='en', n_semaphore=8, cache_dir=temporary/'disk_cache' if args.cache == 'on' else None, static_cache_dir=temporary/'static_cache')
        rows = frame.to_dicts()
        (output/'results.json').write_text(json.dumps(rows,ensure_ascii=False,default=str))
        try:
            with (output/'results.csv').open('w',newline='') as csv_output:
                writer = csv.DictWriter(csv_output,fieldnames=frame.columns)
                writer.writeheader()
                writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in rows)
        except Exception as export_error:
            summary['csv_export_error'] = str(export_error)
        for q in queries:
            selected = [r for r in rows if r.get('query') == q]
            messages = events.get(q, [])
            stops = [m for m in messages if any(s in m for s in ['reached timeout limit', 'approaching timeout limit', 'Stopping SPA scroll', 'Reached end of results', 'Error during scrape_query_spa', 'No results found', 'Could not find results feed', 'Exceeded max CAPTCHA'])]
            metrics.setdefault(q, {}).update({'count':len(selected), 'unique_links':len({r.get('link','').split('?')[0] for r in selected if r.get('link')}), 'null_name':sum(r.get('name') is None for r in selected), 'preview_timeout_or_error':sum('Error or timeout waiting for SPA preview' in m for m in messages), 'navigation_timeout':sum(('navigat' in m.lower() or 'Page.goto' in m) and 'timeout' in m.lower() for m in messages), 'captcha_messages':sum('captcha' in m.lower() for m in messages), 'stop_reasons':stops or (['max_places'] if len(selected)>=50 else ['returned without explicit stop log']), 'fallback_rescued':sum('[Fallback Rescue]' in m for m in messages)})
        summary['total_count'] = len(rows)
        summary['unique_links'] = len({r.get('link','').split('?')[0] for r in rows if r.get('link')})
    except Exception as e:
        summary['error'] = str(e)
        logging.exception('Experiment failed')
    finally:
        summary['elapsed_seconds'] = round(time.monotonic()-start,3)
        summary['queries'] = metrics
        summary['global_warning_events'] = [m for m in events.get('global',[]) if any(s in m.lower() for s in ['timeout','captcha','error'])]
        (output/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
        print(json.dumps(summary,ensure_ascii=False),flush=True)

asyncio.run(main())
