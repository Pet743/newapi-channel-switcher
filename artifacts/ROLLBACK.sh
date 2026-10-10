#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1:-}" = "--deployment" ]; then
    cd "$SCRIPT_DIR/.."
    python3 - <<'PY'
import base64,json,urllib.request
from pathlib import Path
env=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
auth=base64.b64encode((env['ADMIN_USER']+':'+env['ADMIN_PASSWORD']).encode()).decode()
base='http://127.0.0.1:'+env.get('BIND_PORT','19041')+'/api/'
def request(path,body=None):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
for group in request('groups')['groups']:
    if group['channels']:
        request('groups/'+str(group['id'])+'/restore',{})
        print('restored_group='+str(group['id']))
PY
    docker compose stop
    echo 'deployment=stopped; channels=restored'
else
    TARGET=${1:?用法: ROLLBACK.sh <待恢复的代码副本> 或 --deployment}
    cp "$SCRIPT_DIR/ORIGINAL_FILE.core.py" "$TARGET"
    echo "restored=$TARGET"
fi
