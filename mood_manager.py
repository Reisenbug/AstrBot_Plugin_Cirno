"""管理琪露诺此刻的模式：骨架预定义（cirno_moods），血肉由她自己填。

模式切换时问一次 LLM「你现在为什么是这个状态、心里挂着什么」，
把答案存下来当作她自己的东西。之后每次说话都带着它——
这样她嘴里就有了对方没喂给她的内容。
"""

import random
import time

from astrbot.api import logger

from .cirno_moods import (
    CIRNO_MOODS,
    FEELING_DECAY,
    MOOD_MAX_DURATION,
    MOOD_MIN_DURATION,
    NEGATIVE_FEELING_STYLE,
    POSITIVE_FEELING_STYLE,
)


class CirnoMoodManager:
    def __init__(self):
        self.mood = self._weighted_pick()
        self.mood_entered_at = time.time()
        self.mood_until = time.time() + self._roll_duration()
        # 她自己填的：此刻为什么是这个状态、心里挂着什么事
        self.note = ""
        self.feeling = ""
        self.feeling_until = 0.0

    @staticmethod
    def _weighted_pick(exclude: str | None = None) -> str:
        pool = {k: v["weight"] for k, v in CIRNO_MOODS.items() if k != exclude}
        total = sum(pool.values())
        r = random.random() * total
        acc = 0.0
        for mood, w in pool.items():
            acc += w
            if r <= acc:
                return mood
        return next(iter(pool))

    @staticmethod
    def _roll_duration() -> float:
        return random.uniform(MOOD_MIN_DURATION, MOOD_MAX_DURATION)

    def is_expired(self) -> bool:
        return time.time() >= self.mood_until

    def rotate(self) -> str:
        """换一个模式。返回新模式 id；调用方负责让她自己填 note。"""
        old = self.mood
        self.mood = self._weighted_pick(exclude=old)
        self.mood_entered_at = time.time()
        self.mood_until = time.time() + self._roll_duration()
        self.note = ""
        logger.info(
            f"[琪露诺模式切换] {CIRNO_MOODS[old]['label']} -> {CIRNO_MOODS[self.mood]['label']}"
            f" | 将持续 {(self.mood_until - time.time()) / 3600:.1f}h"
        )
        return self.mood

    def set_note(self, note: str) -> None:
        self.note = (note or "").strip()[:60]
        if self.note:
            logger.info(f"[琪露诺模式内容] {CIRNO_MOODS[self.mood]['label']}：{self.note}")

    def mark_feeling(self, sentiment: str, intensity: str = "mild") -> None:
        """对话结束时把 <inner> 的情绪叠上来。只有 strong 才留痕，避免每句话都在改状态。"""
        if sentiment not in FEELING_DECAY or intensity != "strong":
            return
        self.feeling = sentiment
        self.feeling_until = time.time() + FEELING_DECAY[sentiment]

    def _active_feeling(self) -> str:
        if self.feeling and time.time() < self.feeling_until:
            return self.feeling
        return ""

    def build_seed_question(self) -> str:
        """让她自己填此刻状态的引子。"""
        m = CIRNO_MOODS[self.mood]
        return (
            f"你现在的状态是「{m['label']}」。{m['seed']}？"
            "用一句话说，20字以内，就说这件具体的事，别解释、别说'我现在很xx'。"
        )

    def get_prompt_injection(self) -> str:
        m = CIRNO_MOODS[self.mood]
        parts = [f"\n【你现在的状态：{m['label']}】{m['style']}"]
        if self.note:
            parts.append(
                f"\n此刻你心里挂着这件事：{self.note}"
                "\n这是你自己的事，对方并不知道。聊着聊着可以自己冒出来——"
                "不用等人问，也不用非要跟当前话题有关。"
            )
        feeling = self._active_feeling()
        if feeling == "negative":
            parts.append(f"\n{NEGATIVE_FEELING_STYLE}")
        elif feeling == "positive":
            parts.append(f"\n{POSITIVE_FEELING_STYLE}")
        return "".join(parts)

    def get_debug_info(self) -> dict:
        m = CIRNO_MOODS[self.mood]
        return {
            "mood": self.mood,
            "mood_label": m["label"],
            "note": self.note,
            "remain_hours": round(max(0.0, self.mood_until - time.time()) / 3600, 1),
            "feeling": self._active_feeling() or "none",
        }

    def to_dict(self) -> dict:
        return {
            "mood": self.mood,
            "mood_entered_at": self.mood_entered_at,
            "mood_until": self.mood_until,
            "note": self.note,
            "feeling": self.feeling,
            "feeling_until": self.feeling_until,
        }

    def from_dict(self, data: dict) -> None:
        mood = data.get("mood")
        self.mood = mood if mood in CIRNO_MOODS else self._weighted_pick()
        try:
            self.mood_entered_at = float(data.get("mood_entered_at", time.time()))
            self.mood_until = float(data.get("mood_until", 0.0))
        except (TypeError, ValueError):
            self.mood_entered_at = time.time()
            self.mood_until = time.time() + self._roll_duration()
        self.note = str(data.get("note", ""))[:60]
        self.feeling = str(data.get("feeling", ""))
        try:
            self.feeling_until = float(data.get("feeling_until", 0.0))
        except (TypeError, ValueError):
            self.feeling_until = 0.0
