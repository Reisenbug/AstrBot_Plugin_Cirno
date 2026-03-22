import json
import re
import time

from astrbot.api import logger

AFFINITY_LEVELS = [
    (0, 15, "讨厌", "你不太喜欢这个人，态度冷淡甚至有点凶，不想搭理。"),
    (16, 30, "冷淡", "你对这个人没什么感觉，回答简短，不太热情。"),
    (31, 45, "普通", "你对这个人态度一般，正常交流。"),
    (46, 60, "友好", "你觉得这个人还不错，比较愿意和他聊天。"),
    (61, 75, "喜欢", "你挺喜欢这个人的，会比较主动，语气亲近。"),
    (76, 90, "很喜欢", "你很喜欢这个人，会撒娇、分享秘密、主动关心。"),
    (91, 100, "最好的朋友", "这个人是你最重要的朋友之一，你会无条件信任和依赖。"),
]

AFFINITY_WEIGHTS = {
    "familiarity": 0.15,
    "trust": 0.3,
    "fun": 0.2,
    "importance": 0.35,
}

STATE_CATEGORY_MODIFIERS = {
    "rest": {"negative": 1.5},
    "social": {"positive": 1.3},
    "rare": {"negative": 1.5},
}

INNER_PATTERN = re.compile(r"<inner>(.*?)</inner>", re.DOTALL)

RATING_PROMPT = (
    "\n【情绪反馈规则】"
    "你需要在每次回复的最末尾附加一个 <inner> 标签，格式为 <inner>{\"valence_shift\": N, \"reason\": \"...\"}</inner>。"
    "valence_shift 是一个 0~1 的小数，代表这次对话给你带来的情绪倾向：0.5 为中性，高于 0.5 为正面，低于 0.5 为负面。"
    "reason 是简短的一句话，说明为什么你有这种感受。"
    "判断依据——以你（琪露诺）的主观感受为准：对方是否让你开心、生气、无聊、兴奋、难过。"
    "示例：普通闲聊 <inner>{\"valence_shift\": 0.5, \"reason\": \"普通的聊天\"}</inner>，"
    "被夸可爱 <inner>{\"valence_shift\": 0.8, \"reason\": \"被夸了好开心\"}</inner>，"
    "被骂笨蛋 <inner>{\"valence_shift\": 0.2, \"reason\": \"被骂了好生气\"}</inner>。"
    "注意：<inner> 标签必须放在回复的最后，不要在回复正文中提及情绪、好感度或这个标签。"
)

KEY_EVENT_PROMPT = """你是琪露诺（⑨），幻想乡最强的冰精灵。
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
{{"event": "事件的简短描述", "dimension": "受影响的好感维度（trust/fun/importance）", "delta": 0.1, "memory": "你想记住的一句话（用琪露诺视角）"}}

delta 范围 -0.15 ~ +0.15，正面事件为正，负面事件为负。
dimension 说明：trust=信任相关，fun=有趣相关，importance=重要性相关。

如果没有关键事件，只输出：null

只输出JSON或null，不要输出其他内容。"""


class AffinityManager:
    def __init__(self, plugin, decay_rate: float = 0.5, mood_decay_rate: float = 1.0):
        self._plugin = plugin
        self._decay_rate = decay_rate
        self._mood_decay_rate = mood_decay_rate

        self._emotion = {
            "baseline": 0.7,
            "valence": 0.7,
            "arousal": 0.5,
            "vulnerability": 0.2,
        }
        self._user_data: dict[str, dict] = {}
        self._event_counters: dict[str, int] = {}

    def _validate_emotion(self, data: dict) -> dict:
        defaults = {"baseline": 0.7, "valence": 0.7, "arousal": 0.5, "vulnerability": 0.2}
        result = {}
        for key, default in defaults.items():
            try:
                val = float(data.get(key, default))
                result[key] = max(0.0, min(1.0, val))
            except (TypeError, ValueError):
                result[key] = default
        return result

    async def load(self):
        emotion_saved = await self._plugin.get_kv_data("cirno_emotion", None)
        if emotion_saved and isinstance(emotion_saved, dict):
            self._emotion = self._validate_emotion(emotion_saved)
        else:
            old_mood = await self._plugin.get_kv_data("cirno_mood", None)
            if old_mood is not None:
                try:
                    mood_val = float(old_mood)
                    self._emotion["valence"] = max(0.0, min(1.0, (mood_val + 10) / 20))
                    self._emotion["baseline"] = self._emotion["valence"]
                    logger.info(f"好感度迁移：旧 cirno_mood={mood_val} → valence={self._emotion['valence']:.2f}")
                except (TypeError, ValueError):
                    logger.warning(f"好感度迁移：旧 cirno_mood 值无效，使用默认值")

        user_saved = await self._plugin.get_kv_data("affinity_data_v2", None)
        if user_saved and isinstance(user_saved, dict):
            self._user_data = user_saved
        else:
            old_data = await self._plugin.get_kv_data("affinity_data", None)
            if old_data and isinstance(old_data, dict):
                for uid, entry in old_data.items():
                    try:
                        old_val = entry.get("value", 50.0) if isinstance(entry, dict) else 50.0
                        normalized = float(old_val) / 100.0
                        self._user_data[uid] = {
                            "familiarity": min(1.0, normalized * 0.8),
                            "trust": max(0.0, min(1.0, 0.3 + normalized * 0.4)),
                            "fun": 0.5,
                            "importance": max(0.0, min(1.0, normalized * 0.3)),
                            "last_ts": entry.get("last_ts", time.time()) if isinstance(entry, dict) else time.time(),
                        }
                    except (TypeError, ValueError):
                        continue
                logger.info(f"好感度迁移：旧格式 → 四维好感度，共 {len(self._user_data)} 人")

        counters_saved = await self._plugin.get_kv_data("affinity_event_counters", None)
        if counters_saved and isinstance(counters_saved, dict):
            self._event_counters = {k: v for k, v in counters_saved.items() if isinstance(v, (int, float))}

        logger.info(
            f"好感度系统已加载：{len(self._user_data)} 人，"
            f"valence={self._emotion['valence']:.2f}, arousal={self._emotion['arousal']:.2f}, "
            f"vulnerability={self._emotion['vulnerability']:.2f}"
        )

    async def save(self):
        await self._plugin.put_kv_data("cirno_emotion", self._emotion)
        await self._plugin.put_kv_data("affinity_data_v2", self._user_data)
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

    def get_composite(self, user_id: str) -> float:
        ud = self._user_data.get(user_id)
        if not ud:
            return 30.0
        score = (
            ud["familiarity"] * AFFINITY_WEIGHTS["familiarity"]
            + ud["trust"] * AFFINITY_WEIGHTS["trust"]
            + ud["fun"] * AFFINITY_WEIGHTS["fun"]
            + ud["importance"] * AFFINITY_WEIGHTS["importance"]
        )
        return max(0.0, min(100.0, score * 100))

    def get_level(self, user_id: str) -> str:
        value = self.get_composite(user_id)
        for low, high, name, _ in AFFINITY_LEVELS:
            if low <= value <= high:
                return name
        return "普通"

    def get_user_data(self, user_id: str) -> dict:
        return self._user_data.get(user_id, {
            "familiarity": 0.0, "trust": 0.5, "fun": 0.5, "importance": 0.0,
        })

    def extract_inner(self, bot_reply: str) -> tuple[str, float | None, str | None]:
        m = INNER_PATTERN.search(bot_reply)
        if not m:
            return bot_reply, None, None
        cleaned = bot_reply[:m.start()].rstrip() + bot_reply[m.end():].rstrip()
        cleaned = cleaned.strip()
        try:
            data = json.loads(m.group(1))
            vs = float(data.get("valence_shift", 0.5))
            vs = max(0.0, min(1.0, vs))
            reason = data.get("reason")
            return cleaned, vs, reason
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

        e["valence"] = e["valence"] * 0.95 + e["baseline"] * 0.05

        e["valence"] = max(0.0, min(1.0, e["valence"]))
        e["arousal"] = max(0.0, min(1.0, e["arousal"]))
        e["vulnerability"] = max(0.0, min(1.0, e["vulnerability"]))

    def update_affinity(self, user_id: str, valence_shift: float):
        ud = self._user_data.get(user_id)
        if not ud:
            ud = {"familiarity": 0.0, "trust": 0.5, "fun": 0.5, "importance": 0.0, "last_ts": time.time()}
            self._user_data[user_id] = ud

        ud["familiarity"] = min(1.0, ud["familiarity"] + 0.005)

        if valence_shift > 0.55:
            ud["trust"] = min(1.0, ud["trust"] + (valence_shift - 0.5) * 0.05)
        elif valence_shift < 0.45:
            ud["trust"] = max(0.0, ud["trust"] - (0.5 - valence_shift) * 0.08)

        if self._emotion["arousal"] > 0.6:
            ud["fun"] = min(1.0, ud["fun"] + (self._emotion["arousal"] - 0.5) * 0.03)
        else:
            ud["fun"] = ud["fun"] * 0.998 + 0.5 * 0.002

        ud["last_ts"] = time.time()

    def update_key_event(self, user_id: str, dimension: str, delta: float):
        ud = self._user_data.get(user_id)
        if not ud:
            return
        if dimension not in ("trust", "fun", "importance"):
            return
        delta = max(-0.15, min(0.15, delta))
        ud[dimension] = max(0.0, min(1.0, ud[dimension] + delta))

    def increment_event_counter(self, user_id: str) -> int:
        self._event_counters[user_id] = self._event_counters.get(user_id, 0) + 1
        return self._event_counters[user_id]

    def reset_event_counter(self, user_id: str):
        self._event_counters[user_id] = 0

    def build_mood_prompt(self) -> str:
        v = self._emotion["valence"]
        a = self._emotion["arousal"]
        vuln = self._emotion["vulnerability"]

        if v >= 0.6 and a >= 0.6:
            desc = "你现在特别兴奋，话多，什么都觉得好玩！"
        elif v >= 0.6 and a < 0.6:
            desc = "你现在心情不错，懒洋洋的，很满足。"
        elif v < 0.4 and a >= 0.6:
            desc = "你现在又委屈又生气，随时可能炸！"
        elif v < 0.4:
            desc = "你现在闷闷不乐的，不太想说话，回复会比平时短。"
        else:
            desc = "你现在心情平平的，正常状态。"

        if vuln > 0.6:
            desc += "你现在有点脆弱，别人稍微说重一点你就想哭。"

        return f"\n【当前情绪：valence={v:.2f} arousal={a:.2f}】{desc}"

    def build_status_prompt(self, user_id: str) -> str:
        composite = self.get_composite(user_id)
        level = self.get_level(user_id)
        ud = self.get_user_data(user_id)

        parts = [f"\n【对当前对话者的好感度：{level}（{composite:.0f}/100）】"]

        for low, high, name, prompt in AFFINITY_LEVELS:
            if low <= composite <= high:
                parts.append(prompt)
                break

        traits = []
        if ud["familiarity"] > 0.7:
            traits.append("你们经常聊天，很熟悉")
        elif ud["familiarity"] < 0.2:
            traits.append("你和这个人不太熟")
        if ud["trust"] > 0.7:
            traits.append("你很信任这个人")
        elif ud["trust"] < 0.3:
            traits.append("你不太信任这个人")
        if ud["fun"] > 0.7:
            traits.append("你觉得和这个人聊天很有趣")
        if ud["importance"] > 0.5:
            traits.append("这个人对你来说很重要")

        if traits:
            parts.append("（" + "，".join(traits) + "）")

        return "".join(parts)

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
            if not isinstance(result, dict):
                return None
            if "event" not in result or "dimension" not in result or "delta" not in result:
                return None
            result["delta"] = max(-0.15, min(0.15, float(result["delta"])))
            if result["dimension"] not in ("trust", "fun", "importance"):
                return None
            return result
        except (json.JSONDecodeError, ValueError):
            return None

    def get_debug_info(self, user_id: str | None = None) -> dict:
        info = {
            "valence": self._emotion["valence"],
            "arousal": self._emotion["arousal"],
            "vulnerability": self._emotion["vulnerability"],
            "baseline": self._emotion["baseline"],
        }
        if user_id:
            ud = self.get_user_data(user_id)
            info["user"] = {
                "familiarity": ud["familiarity"],
                "trust": ud["trust"],
                "fun": ud["fun"],
                "importance": ud["importance"],
                "composite": self.get_composite(user_id),
                "level": self.get_level(user_id),
            }
        return info
