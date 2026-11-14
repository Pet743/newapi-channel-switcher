"""Run inside the service container; reads existing auth without printing it."""
import base64
import json
import os
import urllib.error
import urllib.request

authorization = base64.b64encode((os.getenv('ADMIN_USER', 'root') + ':' + os.environ['ADMIN_PASSWORD']).encode()).decode()
def request(path, body=None):
    req = urllib.request.Request('http://127.0.0.1:19041' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Basic '+authorization, 'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response: return response.read()

groups = json.loads(request('/api/groups'))['groups']
assert groups
target = next(g for g in groups if g['id'] == 3)
assert (target['error_window_seconds'], target['error_threshold']) == (5, 3)
assert target['enabled'] == 1
assert '503 切换规则' in request('/').decode()
updated = json.loads(request('/api/groups/3/error-rule', {'error_window_seconds':5,'error_threshold':3}))
assert updated['enabled'] == target['enabled']
assert updated['current_channel_id'] == target['current_channel_id']
print(json.dumps({'group_id':3,'name':updated['name'],'error_window_seconds':5,'error_threshold':3,
                  'enabled':updated['enabled'],'current_channel_id':updated['current_channel_id'],
                  'html_rule_form':True,'save_rule_status':200}, ensure_ascii=False, separators=(',',':')))
try:
    request('/api/groups/3/error-rule', {'error_window_seconds':5,'error_threshold':0})
except urllib.error.HTTPError as exc:
    assert exc.code == 400
    print('invalid_threshold_status=400')
else:
    raise AssertionError('invalid threshold accepted')
