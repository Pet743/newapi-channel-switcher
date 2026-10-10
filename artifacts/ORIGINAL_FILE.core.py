from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol


ERROR_RE = re.compile(
    r"record error log:.*?channelId=(?P<channel_id>\d+).*?content=status_code=(?P<status>\d+),\s*(?P<message>.*?)(?:\s*\(request id:|\s*$)"
)


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


class LogParser:
    """解析 newapi 的 record error log 行，只接受 503 事件。"""

    @staticmethod
    def parse(line: str) -> tuple[int, str] | None:
        match = ERROR_RE.search(line)
        if not match or int(match.group("status")) != 503:
            return None
        return int(match.group("channel_id")), match.group("message").strip()


class LogWatcher:
    """跟踪日志目录中最新文件，避免重启时重复处理历史日志。"""

    def __init__(self, directory: str, on_503: Callable[[int, str], None], interval: float = 1.0):
        self.directory = Path(directory)
        self.on_503 = on_503
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offsets: dict[tuple[int, str], int] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="newapi-log-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except OSError:
                pass
            self._stop.wait(self.interval)

    def poll_once(self) -> None:
        files = sorted(self.directory.glob("oneapi-*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
        # 首次启动仅从最新文件的末尾开始，避免把历史 503 当作新事件。
        for path in files[-3:]:
            stat = path.stat()
            key = (stat.st_ino, str(path))
            offset = self._offsets.get(key)
            if offset is None:
                offset = stat.st_size
            if stat.st_size < offset:
                offset = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    parsed = LogParser.parse(line)
                    if parsed:
                        self.on_503(*parsed)
                self._offsets[key] = fh.tell()


class SwitchEngine:
    """以分组配置为边界进行单活切换。"""

    def __init__(
        self,
        repository: ChannelRepository,
        config_store,
        tester: Callable[[Channel], CandidateResult],
        event_sink: Callable[[dict], None] | None = None,
    ):
        self.repository = repository
        self.config_store = config_store
        self.tester = tester
        self.event_sink = event_sink or (lambda _event: None)
        self._lock = threading.RLock()

    def handle_503(self, channel_id: int, message: str) -> list[dict]:
        events: list[dict] = []
        with self._lock:
            for group in self.config_store.groups_for_channel(channel_id):
                candidates = self.config_store.candidates(group["id"])
                candidates = sorted(candidates, key=lambda x: (-int(x["priority"]), int(x["channel_id"])))
                for candidate in candidates:
                    candidate_id = int(candidate["channel_id"])
                    if candidate_id == channel_id:
                        continue
                    channel = next((c for c in self.repository.get_channels([candidate_id]) if c.id == candidate_id), None)
                    if not channel:
                        continue
                    result = self.tester(channel)
                    event = {
                        "type": "test",
                        "group_id": group["id"],
                        "group_name": group["name"],
                        "failed_channel_id": channel_id,
                        "candidate_channel_id": candidate_id,
                        "status_code": result.status_code,
                        "success": result.success,
                        "detail": result.detail,
                        "elapsed_ms": result.elapsed_ms,
                        "message": message,
                    }
                    events.append(event)
                    self.event_sink(event)
                    if result.success:
                        managed_ids = [int(item["channel_id"]) for item in candidates]
                        self.repository.set_active(managed_ids, candidate_id)
                        self.config_store.set_current(group["id"], candidate_id)
                        switched = {**event, "type": "switched", "active_channel_id": candidate_id}
                        events.append(switched)
                        self.event_sink(switched)
                        break
        return events

    def test_and_activate(self, group_id: int, candidate_id: int) -> list[dict]:
        with self._lock:
            rows = self.config_store.candidates(group_id)
            if candidate_id not in {int(row["channel_id"]) for row in rows}:
                raise ValueError("渠道不属于该分组")
            channel = next((c for c in self.repository.get_channels([candidate_id]) if c.id == candidate_id), None)
            if not channel:
                raise ValueError("渠道不存在")
            result = self.tester(channel)
            event = {"type": "manual_test", "group_id": group_id, "candidate_channel_id": candidate_id, "status_code": result.status_code, "success": result.success, "detail": result.detail, "elapsed_ms": result.elapsed_ms}
            self.event_sink(event)
            if result.success:
                self.repository.set_active([int(row["channel_id"]) for row in rows], candidate_id)
                self.config_store.set_current(group_id, candidate_id)
            return [event]


class MemoryRepository:
    """测试用仓储，行为与真实仓储相同。"""

    def __init__(self, channels: list[Channel]):
        self.channels = {c.id: c for c in channels}

    def get_channels(self, ids=None):
        return [self.channels[i] for i in (ids or self.channels) if i in self.channels]

    def set_active(self, channel_ids, active_id):
        for i in channel_ids:
            c = self.channels[i]
            self.channels[i] = Channel(**{**c.__dict__, "status": 1 if i == active_id else 2})
