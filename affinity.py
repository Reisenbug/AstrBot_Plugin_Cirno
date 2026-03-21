import logging
import re
import time

logger = logging.getLogger("astrbot")

AFFINITY_LEVELS = [
    (0, 15, "讨厌", "你不太喜欢这个人，态度冷淡甚至有点凶，不想搭理。"),
    (16, 30, "冷淡", "你对这个人没什么感觉，回答简短，不太热情。"),
    (31, 45, "普通", "你对这个人态度一般，正常交流。"),
    (46, 60, "友好", "你觉得这个人还不错，比较愿意和他聊天。"),
    (61, 75, "喜欢", "你挺喜欢这个人的，会比较主动，语气亲近。"),
    (76, 90, "很喜欢", "你很喜欢这个人，会撒娇、分享秘密、主动关心。"),
    (91, 100, "最好的朋友", "这个人是你最重要的朋友之一，你会无条件信任和依赖。"),
]

STATE_CATEGORY_MODIFIERS = {
    "sleep": {"negative": 1.5},
    "social": {"positive": 1.3},
    "rare": {"negative": 1.5},
}

RATING_PROMPT = (
    "\n【好感度评价规则】"
    "你需要在每次回复的最末尾附加一个好感度变化标记，格式为 [好感:+N] 或 [好感:-N]，N 为 0~5 的整数。"
    "这个标记用来表示这次对话让你对对方的好感变化了多少。"
    "判断依据：对方是否友善、是否尊重你、是否夸你、是否在骂你或嘲笑你、是否关心你、是否无聊敷衍。"
    "正常闲聊给 [好感:+0]，夸你或关心你给 +1~3，特别暖心给 +4~5，骂你或嘲讽给 -1~3，严重侮辱给 -4~5。"
    "以你（琪露诺）的主观感受为准，不需要客观。"
    "注意：标记必须放在回复的最后一行，单独一行，不要在回复正文中提及好感度或这个标记。"
)

_DELTA_PATTERN = re.compile(r"\[好感:\s*([+-]?\d+)\s*\]\s*$")


class AffinityManager:
    def __init__(self, plugin, decay_rate: float = 0.5):
        self._plugin = plugin
        self._decay_rate = decay_rate
        self._data: dict[str, dict] = {}

    async def load(self):
        saved = await self._plugin.get_kv_data("affinity_data", None)
        if saved and isinstance(saved, dict):
            self._data = saved
        logger.info(f"好感度数据已加载，共 {len(self._data)} 人")

    async def save(self):
        await self._plugin.put_kv_data("affinity_data", self._data)

    def get(self, user_id: str) -> float:
        entry = self._data.get(user_id)
        if entry is None:
            return 50.0
        return entry.get("value", 50.0)

    def get_level(self, user_id: str) -> str:
        value = self.get(user_id)
        for low, high, name, _ in AFFINITY_LEVELS:
            if low <= value <= high:
                return name
        return "友好"

    def extract_delta(self, bot_reply: str) -> tuple[str, int]:
        """从回复末尾提取 [好感:±N]，返回 (清理后的文本, delta)。"""
        m = _DELTA_PATTERN.search(bot_reply)
        if not m:
            return bot_reply, 0
        delta = int(m.group(1))
        delta = max(-5, min(5, delta))
        cleaned = bot_reply[:m.start()].rstrip()
        return cleaned, delta

    def update(self, user_id: str, delta: int, state_category: str) -> float:
        current = self.get(user_id)

        adjusted = float(delta)
        if state_category in STATE_CATEGORY_MODIFIERS:
            mods = STATE_CATEGORY_MODIFIERS[state_category]
            if adjusted > 0 and "positive" in mods:
                adjusted *= mods["positive"]
            elif adjusted < 0 and "negative" in mods:
                adjusted *= mods["negative"]

        if current > 50:
            adjusted -= self._decay_rate
        elif current < 50:
            adjusted += self._decay_rate

        new_value = max(0.0, min(100.0, current + adjusted))
        self._data[user_id] = {"value": new_value, "last_ts": time.time()}
        return adjusted

    def build_status_prompt(self, user_id: str) -> str:
        value = self.get(user_id)
        for low, high, name, prompt in AFFINITY_LEVELS:
            if low <= value <= high:
                return f"\n【对当前对话者的好感度：{name}（{value:.0f}/100）】{prompt}"
        return ""

    def build_rating_prompt(self) -> str:
        return RATING_PROMPT
