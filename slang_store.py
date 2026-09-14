import json
import time
from pathlib import Path

from astrbot.api import logger

MAX_SLANG = 50
MAX_EXAMPLES = 3

# 硬黑名单：即便 LLM 漏判也不入库——口癖语气词（防 daze 投毒）、切词碎片、
# 琪露诺自己的冰招式（黑话库不该怂恿她用冰梗，否则句句冻人）
_SLANG_BLOCKLIST = {
    "da", "ze", "daze", "だぜ", "的说",
    "这是", "咱才",
    "冻住", "冰雕", "冻", "冰冻",
}


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

    def add(self, word: str, examples: list[str]) -> bool:
        """只存词和群友的原话，不存"含义"。让模型给不认识的缩写下定义等于要求它做
        词义归纳(WSI)，那是公认未解的任务，它只会按表层模式硬编（"hyw"猜成"好诱吻"）。
        原话是真实数据，没有生成环节，编不错。"""
        word = word.strip()
        if not word:
            return False
        if word.lower() in _SLANG_BLOCKLIST:
            return False
        if any(e["word"] == word for e in self._entries):
            return False
        cleaned = [x.strip() for x in examples if x and x.strip()][:MAX_EXAMPLES]
        if not cleaned:
            return False
        self._entries.append({
            "word": word,
            "examples": cleaned,
            "ts": time.time(),
        })
        if len(self._entries) > MAX_SLANG:
            self._entries.sort(key=lambda e: e.get("ts", 0))
            self._entries = self._entries[-MAX_SLANG:]
        return True

    def match(self, text: str) -> list[dict]:
        """词本身出现在消息里就算命中。黑话多是 hyw/wzy/kuuki 这种分词器不认识的东西，
        走 jieba 只会被切碎，子串是唯一可靠的办法。"""
        if not text or not self._entries:
            return []
        lower = text.lower()
        matched = [e for e in self._entries if e["word"].lower() in lower]
        return matched[:3]
