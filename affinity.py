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

MOOD_LEVELS = [
    (-10, -7, "很差", "你现在心情非常糟糕，暴躁易怒，说话带刺，很容易发火。"),
    (-6, -3, "不好", "你现在心情不太好，有点烦躁，不太想搭理人，语气冲。"),
    (-2, 2, "一般", "你现在心情平平，正常状态。"),
    (3, 6, "不错", "你现在心情挺好的，比平时更活泼开朗，愿意多聊几句。"),
    (7, 10, "超好", "你现在心情特别好，非常兴奋开心，会主动分享快乐，语气超级元气。"),
]

STATE_CATEGORY_MODIFIERS = {
    "sleep": {"negative": 1.5},
    "social": {"positive": 1.3},
    "rare": {"negative": 1.5},
}

RATING_PROMPT = (
    "\n【心情和好感度评价规则】"
    "你需要在每次回复的最末尾单独一行附加标记，格式为 [心情:+N,好感:+N]，N 为整数。"
    "心情变化范围 -3~+3，代表这次对话对你整体情绪的影响（对所有人生效）。"
    "好感变化范围 -5~+5，代表这次对话让你对这个人的好感变化了多少（只对当前对话者生效）。"
    "判断依据——"
    "心情：对方说的话是否让你开心/生气/无聊/兴奋，综合你当前在做什么来判断。"
    "好感：对方是否友善、尊重你、夸你、关心你，或者在骂你、嘲笑你、敷衍你。"
    "以你（琪露诺）的主观感受为准。"
    "示例：普通闲聊 [心情:+0,好感:+0]，被夸可爱 [心情:+2,好感:+3]，被骂笨蛋 [心情:-2,好感:-3]。"
    "注意：标记必须放在回复的最后一行，单独一行，不要在回复正文中提及心情、好感度或这个标记。"
)

_DELTA_PATTERN = re.compile(
    r"\[心情:\s*([+-]?\d+)\s*,\s*好感:\s*([+-]?\d+)\s*\]\s*$"
)


class AffinityManager:
    def __init__(self, plugin, decay_rate: float = 0.5, mood_decay_rate: float = 1.0):
        self._plugin = plugin
        self._decay_rate = decay_rate
        self._mood_decay_rate = mood_decay_rate
        self._data: dict[str, dict] = {}
        self._mood: float = 0.0

    async def load(self):
        saved = await self._plugin.get_kv_data("affinity_data", None)
        if saved and isinstance(saved, dict):
            self._data = saved
        mood_saved = await self._plugin.get_kv_data("cirno_mood", None)
        if mood_saved is not None:
            self._mood = float(mood_saved)
        logger.info(f"好感度数据已加载，共 {len(self._data)} 人，当前心情={self._mood:.1f}")

    async def save(self):
        await self._plugin.put_kv_data("affinity_data", self._data)
        await self._plugin.put_kv_data("cirno_mood", self._mood)

    @property
    def mood(self) -> float:
        return self._mood

    def get_mood_level(self) -> str:
        for low, high, name, _ in MOOD_LEVELS:
            if low <= self._mood <= high:
                return name
        return "一般"

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

    def extract_delta(self, bot_reply: str) -> tuple[str, int, int]:
        """从回复末尾提取 [心情:±N,好感:±N]，返回 (清理后文本, mood_delta, affinity_delta)。"""
        m = _DELTA_PATTERN.search(bot_reply)
        if not m:
            return bot_reply, 0, 0
        mood_delta = max(-3, min(3, int(m.group(1))))
        affinity_delta = max(-5, min(5, int(m.group(2))))
        cleaned = bot_reply[:m.start()].rstrip()
        return cleaned, mood_delta, affinity_delta

    def update_mood(self, delta: int, state_category: str) -> float:
        adjusted = float(delta)
        if state_category in STATE_CATEGORY_MODIFIERS:
            mods = STATE_CATEGORY_MODIFIERS[state_category]
            if adjusted > 0 and "positive" in mods:
                adjusted *= mods["positive"]
            elif adjusted < 0 and "negative" in mods:
                adjusted *= mods["negative"]

        if self._mood > 0:
            adjusted -= self._mood_decay_rate
        elif self._mood < 0:
            adjusted += self._mood_decay_rate

        self._mood = max(-10.0, min(10.0, self._mood + adjusted))
        return adjusted

    def update_affinity(self, user_id: str, delta: int, state_category: str) -> float:
        current = self.get(user_id)
        adjusted = float(delta)

        if state_category in STATE_CATEGORY_MODIFIERS:
            mods = STATE_CATEGORY_MODIFIERS[state_category]
            if adjusted > 0 and "positive" in mods:
                adjusted *= mods["positive"]
            elif adjusted < 0 and "negative" in mods:
                adjusted *= mods["negative"]

        if self._mood > 3:
            adjusted += 0.5
        elif self._mood < -3:
            adjusted -= 0.5

        if current > 50:
            adjusted -= self._decay_rate
        elif current < 50:
            adjusted += self._decay_rate

        new_value = max(0.0, min(100.0, current + adjusted))
        self._data[user_id] = {"value": new_value, "last_ts": time.time()}
        return adjusted

    def build_mood_prompt(self) -> str:
        for low, high, name, prompt in MOOD_LEVELS:
            if low <= self._mood <= high:
                return f"\n【当前心情：{name}（{self._mood:.0f}）】{prompt}"
        return ""

    def build_status_prompt(self, user_id: str) -> str:
        value = self.get(user_id)
        for low, high, name, prompt in AFFINITY_LEVELS:
            if low <= value <= high:
                return f"\n【对当前对话者的好感度：{name}（{value:.0f}/100）】{prompt}"
        return ""

    def build_rating_prompt(self) -> str:
        return RATING_PROMPT
