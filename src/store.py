import json
import sqlite3
import threading
import time
from pathlib import Path

class ConfigStore:
    def __init__(self, path="data/config.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS groups_config (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 0,
                current_channel_id INTEGER, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS group_channels (group_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(group_id, channel_id));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tests (channel_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (group_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, status INTEGER NOT NULL,
                PRIMARY KEY(group_id,channel_id));
        ''')
        cols = {r[1] for r in self.conn.execute('PRAGMA table_info(groups_config)')}
        if 'newapi_group' not in cols:
            self.conn.execute("ALTER TABLE groups_config ADD COLUMN newapi_group TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    def groups(self):
        with self.lock: return [dict(r) for r in self.conn.execute('SELECT * FROM groups_config ORDER BY id')]
    def get_group(self, gid):
        with self.lock:
            r = self.conn.execute('SELECT * FROM groups_config WHERE id=?', (gid,)).fetchone()
            return dict(r) if r else None
    def create_group(self, name, newapi_group=''):
        if not name.strip() or len(name) > 128: raise ValueError('分组名称需为1～128个字符')
        with self.lock, self.conn:
            if newapi_group and self.conn.execute('SELECT 1 FROM groups_config WHERE newapi_group=?',(newapi_group,)).fetchone():
                raise ValueError('该newapi分组已有管理配置，请编辑已有分组')
            cur = self.conn.execute('INSERT INTO groups_config(name,newapi_group,enabled,created_at) VALUES(?,?,0,?)', (name.strip(),newapi_group,int(time.time())))
            return self.get_group(cur.lastrowid)
    def update_group(self, gid, name=None, enabled=None):
        fields, values = [], []
        if name is not None:
            if not name.strip(): raise ValueError('名称不能为空')
            fields.append('name=?'); values.append(name.strip())
        if enabled is not None: fields.append('enabled=?'); values.append(int(bool(enabled)))
        if fields:
            with self.lock, self.conn:
                self.conn.execute('UPDATE groups_config SET '+','.join(fields)+' WHERE id=?', values+[gid])
        return self.get_group(gid)
    def delete_group(self, gid):
        with self.lock, self.conn:
            for table in ('group_channels','snapshots'): self.conn.execute(f'DELETE FROM {table} WHERE group_id=?', (gid,))
            self.conn.execute('DELETE FROM groups_config WHERE id=?', (gid,))
    def candidates(self, gid):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM group_channels WHERE group_id=? ORDER BY priority DESC, channel_id', (gid,))]
    def replace_candidates(self, gid, rows):
        if not self.get_group(gid): raise ValueError('分组不存在')
        ids = [int(r['channel_id']) for r in rows]
        if len(ids) != len(set(ids)): raise ValueError('渠道ID不能重复')
        for g in self.groups():
            if g['id'] != gid and any(r['channel_id'] in ids for r in self.candidates(g['id'])):
                raise ValueError('一个渠道只能由一个伴生分组管理')
        with self.lock, self.conn:
            self.conn.execute('DELETE FROM group_channels WHERE group_id=?', (gid,))
            self.conn.executemany('INSERT INTO group_channels VALUES(?,?,?)', [(gid,int(r['channel_id']),int(r['priority'])) for r in rows])
    def groups_for_channel(self, cid):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT g.* FROM groups_config g JOIN group_channels c ON c.group_id=g.id WHERE c.channel_id=? AND g.enabled=1',(cid,))]
    def set_current(self, gid, cid):
        with self.lock, self.conn:
            self.conn.execute('UPDATE groups_config SET current_channel_id=? WHERE id=?',(cid,gid))
    def save_snapshot(self, gid, channels):
        with self.lock, self.conn:
            self.conn.executemany('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)',[(gid,c.id,c.status) for c in channels])
    def snapshot(self, gid):
        with self.lock:
            return {r['channel_id']:r['status'] for r in self.conn.execute('SELECT * FROM snapshots WHERE group_id=?',(gid,))}
    def add_event(self, payload):
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO events(created_at,payload) VALUES(?,?)',(int(time.time()),payload))
            self.conn.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-5000 FROM events)')
    def recent_events(self, limit=100):
        with self.lock:
            return [{**dict(r),'payload':json.loads(r['payload'])} for r in self.conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?',(limit,))]
    def record_test(self, result):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR REPLACE INTO tests VALUES(?,?,?)',(result.channel_id,int(time.time()),json.dumps(result.__dict__,ensure_ascii=False)))
    def test_results(self):
        with self.lock:
            return {r['channel_id']:{**json.loads(r['payload']),'created_at':r['created_at']} for r in self.conn.execute('SELECT * FROM tests')}
