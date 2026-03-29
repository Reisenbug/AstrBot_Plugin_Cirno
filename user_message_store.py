import json
import time
from pathlib import Path

from astrbot.api import logger

MAX_PER_USER = 200


class UserMessageStore:
    def __init__(self, data_dir: str):
        self._dir = Path(data_dir) / "user_messages"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, uid: str) -> Path:
        return self._dir / f"{uid}.json"

    def _load(self, uid: str) -> list[dict]:
        p = self._path(uid)
        if not p.exists():
            return []
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"UserMessageStore: 读取 {uid} 失败: {e}")
            return []

    def _save(self, uid: str, records: list[dict]) -> None:
        try:
            with open(self._path(uid), "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"UserMessageStore: 写入 {uid} 失败: {e}")

    def append(self, uid: str, name: str, msg: str) -> None:
        if not msg.strip():
            return
        records = self._load(uid)
        records.append({"ts": time.time(), "name": name, "msg": msg[:300]})
        if len(records) > MAX_PER_USER:
            records = records[-MAX_PER_USER:]
        self._save(uid, records)

    def get_recent(self, uid: str, limit: int = 30) -> list[dict]:
        records = self._load(uid)
        return records[-limit:]
