import hashlib
import json
import re
import time

from astrbot.api import logger

INTERACTION_TYPE_WEIGHTS = {
    "compliment": {"trust": 1.0, "fun": 0.5, "importance": 0.3},
    "thanks":     {"trust": 1.2, "fun": 0.2, "importance": 0.2},
    "tease":      {"trust": 0.3, "fun": 1.5, "importance": 0.1},
    "care":       {"trust": 0.8, "fun": 0.3, "importance": 0.5},
    "insult":     {"trust": 1.5, "fun": 0.2, "importance": 0.1},
    "share":      {"trust": 0.5, "fun": 0.8, "importance": 0.8},
    "worry":      {"trust": 0.6, "fun": 0.1, "importance": 0.8},
    "apology":    {"trust": 2.0, "fun": 0.1, "importance": 1.0},
    "default":    {"trust": 1.0, "fun": 0.3, "importance": 0.1},
}

AFFINITY_LEVELS = [
    (0, 15, "无视"),
    (16, 30, "讨厌"),
    (31, 45, "冷淡"),
    (46, 60, "普通"),
    (61, 75, "友好"),
    (76, 90, "喜欢"),
    (91, 100, "很喜欢"),
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
    "\"interaction_type\": \"类型\", \"reason\": \"一句话\"}</inner>"
    "\nsentiment评估对方说的话对你情绪的影响（不是对方的情绪状态）。"
    "\ninteraction_type: compliment/thanks/tease/care/insult/share/worry/apology/default"
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
{{"event": "事件的简短描述", "dimension": "受影响的好感维度（trust/fun/importance）", "delta": 0.1, "memory": "用平静内省的语气记录这件事，不要带口癖、语气词、emoji"}}

delta 范围 -0.15 ~ +0.15，正面事件为正，负面事件为负。
dimension 说明：trust=信任相关，fun=有趣相关，importance=重要性相关。

如果没有关键事件，只输出：null

只输出JSON或null，不要输出其他内容。"""


class AffinityManager:
    def __init__(self, plugin, boredom_window: int = 300, boredom_threshold: int = 12):
        self._plugin = plugin
        self._boredom_window = boredom_window
        self._boredom_threshold = boredom_threshold

        self._emotion = {
            "baseline": 0.7,
            "valence": 0.7,
            "arousal": 0.5,
            "vulnerability": 0.2,
        }
        self._user_data: dict[str, dict] = {}
        self._event_counters: dict[str, int] = {}
        self._recent_interactions: dict[str, list[float]] = {}
        self._valence_history: dict[str, list[float]] = {}  # user_id -> recent valence_shifts
        self._WARMTH_WINDOW = 10

    @staticmethod
    def _daily_hash(seed: str) -> float:
        today = time.strftime("%Y-%m-%d")
        h = hashlib.md5(f"{today}:{seed}".encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    def _daily_baseline(self) -> float:
        r = self._daily_hash("baseline")
        return 0.45 + r * 0.4

    def _daily_user_drift(self, user_id: str) -> float:
        r = self._daily_hash(f"drift:{user_id}")
        return (r - 0.5) * 80

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
            self._event_counters = {k: int(v) for k, v in counters_saved.items() if isinstance(v, (int, float))}

        baseline = self._daily_baseline()
        self._emotion["baseline"] = baseline
        self._emotion["valence"] = baseline

        logger.info(
            f"好感度系统已加载：{len(self._user_data)} 人，"
            f"今日基准心情={baseline:.2f}, "
            f"arousal={self._emotion['arousal']:.2f}, "
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

    _USER_DEFAULTS = {"familiarity": 0.0, "trust": 0.5, "fun": 0.5, "importance": 0.0}

    def _safe_user_data(self, user_id: str) -> dict:
        ud = self._user_data.get(user_id)
        if not ud or not isinstance(ud, dict):
            return dict(self._USER_DEFAULTS)
        for key, default in self._USER_DEFAULTS.items():
            if key not in ud or not isinstance(ud[key], (int, float)):
                ud[key] = default
        return ud

    def get_composite(self, user_id: str) -> float:
        ud = self._safe_user_data(user_id)
        score = (
            ud["familiarity"] * AFFINITY_WEIGHTS["familiarity"]
            + ud["trust"] * AFFINITY_WEIGHTS["trust"]
            + ud["fun"] * AFFINITY_WEIGHTS["fun"]
            + ud["importance"] * AFFINITY_WEIGHTS["importance"]
        )
        drift = self._daily_user_drift(user_id)
        return max(0.0, min(100.0, score * 100 + drift))

    def get_level(self, user_id: str) -> str:
        value = self.get_composite(user_id)
        for low, high, name in AFFINITY_LEVELS:
            if low <= value <= high:
                return name
        return "普通"

    def get_user_data(self, user_id: str) -> dict:
        return self._safe_user_data(user_id)

    def extract_inner(self, bot_reply: str) -> tuple[str, float | None, str | None, str | None]:
        m = INNER_PATTERN.search(bot_reply)
        if not m:
            return bot_reply, None, None, None
        cleaned = bot_reply[:m.start()].rstrip() + bot_reply[m.end():].rstrip()
        cleaned = cleaned.strip()
        try:
            data = json.loads(m.group(1))
            reason = data.get("reason")
            interaction_type = data.get("interaction_type") or None
            if interaction_type and interaction_type not in INTERACTION_TYPE_WEIGHTS:
                interaction_type = None

            sentiment = data.get("sentiment", "neutral").strip().lower()
            intensity = data.get("intensity", "mild").strip().lower()
            vs = _SENTIMENT_TO_VALENCE.get((sentiment, intensity), 0.5)

            return cleaned, vs, reason, interaction_type
        except (json.JSONDecodeError, ValueError, AttributeError):
            return cleaned, None, None, None

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

    _DRIFT_TARGETS = {"trust": 0.5, "fun": 0.5}
    _DRIFT_RATE = 0.002        # 每次互动向中性漂移的速率
    _STREAK_THRESHOLD = 5      # 连续正面互动触发加速的次数
    _STREAK_BOOST = 2.0        # 加速倍率
    _BIG_MOMENT_TYPES = {"care", "share", "worry", "apology"}  # 触发重要事件快速修复的类型

    def update_affinity(self, user_id: str, valence_shift: float, interaction_type: str | None = None):
        ud = self._user_data.get(user_id)
        if not ud:
            ud = {"familiarity": 0.0, "trust": 0.5, "fun": 0.5, "importance": 0.0, "last_ts": time.time()}
            self._user_data[user_id] = ud

        weights = INTERACTION_TYPE_WEIGHTS.get(interaction_type or "default", INTERACTION_TYPE_WEIGHTS["default"])
        intensity = abs(valence_shift - 0.5) * 2

        # 机制1：时间自然漂移向中性
        idle_hours = (time.time() - ud.get("last_ts", time.time())) / 3600
        if idle_hours > 1:
            drift = min(self._DRIFT_RATE * idle_hours, 0.05)
            for dim, target in self._DRIFT_TARGETS.items():
                current = ud.get(dim, target)
                ud[dim] = current + (target - current) * drift

        ud["familiarity"] = min(1.0, ud["familiarity"] + 0.005)

        # 机制2：连续正面互动加速
        history = self._valence_history.get(user_id, [])
        recent_positive = sum(1 for v in history[-self._STREAK_THRESHOLD:] if v > 0.55)
        streak_boost = self._STREAK_BOOST if (valence_shift > 0.55 and recent_positive >= self._STREAK_THRESHOLD - 1) else 1.0

        if valence_shift > 0.55:
            ud["trust"] = min(1.0, ud["trust"] + intensity * 0.05 * weights["trust"] * streak_boost)
        elif valence_shift < 0.45:
            ud["trust"] = max(0.0, ud["trust"] - intensity * 0.08 * weights["trust"])

        if self._emotion["arousal"] > 0.6 or interaction_type in ("tease", "share"):
            ud["fun"] = min(1.0, ud["fun"] + intensity * 0.03 * weights["fun"] * streak_boost)
        else:
            ud["fun"] = ud["fun"] * 0.998 + 0.5 * 0.002

        if valence_shift > 0.55:
            ud["importance"] = min(1.0, ud["importance"] + intensity * 0.02 * weights["importance"])

        # 机制3：重要正面事件直接拉分
        if interaction_type in self._BIG_MOMENT_TYPES and valence_shift > 0.6 and intensity > 0.3:
            ud["trust"] = min(1.0, ud["trust"] + 0.05)
            ud["importance"] = min(1.0, ud["importance"] + 0.03)

        ud["last_ts"] = time.time()

        history.append(valence_shift)
        self._valence_history[user_id] = history[-self._WARMTH_WINDOW:]

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

    def record_interaction(self, user_id: str):
        now = time.time()
        ts_list = self._recent_interactions.get(user_id, [])
        cutoff = now - self._boredom_window
        ts_list = [t for t in ts_list if t > cutoff]
        ts_list.append(now)
        self._recent_interactions[user_id] = ts_list

    def get_boredom(self, user_id: str) -> float:
        now = time.time()
        cutoff = now - self._boredom_window
        ts_list = self._recent_interactions.get(user_id, [])
        count = sum(1 for t in ts_list if t > cutoff)
        if count <= self._boredom_threshold:
            return 0.0
        return min(1.0, (count - self._boredom_threshold) / self._boredom_threshold)

    def get_warmth(self, user_id: str) -> float | None:
        history = self._valence_history.get(user_id, [])
        if len(history) < 3:
            return None
        return sum(history) / len(history)

    def build_status_prompt(self, user_id: str) -> str:
        composite = self.get_composite(user_id)
        level = self.get_level(user_id)
        ud = self.get_user_data(user_id)
        v = self._emotion["valence"]
        a = self._emotion["arousal"]
        vuln = self._emotion["vulnerability"]

        ud = self._safe_user_data(user_id)
        familiarity = ud.get("familiarity", 0)

        if composite >= 76:
            if v < 0.4:
                cross = "语气比平时硬，话少，不想解释——但不会无视对方，也不会说错的话。"
            elif v >= 0.6 and a >= 0.6:
                cross = "语气比平时软，会认真听对方说话，偶尔说出比平时更直接的话再假装没事。"
            elif v >= 0.6:
                cross = "比平时放松，话会多一点，不那么好胜。"
            else:
                cross = "嘴上还是会嘴硬，但会记住对方说的话，认真回应。"
        elif composite >= 61:
            if v < 0.4:
                cross = "话少，不想多说，但不会冷漠或无视对方。"
            elif v >= 0.6:
                cross = "比平时随意，愿意多聊几句。"
            else:
                cross = "正常回应，不刻意表现亲近也不疏远。"
        elif composite >= 46:
            if v < 0.4:
                cross = "回应简短，没精力特别热情，但不会故意冷淡。"
            else:
                cross = "随口聊聊，没有特别的态度。"
        elif composite >= 31:
            if familiarity < 0.1:
                cross = "不太认识，随口应几句，带点好奇。"
            else:
                cross = "聊过几次但没什么特别印象，正常回应就好。"
        elif composite >= 16:
            if familiarity < 0.1:
                cross = "不认识这个人，先看看对方说什么。"
            else:
                cross = "没留下好印象，不冷漠但也不主动热情。"
        else:
            if familiarity < 0.05:
                cross = "完全陌生，带点观望的态度聊聊。"
            else:
                cross = "印象不太好，懒得费心，但不会主动找麻烦。"

        if vuln > 0.6 and composite >= 61:
            cross += "现在比平时更容易在意对方的反应，但不会主动表现出来。"

        # warmth 优先于 drift；只有 warmth 中性时 drift 才生效
        warmth = self.get_warmth(user_id)
        drift = self._daily_user_drift(user_id)
        if warmth is not None and warmth < 0.4:
            cross += "最近对方反应比较冷，说话时会更注意对方的态度。"
        elif warmth is not None and warmth > 0.65:
            cross += "最近互动顺畅，比平时放松一点。"
        elif warmth is None or 0.4 <= warmth <= 0.65:
            if drift < -25:
                cross += "今天莫名对这个人少了点耐心。"
            elif drift > 25:
                cross += "今天莫名对这个人多了点好感。"

        boredom = self.get_boredom(user_id)
        if boredom >= 0.7:
            cross += "聊了太久，开始有点不专注，想找机会结束。"
        elif boredom >= 0.4:
            cross += "聊了一阵，注意力开始飘。"

        return f"\n【对当前对话者：{level}（{composite:.0f}/100）】{cross}"

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
