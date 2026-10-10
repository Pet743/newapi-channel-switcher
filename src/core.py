from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

ERROR_RE = re.compile(r"record error log:.*?channelId=(\d+).*?content=status_code=503(?:\D|$)(.*)")
CHANNEL_ERROR_RE = re.compile(r"channel error \(channel #(\d+), status code: 503\):\s*(.*)")

@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    status: int
    group: str
    priority: int
    weight: int
    base_url: str = ""
    key: str = ""
    models: str = ""
    setting: str = ""

    def public(self):
        return {k: getattr(self, k) for k in ("id", "name", "status", "group", "priority", "weight", "base_url", "models")}

@dataclass(frozen=True)
class CandidateResult:
    channel_id: int
    status_code: int | None
    elapsed_ms: int
    success: bool
    detail: str

class ChannelRepository(Protocol):
    def get_channels(self, ids: Iterable[int] | None = None) -> list[Channel]: ...
    def set_active(self, channel_ids: list[int], active_id: int) -> None: ...
    def restore_statuses(self, statuses: dict[int, int]) -> None: ...

class LogParser:
    @staticmethod
    def parse(line: str) -> tuple[int, str] | None:
        match = ERROR_RE.search(line) or CHANNEL_ERROR_RE.search(line)
        return (int(match[1]), match[2].strip()) if match else None

class LogWatcher:
    """首次跳过历史；新文件从头读；只处理已完整写入的行。"""
    def __init__(self, directory, on_503, interval=1.0):
        self.directory = Path(directory)
        self.on_503, self.interval = on_503, interval
        self._stop = threading.Event()
        self._offsets = {}
        self._initialized = False
        self._thread = None

    def start(self):
        self.poll_once()
        self._thread = threading.Thread(target=self._run, name="log-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.wait(self.interval):
            try: self.poll_once()
            except Exception: logging.exception("日志监听失败，下一轮重试")

    def poll_once(self):
        paths = list(self.directory.glob("oneapi-*.log"))
        if not self._initialized:
            for path in paths:
                stat = path.stat()
                self._offsets[(stat.st_ino, str(path))] = stat.st_size
            self._initialized = True
            return
        live = set()
        for path in paths:
            stat = path.stat()
            key = (stat.st_ino, str(path)); live.add(key)
            offset = self._offsets.get(key, 0)
            if stat.st_size < offset: offset = 0
            with path.open("rb") as fh:
                fh.seek(offset)
                for _ in range(5000):
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"): break
                    # 故障切换失败仍消费本行，由定时探测重试，避免不停重放。
                    self._offsets[key] = fh.tell()
                    parsed = LogParser.parse(line.decode("utf-8", "replace"))
                    if parsed:
                        try: self.on_503(*parsed)
                        except Exception: logging.exception("503 事件处理失败")
                self._offsets.setdefault(key, offset)
        self._offsets = {k: v for k, v in self._offsets.items() if k in live}

class SwitchEngine:
    def __init__(self, repository, config_store, tester, event_sink=None, cooldown=60):
        self.repository, self.config_store, self.tester = repository, config_store, tester
        self.event_sink = event_sink or (lambda event: None)
        self.cooldown = cooldown
        self.lock = threading.RLock()
        self._lock = self.lock
        self._failed_until = {}

    def _test(self, group, channel, reason):
        result = self.tester(channel)
        self.config_store.record_test(result)
        if not result.success: self._failed_until[channel.id] = time.time() + self.cooldown
        else: self._failed_until.pop(channel.id, None)
        event = {"type": "test", "group_id": group["id"], "group_name": group["name"],
                 "candidate_channel_id": channel.id, "status_code": result.status_code,
                 "success": result.success, "detail": result.detail, "elapsed_ms": result.elapsed_ms,
                 "reason": reason}
        self.event_sink(event)
        return result

    def _choose(self, group, reason, excluded=None, force=False):
        rows = self.config_store.candidates(group["id"])
        ids = [int(r["channel_id"]) for r in rows]
        channels = {c.id: c for c in self.repository.get_channels(ids)} if ids else {}
        current = group["current_channel_id"]
        for row in rows:
            cid = int(row["channel_id"])
            if cid == excluded or cid not in channels: continue
            if not force and self._failed_until.get(cid, 0) > time.time(): continue
            # 按优先级探测；到达当前渠道时保留它，避免反复产生测试费用。
            if cid == current and excluded is None and not force and cid not in self._failed_until:
                self.repository.set_active(ids, cid)
                return {"success": True, "active_channel_id": cid, "changed": False}
            result = self._test(group, channels[cid], reason)
            if not result.success: continue
            self.repository.set_active(ids, cid)
            self.config_store.set_current(group["id"], cid)
            self.config_store.update_group(group["id"], enabled=True)
            event = {"type": "switched", "group_id": group["id"], "group_name": group["name"],
                     "failed_channel_id": excluded, "previous_channel_id": current,
                     "active_channel_id": cid, "reason": reason, "success": True}
            self.event_sink(event)
            return {"success": True, "active_channel_id": cid, "changed": cid != current}
        # 全失败时保留最近成功渠道；后台提示并在下次周期重试。
        if current in channels:
            self.repository.set_active(ids, current)
        self.event_sink({"type": "unavailable", "group_id": group["id"], "group_name": group["name"],
                         "reason": reason, "success": False, "detail": "候选渠道全部测试失败，保留当前状态，等待下一轮探测"})
        return {"success": False, "active_channel_id": current, "changed": False}

    def handle_503(self, channel_id, message):
        results = []
        with self.lock:
            for group in self.config_store.groups_for_channel(channel_id):
                # 忽略旧请求/备用渠道迟到的错误，不覆盖正在使用的成功渠道。
                if group["current_channel_id"] != channel_id: continue
                self._failed_until[channel_id] = time.time() + self.cooldown
                self.event_sink({"type": "503", "group_id": group["id"], "group_name": group["name"],
                                 "failed_channel_id": channel_id, "detail": message})
                results.append(self._choose(group, "检测到503", excluded=channel_id))
        return results

    def activate_best(self, group_id):
        with self.lock:
            group = self.config_store.get_group(group_id)
            if not group: raise ValueError("分组不存在")
            if not self.config_store.candidates(group_id): raise ValueError("请先保存渠道配置")
            self.config_store.update_group(group_id, enabled=True)
            return self._choose(group, "启用配置", force=True)

    def reconcile(self):
        with self.lock:
            for group in self.config_store.groups():
                if group["enabled"]:
                    try: self._choose(group, "高优先级恢复探测")
                    except Exception:
                        logging.exception("分组巡检失败")
                        self.event_sink({"type": "error", "group_id": group["id"], "group_name": group["name"], "detail": "巡检失败，请检查数据库和渠道配置"})

    def restore(self, group_id):
        with self.lock:
            snapshot = self.config_store.snapshot(group_id)
            if not snapshot: raise ValueError("该分组没有接管快照")
            self.repository.restore_statuses(snapshot)
            self.config_store.update_group(group_id, enabled=False)
            self.event_sink({"type": "restored", "group_id": group_id, "detail": "恢复接管前的渠道状态并暂停自动切换"})

class MemoryRepository:
    def __init__(self, channels): self.channels = {c.id: c for c in channels}
    def get_channels(self, ids=None):
        return [self.channels[i] for i in (self.channels if ids is None else ids) if i in self.channels]
    def restore_statuses(self, statuses):
        for cid, status in statuses.items():
            self.channels[cid] = Channel(**{**self.channels[cid].__dict__, "status": status})
    def set_active(self, channel_ids, active_id):
        self.restore_statuses({cid: 1 if cid == active_id else 2 for cid in channel_ids})
