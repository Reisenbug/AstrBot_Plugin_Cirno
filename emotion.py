"""琪露诺此刻的情绪。

原本这里还有一套四维好感度（familiarity/trust/fun/importance 加权成 0~100 的
等级）。删掉了：它算了三个月一次都没落盘，`fun` 只涨不跌，"今天对这个人的感觉"
其实是 md5(日期+QQ号) 的随机数。而"这个人给我的感觉"核心记忆的印象写得好得多，
一句话顶四个浮点数。

留下的是真在起作用的部分：一个全局情绪。她心情差就更想去戳人，心情好才有兴致
搞恶作剧，情绪上头或心里发软才会自己开口。这些都只看当下情绪，不看对谁。
"""

import json
import re
import time

from astrbot.api import logger

INNER_PATTERN = re.compile(r"<inner>(.*?)</inner>", re.DOTALL)

# 状态类别对情绪波动的放大：休息时被吵更烦，社交时的好事更受用
STATE_CATEGORY_MODIFIERS = {
    "rest": {"negative": 1.5},
    "social": {"positive": 1.3},
    "rare": {"negative": 1.5},
}

_SENTIMENT_TO_VALENCE = {
    ("positive", "strong"): 0.85,
    ("positive", "mild"):   0.65,
    ("neutral",  "strong"): 0.50,
    ("neutral",  "mild"):   0.50,
    ("negative", "mild"):   0.35,
    ("negative", "strong"): 0.15,
}

RATING_PROMPT = (
    "\n【必须遵守】每条回复末尾附上情绪标签，格式："
    "<inner>{\"sentiment\": \"positive/neutral/negative\", \"intensity\": \"mild/strong\", "
    "\"reason\": \"一句话\"}</inner>"
    "\nsentiment评估对方说的话对你情绪的影响（不是对方的情绪状态）。"
    "\n不要在正文中提及标签。"
)

KEY_EVENT_PROMPT = """你是琪露诺，幻想乡最强的冰精灵。
回顾下面这段你和「{nickname}」最近的对话，从你（琪露诺）的主观视角判断：有没有发生什么让你印象深刻的关键事件？

关键事件的例子：
- 对方帮了你一个大忙、教会你一个很厉害的东西
- 对方伤害了你的感情、严重侮辱你
- 你们分享了一个很有趣的经历
- 对方告诉你一个重要的秘密
- 对方连续多次对你很好/很差

最近的对话：
{messages}

如果有关键事件，用JSON格式输出：
{{"event": "事件的简短描述", "weight": 0.1, "memory": "用平静内省的语气记录这件事，不要带口癖、语气词、emoji"}}

weight 范围 -0.15 ~ +0.15，正面事件为正，负面事件为负，绝对值越大代表这件事越重要。

如果没有关键事件，只输出：null

只输出JSON或null，不要输出其他内容。"""


class EmotionManager:
    def __init__(self, plugin):
        self._plugin = plugin
        self._emotion = {
            "valence": 0.7,        # 心情好坏
            "arousal": 0.5,        # 情绪烈度，0.5 为平静
            "vulnerability": 0.2,  # 心里发软、想找人的程度
        }
        self._event_counters: dict[str, int] = {}

    def _validate_emotion(self, data: dict) -> dict:
        defaults = {"valence": 0.7, "arousal": 0.5, "vulnerability": 0.2}
        result = {}
        for key, default in defaults.items():
            try:
                result[key] = max(0.0, min(1.0, float(data.get(key, default))))
            except (TypeError, ValueError):
                result[key] = default
        return result

    async def load(self):
        saved = await self._plugin.get_kv_data("cirno_emotion", None)
        if saved and isinstance(saved, dict):
            self._emotion = self._validate_emotion(saved)

        counters = await self._plugin.get_kv_data("affinity_event_counters", None)
        if counters and isinstance(counters, dict):
            self._event_counters = {
                k: int(v) for k, v in counters.items() if isinstance(v, (int, float))
            }

        logger.info(
            f"情绪已加载：valence={self._emotion['valence']:.2f}, "
            f"arousal={self._emotion['arousal']:.2f}, "
            f"vulnerability={self._emotion['vulnerability']:.2f}"
        )

    async def save(self):
        await self._plugin.put_kv_data("cirno_emotion", self._emotion)
        await self._plugin.put_kv_data("affinity_event_counters", self._event_counters)

    @property
    def valence(self) -> float:
        return self._emotion["valence"]

    @property
    def arousal(self) -> float:
        return self._emotion["arousal"]

    @property
    def vulnerability(self) -> float:
        return self._emotion["vulnerability"]

    @staticmethod
    def peek_sentiment(bot_reply: str) -> tuple[str, str]:
        """只取 <inner> 里的原始 sentiment/intensity，供心情层使用。取不到返回空。"""
        m = INNER_PATTERN.search(bot_reply)
        if not m:
            return "", ""
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError, AttributeError):
            return "", ""
        return (
            str(data.get("sentiment", "")).strip().lower(),
            str(data.get("intensity", "")).strip().lower(),
        )

    def extract_inner(self, bot_reply: str) -> tuple[str, float | None, str | None]:
        """剥掉 <inner> 标签，返回 (正文, valence_shift, reason)。"""
        m = INNER_PATTERN.search(bot_reply)
        if not m:
            return bot_reply, None, None
        cleaned = (bot_reply[:m.start()].rstrip() + bot_reply[m.end():].rstrip()).strip()
        try:
            data = json.loads(m.group(1))
            sentiment = str(data.get("sentiment", "neutral")).strip().lower()
            intensity = str(data.get("intensity", "mild")).strip().lower()
            vs = _SENTIMENT_TO_VALENCE.get((sentiment, intensity), 0.5)
            return cleaned, vs, data.get("reason")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return cleaned, None, None

    def update_emotion(self, valence_shift: float, state_category: str):
        e = self._emotion
        adjusted_shift = valence_shift
        if state_category in STATE_CATEGORY_MODIFIERS:
            mods = STATE_CATEGORY_MODIFIERS[state_category]
            if adjusted_shift < 0.5 and "negative" in mods:
                adjusted_shift = 0.5 - (0.5 - adjusted_shift) * mods["negative"]
            elif adjusted_shift > 0.5 and "positive" in mods:
                adjusted_shift = 0.5 + (adjusted_shift - 0.5) * mods["positive"]
            adjusted_shift = max(0.0, min(1.0, adjusted_shift))

        e["valence"] = e["valence"] * 0.7 + adjusted_shift * 0.3

        shift_intensity = abs(adjusted_shift - 0.5) * 2
        e["arousal"] = e["arousal"] * 0.8 + shift_intensity * 0.2

        if e["valence"] < 0.4:
            e["vulnerability"] = min(1.0, e["vulnerability"] + 0.05)
        e["vulnerability"] *= 0.95

        # 向平静回落，避免一直卡在极端情绪
        e["valence"] = e["valence"] * 0.95 + 0.5 * 0.05

        for k in e:
            e[k] = max(0.0, min(1.0, e[k]))

    def increment_event_counter(self, user_id: str) -> int:
        self._event_counters[user_id] = self._event_counters.get(user_id, 0) + 1
        return self._event_counters[user_id]

    def reset_event_counter(self, user_id: str):
        self._event_counters[user_id] = 0

    def build_rating_prompt(self) -> str:
        return RATING_PROMPT

    def build_key_event_prompt(self, nickname: str, messages: str) -> str:
        return KEY_EVENT_PROMPT.format(nickname=nickname, messages=messages)

    def parse_key_event_result(self, text: str) -> dict | None:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        if text.lower() == "null" or not text:
            return None
        try:
            result = json.loads(text)
            if not isinstance(result, dict) or "event" not in result:
                return None
            result["weight"] = max(-0.15, min(0.15, float(result.get("weight", 0.05))))
            return result
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def get_debug_info(self) -> dict:
        return dict(self._emotion)
