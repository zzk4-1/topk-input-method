"""磁盘缓存：同一 (场景, 前缀, k, 批次) 的候选只让 LLM 算一次。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path


class DiskCache:
    def __init__(self, path: "str | Path"):
        self.path = Path(path)
        self._lock = threading.Lock()  # 预取线程与主线程并发写入
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                raise ValueError
        except Exception:
            self._data = {}

    @staticmethod
    def make_key(context: str, draft: str, k: int, batch: int) -> str:
        raw = f"{context}\x00{draft}\x00{k}\x00{batch}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, context: str, draft: str, k: int, batch: int):
        return self._data.get(self.make_key(context, draft, k, batch))

    def put(self, context: str, draft: str, k: int, batch: int, candidates: list) -> None:
        with self._lock:
            self._data[self.make_key(context, draft, k, batch)] = candidates
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                os.replace(tmp, self.path)  # 原子替换：退出时杀掉 daemon 线程也不会损坏缓存
            except OSError:
                pass  # 缓存写失败不影响主流程
