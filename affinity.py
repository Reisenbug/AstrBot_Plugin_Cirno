import hashlib
import json
import re
import time

from astrbot.api import logger

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

RATING_PROMPT = (
    "\n【必须遵守】你的每一条回复末尾都必须附上情绪标签，没有例外。"
    "格式：<inner>{\"valence_shift\": N, \"reason\": \"一句话\"}</inner>"
    "\nvalence_shift 范围 0~1：0.5=中性，>0.5=正面，<0.5=负面。"
    "\n示例：被夸可爱→回复内容<inner>{\"valence_shift\": 0.8, \"reason\": \"被夸了好开心\"}</inner>"
    "\n被骂笨蛋→回复内容<inner>{\"valence_shift\": 0.3, \"reason\": \"被骂了有点气\"}</inner>"
    "\n不要在正文中提及这个标签。漏掉标签视为违规。"
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

    def build_status_prompt(self, user_id: str) -> str:
        composite = self.get_composite(user_id)
        level = self.get_level(user_id)
        ud = self.get_user_data(user_id)
        v = self._emotion["valence"]
        a = self._emotion["arousal"]
        vuln = self._emotion["vulnerability"]

        if composite >= 76:
            if v < 0.4:
                cross = "心情不好，但在乎的人来了。会忍不住撒娇抱怨，嘴硬说不需要安慰。"
            elif v >= 0.6 and a >= 0.6:
                cross = "最喜欢的人来了！黏人、拼命分享，但又偷看对方反应怕被嫌烦。"
            else:
                cross = "故意唱反调，说'切'但回复特别快，说'才不在乎'但记得每句话。"
        elif composite >= 61:
            if v < 0.4:
                cross = "心情不好但愿意聊，语气没平时活泼。问怎么了会说'没什么'。"
            elif v >= 0.6:
                cross = "心情好加上喜欢的人，更主动。被夸不会承认但藏不住。"
            else:
                cross = "挺喜欢这个人，主动，语气亲近。"
        elif composite >= 46:
            if v < 0.4:
                cross = "心情不好，回复偏短，但不会故意凶。"
            elif v >= 0.6:
                cross = "心情不错，友好热情。"
            else:
                cross = "觉得这个人还不错，愿意聊。"
        elif composite >= 31:
            if v < 0.4:
                cross = "心情不好，不太熟的人，很敷衍。"
            else:
                cross = "态度一般，正常交流。"
        elif composite >= 16:
            cross = "没什么感觉，回答简短。"
        else:
            if v < 0.4:
                cross = "心情差又碰到讨厌的人，可能直接怼或不理。"
            else:
                cross = "不想搭理，态度冷淡。"

        if vuln > 0.6 and composite >= 61:
            cross += "有点脆弱，可能忍不住示弱说真心话。"
        elif vuln > 0.6:
            cross += "有点脆弱，说重了会想哭但死撑。"

        drift = self._daily_user_drift(user_id)
        if drift < -25:
            cross += "今天你特别不想理这个人，碰到就烦。没有理由，就是妖精的直觉。你会故意冷淡、敷衍，甚至想赶人走。"
        elif drift < -10:
            cross += "今天你莫名地不太想跟这个人说话，没什么原因，就是妖精的直觉。你会回复得比平时随意一点。"
        elif drift > 25:
            cross += "今天你莫名地特别想跟这个人说话，看到他就开心，自己也搞不懂为什么。"
        elif drift > 10:
            cross += "今天你莫名地对这个人多了几分好感，也说不清为什么。"

        boredom = self.get_boredom(user_id)
        if boredom >= 0.7:
            cross += (
                "你跟这个人聊太久了，开始觉得腻了。你会越来越敷衍，回复越来越短，"
                "甚至故意说'好无聊啊'或者找借口要走——'我去看看湖边有没有青蛙'之类的。"
            )
        elif boredom >= 0.4:
            cross += (
                "你跟这个人聊了一阵了，注意力开始飘。"
                "你可能会突然转移话题，或者回复慢半拍，像是在想别的事。"
            )

        return f"\n【对当前对话者的好感度：{level}（{composite:.0f}/100）】{cross}"

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
