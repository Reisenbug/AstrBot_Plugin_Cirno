import json
import time
from datetime import date
from pathlib import Path

from astrbot.api import logger

MAX_PER_USER_PER_DAY = 200


class GroupMessageStore:
    """
    按群、按用户、按日期存储群消息，供每日画像总结使用。
    目录结构：<data_dir>/group_daily/<group_id>/<YYYY-MM-DD>/<user_id>.json
    每条记录：{"ts": float, "name": str, "msg": str, "bot_reply": str|None}
    """

    def __init__(self, data_dir: str):
        self._base = Path(data_dir) / "group_daily"
        self._base.mkdir(parents=True, exist_ok=True)

    def _today(self) -> str:
        return date.today().isoformat()

    def _path(self, group_id: str, day: str, user_id: str) -> Path:
        d = self._base / group_id / day
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{user_id}.json"

    def _load(self, group_id: str, day: str, user_id: str) -> list[dict]:
        p = self._path(group_id, day, user_id)
        if not p.exists():
            return []
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"GroupMessageStore: 读取失败 {p}: {e}")
            return []

    def _save(self, group_id: str, day: str, user_id: str, records: list[dict]) -> None:
        p = self._path(group_id, day, user_id)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"GroupMessageStore: 写入失败 {p}: {e}")

    def append(self, group_id: str, user_id: str, name: str, msg: str, bot_reply: str | None = None) -> None:
        if not msg.strip():
            return
        day = self._today()
        records = self._load(group_id, day, user_id)
        records.append({
            "ts": time.time(),
            "name": name,
            "msg": msg[:300],
            "bot_reply": bot_reply[:200] if bot_reply else None,
        })
        if len(records) > MAX_PER_USER_PER_DAY:
            records = records[-MAX_PER_USER_PER_DAY:]
        self._save(group_id, day, user_id, records)

    def get_users_for_day(self, group_id: str, day: str) -> list[str]:
        d = self._base / group_id / day
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.json")]

    def get_records(self, group_id: str, day: str, user_id: str) -> list[dict]:
        return self._load(group_id, day, user_id)

    def get_yesterday(self) -> str:
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).isoformat()

    def cleanup_old(self, keep_days: int = 3) -> None:
        from datetime import timedelta
        cutoff = date.today() - timedelta(days=keep_days)
        for group_dir in self._base.iterdir():
            if not group_dir.is_dir():
                continue
            for day_dir in group_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    if date.fromisoformat(day_dir.name) < cutoff:
                        import shutil
                        shutil.rmtree(day_dir)
                except ValueError:
                    pass
