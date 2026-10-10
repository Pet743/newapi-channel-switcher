from __future__ import annotations

import json
import threading
import time
from typing import Iterable

from .core import CandidateResult, Channel

try:
    import psycopg
except ImportError:  # pragma: no cover - local tests can use MemoryRepository
    psycopg = None


CHANNEL_COLUMNS = "id,name,status,\"group\",priority,weight,base_url,key,models,setting"


class PostgresChannelRepository:
    def __init__(self, dsn: str):
        if psycopg is None:
            raise RuntimeError("未安装 psycopg，请先安装 requirements.txt")
        self.dsn = dsn
        self.lock = threading.RLock()

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def get_channels(self, ids: Iterable[int] | None = None) -> list[Channel]:
        with self._connect() as conn:
            if ids is not None:
                id_list = list(ids)
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {CHANNEL_COLUMNS} FROM channels WHERE id = ANY(%s) ORDER BY id", (id_list,))
                    rows = cur.fetchall()
            else:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {CHANNEL_COLUMNS} FROM channels ORDER BY id")
                    rows = cur.fetchall()
        return [Channel(id=r[0], name=r[1] or "", status=int(r[2] or 0), group=r[3] or "", priority=int(r[4] or 0), weight=int(r[5] or 0), base_url=r[6] or "", key=r[7] or "", models=r[8] or "", setting=r[9] or "") for r in rows]

    def set_active(self, channel_ids: list[int], active_id: int) -> None:
        if not channel_ids:
            raise ValueError("分组没有渠道")
        with self.lock, self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM channels WHERE id = ANY(%s) FOR UPDATE", (channel_ids,))
                found = {int(row[0]) for row in cur.fetchall()}
                missing = set(channel_ids) - found
                if missing:
                    raise ValueError(f"渠道不存在: {sorted(missing)}")
                if active_id not in found:
                    raise ValueError("目标渠道不在分组")
                cur.execute("UPDATE channels SET status=2 WHERE id = ANY(%s)", (channel_ids,))
                cur.execute("UPDATE channels SET status=1 WHERE id=%s", (active_id,))
                cur.execute("UPDATE abilities SET enabled=(channel_id=%s) WHERE channel_id = ANY(%s)", (active_id, channel_ids))
            conn.commit()

    def restore_statuses(self, statuses):
        with self.lock, self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM channels WHERE id = ANY(%s) FOR UPDATE", (list(statuses),))
                if {r[0] for r in cur.fetchall()} != set(statuses):
                    raise ValueError("快照中的渠道已被删除，请核对后恢复")
                for cid, status in statuses.items():
                    cur.execute("UPDATE channels SET status=%s WHERE id=%s", (status, cid))
                    cur.execute("UPDATE abilities SET enabled=%s WHERE channel_id=%s", (status == 1, cid))
            conn.commit()


class UpstreamTester:
    def __init__(self, model: str = "gpt-6.1-sol", timeout: float = 20):
        self.model = model
        self.timeout = timeout

    def __call__(self, channel: Channel) -> CandidateResult:
        import urllib.error
        import urllib.request

        started = time.monotonic()
        if not channel.base_url or not channel.key:
            return CandidateResult(channel.id, None, int((time.monotonic() - started) * 1000), False, "渠道缺少 base_url 或 key")
        setting = {}
        try:
            setting = json.loads(channel.setting or "{}")
        except json.JSONDecodeError:
            pass
        proxy = setting.get("proxy")
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        payload = json.dumps({"model": self.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}).encode()
        req = urllib.request.Request(channel.base_url.rstrip("/") + "/v1/chat/completions", data=payload, method="POST", headers={"Authorization": f"Bearer {channel.key}", "Content-Type": "application/json", "User-Agent": "newapi-channel-switcher/0.1"})
        status = None
        detail = ""
        valid = False
        try:
            with opener.open(req, timeout=self.timeout) as response:
                status = response.status
                detail = response.read(65536).decode("utf-8", "replace")
                try:
                    data = json.loads(detail)
                    valid = isinstance(data, dict) and not data.get("error") and bool(data.get("choices") or data.get("output"))
                except ValueError:
                    valid = False
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                detail = exc.read(500).decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
        except Exception as exc:
            detail = str(exc)
        elapsed = int((time.monotonic() - started) * 1000)
        detail = detail.replace(channel.key, "[redacted]")
        if proxy:
            detail = detail.replace(proxy, "[proxy]")
        return CandidateResult(channel.id, status, elapsed, bool(status and 200 <= status < 300 and valid), detail[:500])
