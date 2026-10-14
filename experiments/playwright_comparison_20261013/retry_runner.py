import json
import os
from pathlib import Path
import subprocess
import time
base = Path('/data/IMPORTANT/map_miner/experiments/playwright_comparison_20261013')
while not (base/'r2_cache_off_pw162/summary.json').exists():
    time.sleep(2)
env = dict(os.environ,PYTHONHASHSEED='0',PYTHONPATH='/data/IMPORTANT/map_miner/src')
for run_id,version in [('r1_retry_cache_on_pw162','1.62.0'),('r1_retry_cache_on_pw163','1.63.0')]:
    cmd=['uv','run','--no-project','--with',f'playwright=={version}']
    for dep in ['polars','geopy','aiohttp','beautifulsoup4','pydub','speechrecognition','vosk']:
        cmd.extend(['--with',dep])
    cmd.extend(['python','/tmp/map_miner_pw_comparison/run.py','--id',run_id,'--cache','on'])
    print('START '+run_id,flush=True)
    with (base/f'{run_id}_process.log').open('w') as log:
        p=subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
    print('END '+run_id+' exit='+str(p.returncode),flush=True)
    path=base/run_id/'summary.json'
    if path.exists():
        s=json.loads(path.read_text())
        print(json.dumps({'run':run_id,'total':s.get('total_count'),'elapsed':s['elapsed_seconds'],'queries':s['queries']}),flush=True)
