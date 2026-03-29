import json
import time
from pathlib import Path

from astrbot.api import logger

from .recall_memory import extract_keywords

MAX_SLANG = 50


class SlangStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "slang_store.json"
        self._entries: list[dict] = []

    def load(self) -> None:
        if not self._path.exists():
            self._entries = []
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._entries = data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"SlangStore: 读取失败: {e}")
            self._entries = []

    def save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"SlangStore: 写入失败: {e}")

    def get_all(self) -> list[dict]:
        return list(self._entries)

    def add(self, word: str, meaning: str, scene: str) -> bool:
        word = word.strip()
        if not word:
            return False
        if any(e["word"] == word for e in self._entries):
            return False
        self._entries.append({
            "word": word,
            "meaning": meaning.strip(),
            "scene": scene.strip(),
            "ts": time.time(),
        })
        if len(self._entries) > MAX_SLANG:
            self._entries.sort(key=lambda e: e.get("ts", 0))
            self._entries = self._entries[-MAX_SLANG:]
        return True

    def match(self, text: str) -> list[dict]:
        if not text or not self._entries:
            return []
        msg_kw = set(extract_keywords(text))
        if not msg_kw:
            return []
        matched = []
        for entry in self._entries:
            scene_kw = set(entry.get("scene", "").split())
            if scene_kw & msg_kw:
                matched.append(entry)
        return matched[:3]
