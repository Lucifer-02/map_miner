import json
import os
from pathlib import Path
import subprocess
import time

base = Path('/data/IMPORTANT/map_miner/experiments/playwright_comparison_20261013')
while not (base/'r1_cache_on_pw162/summary.json').exists():
    time.sleep(2)
plan = [('r1_cache_on_pw163','1.63.0','on'),('r1_cache_off_pw162','1.62.0','off'),('r1_cache_off_pw163','1.63.0','off'),('r2_cache_on_pw163','1.63.0','on'),('r2_cache_on_pw162','1.62.0','on'),('r2_cache_off_pw163','1.63.0','off'),('r2_cache_off_pw162','1.62.0','off')]
env = dict(os.environ, PYTHONHASHSEED='0', PYTHONPATH='/data/IMPORTANT/map_miner/src')
deps = ['polars','geopy','aiohttp','beautifulsoup4','pydub','speechrecognition','vosk']
for run_id, version, cache in plan:
    command = ['uv','run','--no-project','--with',f'playwright=={version}']
    for dep in deps:
        command.extend(['--with',dep])
    command.extend(['python','/tmp/map_miner_pw_comparison/run.py','--id',run_id,'--cache',cache])
    print('START '+run_id, flush=True)
    with (base/f'{run_id}_process.log').open('w') as output:
        p = subprocess.run(command,env=env,stdout=output,stderr=subprocess.STDOUT)
    print('END '+run_id+' exit='+str(p.returncode),flush=True)
    path = base/run_id/'summary.json'
    if path.exists():
        s = json.loads(path.read_text())
        print(json.dumps({'run':run_id,'total':s.get('total_count'),'elapsed':s['elapsed_seconds'],'queries':s['queries']}),flush=True)
    else:
        print('MISSING SUMMARY '+run_id,flush=True)
