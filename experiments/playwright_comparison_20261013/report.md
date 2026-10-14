# Playwright / Chromium comparison — 2026-10-13

Google Maps live experiment, Hanoi (21.018785, 105.830415). Eight valid runs; two earlier pilot scrapes were excluded from aggregate counts because the harness CSV exporter rejected nested Polars columns. CSV exporter was corrected without changing scraping inputs or product source. Ten scraper launches total.

## Environment and method

- Playwright 1.62.0: bundled Chromium 151.0.7922.34, revision 1234.
- Playwright 1.63.0: bundled Chromium 153.0.8010.12, revision 1243.
- Headless mode uses bundled chrome-headless-shell (same corresponding version/revision). Headed executable_path is reported separately in each setup.json; headless_shell_executable identifies the bundled shell path.
- Same source SHA256 across all valid runs, confirmed against source after completion. Product source and pinned dependencies were not edited.
- Queries: gym, cafe, hospital, atm; order fixed using PYTHONHASHSEED=0. random.seed(20261013) before each run; concurrent scheduling can still change the order of random draws.
- zoom18, max_places50 per query, lang en, n_semaphore8 (4 actual concurrent queries), SPA true, query_timeout300s, preview_timeout10000ms, radius25km; remaining scraper defaults. No proxy.
- Each run uses a separate fresh static cache. Cache on adds one fresh Chromium disk-cache directory shared by concurrent contexts; cache off omits disk-cache launch flags.
- Runs are sequential; Playwright versions were never scraped simultaneously. Separate uv environments selected each exact Playwright version.
- Actual valid order: r1_cache_off_pw162, r1_cache_off_pw163, r2_cache_on_pw163, r2_cache_on_pw162, r2_cache_off_pw163, r2_cache_off_pw162, r1_retry_cache_on_pw162, r1_retry_cache_on_pw163. The first paired cache-on runs were repeated at the end because of the CSV artifact failure.

## Per-run results

| Run | PW | Cache | Gym | Cafe | Hospital | ATM | Total | Global unique links | Seconds | Navigation timeout | Preview timeout/error |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1_cache_off_pw162 | 1.62.0 | off | 50 | 50 | 42 | 50 | 192 | 192 | 126.88 | 0 | 0 |
| r1_cache_off_pw163 | 1.63.0 | off | 50 | 50 | 42 | 50 | 192 | 192 | 126.93 | 0 | 0 |
| r2_cache_on_pw163 | 1.63.0 | on | 50 | 0 | 0 | 0 | 50 | 50 | 100.26 | 3 | 0 |
| r2_cache_on_pw162 | 1.62.0 | on | 50 | 50 | 43 | 50 | 193 | 193 | 111.31 | 0 | 1 |
| r2_cache_off_pw163 | 1.63.0 | off | 50 | 50 | 43 | 50 | 193 | 193 | 141.87 | 0 | 1 |
| r2_cache_off_pw162 | 1.62.0 | off | 50 | 50 | 40 | 50 | 190 | 190 | 119.28 | 0 | 0 |
| r1_retry_cache_on_pw162 | 1.62.0 | on | 50 | 50 | 42 | 50 | 192 | 192 | 147.34 | 0 | 2 |
| r1_retry_cache_on_pw163 | 1.63.0 | on | 50 | 0 | 0 | 0 | 50 | 50 | 96.96 | 3 | 0 |

## Aggregate

| PW | Cache | Runs | Mean rows | Mean seconds | Navigation timeout total | Preview timeout/error total |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1.62.0 | on | 2 | 192.5 | 129.33 | 0 | 3 |
| 1.63.0 | on | 2 | 50 | 98.61 | 6 | 0 |
| 1.62.0 | off | 2 | 191 | 123.08 | 0 | 0 |
| 1.63.0 | off | 2 | 192.5 | 134.4 | 0 | 1 |

## Interpretation and limits

The failure observed with Playwright 1.63.0 and its bundled Chromium is initial navigation: cafe, hospital and atm time out in Page.goto after 30 seconds while waiting for domcontentloaded. These queries return zero rows. Gym starts first and continues scraping. Turning Chromium disk cache off allows all four queries to navigate in this experiment. The evidence supports a version/configuration interaction involving the shared Chromium disk-cache directory and simultaneous BrowserContexts. It does not isolate Playwright from its bundled Chromium or prove an upstream root cause.

Each version was tested with its corresponding bundled Chromium only. No crossover browser matrix was run. max_places=50 caps gym/cafe/atm, so these runs cannot establish uncapped recall or absolute throughput. Hospital counts vary because Google Maps ranking/results vary and the 25 km filter drops distant places. Query end markers and out-of-range scroll stops are recorded in metrics.csv and per-run summaries.

The two excluded pilots independently logged 200 rows for 1.62/cache-on and 50 rows for 1.63/cache-on; they are observations from logs, not validated exported result datasets. The pilot export failure is a harness issue.

No CAPTCHA-related log messages or null names were observed in valid runs. captcha_messages counts matching log messages, not distinct CAPTCHA challenges. Preview timeout/error counts combine response timeout and Playwright response errors; DOM fallback rescue counts are separate. A shorter failed run is not better performance because three queries return zero. Counts are extracted output rows; a non-null name does not prove that a preview belongs to the clicked place. No place-association ground truth audit was performed. This experiment does not establish complete or always-correct Google Maps data.

## Artifacts and reproduction

- [harness.py](harness.py): measurement wrapper and run configuration; no library source edits.
- [aggregate.json](aggregate.json): full valid summaries and grouped metrics.
- [metrics.csv](metrics.csv): counts, per-query times, timeouts and stop reasons.
- Each run folder: setup.json, summary.json, results.json, results.csv and debug.log. The two pilot folders contain setup/summary/log only and a failed CSV export.
- Debug log attributes scraper events to the concurrent query through a context variable. The wrapper does not change extraction, waits, browser launch flags or collection limits.
- Browser/cache binaries and CAPTCHA diagnostics are kept outside the repository.

Example command from repository root (requires installed bundled browsers):

```bash
PYTHONHASHSEED=0 PYTHONPATH="$PWD/src" uv run --no-project \
  --with playwright==1.62.0 --with polars --with geopy --with aiohttp \
  --with beautifulsoup4 --with pydub --with speechrecognition --with vosk \
  python experiments/playwright_comparison_20261013/harness.py \
  --id reproduction_pw162_cacheoff --cache off
```

The harness writes artifacts to this experiment directory and caches to /tmp/map_miner_pw_comparison. Use a new run ID to preserve earlier artifacts. No Python unit suite was run because product code was unchanged. Markdown links and git diff whitespace were checked.
