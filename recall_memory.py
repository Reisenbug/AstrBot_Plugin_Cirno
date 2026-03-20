import logging
import math
import re
import time
from datetime import datetime

logger = logging.getLogger("astrbot")

STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "吗", "什么",
    "那", "啊", "呢", "吧", "嗯", "哦", "哈", "呀", "哪", "怎么",
    "可以", "这个", "那个", "但是", "因为", "所以", "如果", "虽然",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "my", "your", "his", "its", "our", "their", "this", "that",
    "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
}


def extract_keywords(text: str) -> list[str]:
    text = text.lower()
    words = re.findall(r'[a-zA-Z0-9]+', text)
    cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
    bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    all_tokens = words + cjk_chars + bigrams
    return [t for t in all_tokens if t not in STOP_WORDS and len(t) > 0]


class RecallMemory:
    def __init__(self, plugin, max_months: int = 4, top_k: int = 3):
        self._plugin = plugin
        self._max_months = max_months
        self._top_k = top_k
        self._current_month_key = ""
        self._current_month_data: list[dict] = []
        self._months_index: list[str] = []
        self._history_cache: list[dict] = []

    async def load(self):
        self._months_index = await self._plugin.get_kv_data("recall_months", None) or []
        now = datetime.now()
        self._current_month_key = f"recall_{now.year}_{now.month:02d}"
        if self._current_month_key not in self._months_index:
            self._months_index.append(self._current_month_key)
            await self._plugin.put_kv_data("recall_months", self._months_index)
        self._current_month_data = (
            await self._plugin.get_kv_data(self._current_month_key, None) or []
        )

        self._history_cache = []
        for month_key in self._months_index:
            if month_key == self._current_month_key:
                continue
            month_data = await self._plugin.get_kv_data(month_key, None)
            if month_data and isinstance(month_data, list):
                self._history_cache.extend(month_data)

        total = len(self._current_month_data) + len(self._history_cache)
        logger.info(
            f"回忆记忆已加载，当前月 {self._current_month_key}，"
            f"共 {total} 条记录，{len(self._months_index)} 个月份"
        )

    async def save_current_month(self):
        await self._plugin.put_kv_data(self._current_month_key, self._current_month_data)
        await self._plugin.put_kv_data("recall_months", self._months_index)

    async def archive(self, user_id: str, user_name: str, user_msg: str, bot_reply: str):
        now = datetime.now()
        month_key = f"recall_{now.year}_{now.month:02d}"
        if month_key != self._current_month_key:
            await self.save_current_month()
            self._history_cache.extend(self._current_month_data)
            self._current_month_key = month_key
            if month_key not in self._months_index:
                self._months_index.append(month_key)
            self._current_month_data = (
                await self._plugin.get_kv_data(month_key, None) or []
            )

        keywords = extract_keywords(user_msg + " " + bot_reply)
        entry = {
            "ts": time.time(),
            "uid": str(user_id),
            "name": user_name,
            "msg": user_msg[:200],
            "reply": bot_reply[:200],
            "kw": keywords[:20],
        }
        self._current_month_data.append(entry)
        await self.save_current_month()

    def search(self, query: str, current_user_id: str | None = None, top_k: int | None = None) -> list[dict]:
        if top_k is None:
            top_k = self._top_k
        query_kw = set(extract_keywords(query))
        if not query_kw:
            return []

        now = time.time()
        scored: list[tuple[float, dict]] = []

        for entry in self._history_cache + self._current_month_data:
            entry_kw = set(entry.get("kw", []))
            if not entry_kw:
                continue

            overlap = len(query_kw & entry_kw)
            if overlap == 0:
                continue

            kw_score = overlap / max(len(query_kw), 1)
            age_hours = (now - entry.get("ts", now)) / 3600
            time_decay = math.exp(-age_hours / (24 * 30))
            user_bonus = 0.2 if current_user_id and entry.get("uid") == str(current_user_id) else 0.0
            score = kw_score * 0.7 + time_decay * 0.1 + user_bonus

            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    def build_recall_prompt(self, memories: list[dict]) -> str:
        if not memories:
            return ""
        lines = []
        for m in memories:
            name = m.get("name", "某人")
            msg = m.get("msg", "")[:50]
            reply = m.get("reply", "")[:50]
            ts = m.get("ts", 0)
            dt = datetime.fromtimestamp(ts)
            time_str = dt.strftime("%m月%d日")
            lines.append(f"- {time_str}，{name}说「{msg}」，你回答了「{reply}」")
        return "【你隐约记得这些事】\n" + "\n".join(lines)

    async def cleanup_old_months(self):
        if len(self._months_index) <= self._max_months:
            return
        to_remove = self._months_index[: len(self._months_index) - self._max_months]
        for key in to_remove:
            await self._plugin.delete_kv_data(key)
            logger.info(f"回忆记忆：清理过期月份 {key}")
        self._months_index = self._months_index[len(to_remove):]
        await self._plugin.put_kv_data("recall_months", self._months_index)
