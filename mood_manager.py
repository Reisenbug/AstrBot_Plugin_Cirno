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
    FEELING_IDLE_GRACE,
    FEELING_IDLE_HALF_LIFE,
    FEELINGS,
    MOOD_MAX_DURATION,
    MOOD_MIN_DURATION,
)


class CirnoMoodManager:
    def __init__(self):
        self.mood = self._weighted_pick()
        self.mood_entered_at = time.time()
        self.mood_until = time.time() + self._roll_duration()
        # 她自己填的：此刻为什么是这个状态、心里挂着什么事
        self.note = ""
        self.feeling_charges = {}
        self.feeling_updated_at = time.time()

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

    def mark_feeling(self, feeling: str, intensity: str = "mild") -> None:
        """这轮对话改变情绪；中性对话冲淡情绪，久未互动才按时间淡化。"""
        if feeling not in FEELINGS and feeling != "neutral":
            return
        now = time.time()
        charges = self._effective_charges(now)
        fade = (0.6 if intensity == "strong" else 0.85) if feeling == "neutral" else 0.75
        self.feeling_charges = {key: value * fade for key, value in charges.items() if value * fade >= 0.1}
        if feeling in FEELINGS:
            previous = charges.get(feeling, 0.0)
            self.feeling_charges[feeling] = 1.0 if intensity == "strong" else min(1.0, previous + 0.4)
        self.feeling_updated_at = now

    def _effective_charges(self, now: float | None = None) -> dict[str, float]:
        now = time.time() if now is None else now
        idle = max(0.0, now - self.feeling_updated_at - FEELING_IDLE_GRACE)
        fade = 0.5 ** (idle / FEELING_IDLE_HALF_LIFE)
        return {key: value * fade for key, value in self.feeling_charges.items()}

    def _active_feeling(self) -> tuple[str, str]:
        charges = self._effective_charges()
        if not charges:
            return "", ""
        feeling = max(charges, key=charges.get)
        charge = charges[feeling]
        return (feeling, "strong" if charge >= 0.75 else "mild") if charge >= 0.2 else ("", "")

    def build_seed_question(self) -> str:
        """让她自己填此刻状态的引子。"""
        m = CIRNO_MOODS[self.mood]
        return (
            f"你现在的状态是「{m['label']}」。{m['seed']}？"
            "用一句话说，20字以内，就说这件具体的事，别解释、别说'我现在很xx'。"
        )

    def get_prompt_injection(self, test_mood: str | None = None, test_feeling: str | None = None) -> str:
        m = CIRNO_MOODS[test_mood or self.mood]
        parts = [f"\n【你现在的状态：{m['label']}】{m['style']}"]
        if self.note and test_mood is None:
            parts.append(
                f"\n此刻你心里挂着这件事：{self.note}"
                "\n这是你自己的事，对方并不知道。只有自然接得上当前话题时才提。"
            )
        charges = self._effective_charges()
        if test_feeling is not None:
            charges = {test_feeling: 1.0} if test_feeling in FEELINGS else {}
        active = sorted(charges.items(), key=lambda item: item[1], reverse=True)
        if active and active[0][1] >= 0.2:
            visible = [active[0]]
            if len(active) > 1 and active[1][1] >= max(0.3, active[0][1] * 0.5):
                visible.append(active[1])
            labels = "，也有点".join(FEELINGS[key][0] for key, _ in visible)
            styles = "".join(FEELINGS[key][2] for key, _ in visible)
            strength = "很" if active[0][1] >= 0.75 else "有点"
            parts.append(f"\n【现在的心情：{strength}{labels}】{styles}照常回应眼前的事，别编造情绪的具体缘由，也不用每句都报心情。")
        elif test_feeling == "neutral":
            parts.append("\n【现在的心情：平静】照眼前的事自然回应。")
        return "".join(parts)

    def get_debug_info(self, test_mood: str | None = None, test_feeling: str | None = None) -> dict:
        mood = test_mood or self.mood
        m = CIRNO_MOODS[mood]
        feeling, intensity = self._active_feeling()
        if test_feeling is not None:
            feeling, intensity = test_feeling, "strong"
        return {
            "mood": mood,
            "mood_label": m["label"],
            "note": "" if test_mood else self.note,
            "remain_hours": round(max(0.0, self.mood_until - time.time()) / 3600, 1),
            "feeling": feeling or "none",
            "feeling_intensity": intensity or "none",
        }

    def to_dict(self) -> dict:
        return {
            "mood": self.mood,
            "mood_entered_at": self.mood_entered_at,
            "mood_until": self.mood_until,
            "note": self.note,
            "feeling_charges": self.feeling_charges,
            "feeling_updated_at": self.feeling_updated_at,
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
        self.feeling_charges = {}
        if "feeling_charges" in data:
            try:
                self.feeling_charges = {
                    key: max(0.0, min(1.0, float(value)))
                    for key, value in data["feeling_charges"].items() if key in FEELINGS
                }
                self.feeling_updated_at = float(data.get("feeling_updated_at", time.time()))
            except (AttributeError, TypeError, ValueError):
                self.feeling_charges = {}
                self.feeling_updated_at = time.time()
        elif "feeling_charge" in data:
            try:
                charge = max(-1.0, min(1.0, float(data["feeling_charge"])))
                if charge:
                    self.feeling_charges = {"joy" if charge > 0 else "anger": abs(charge)}
                self.feeling_updated_at = float(data.get("feeling_updated_at", time.time()))
            except (TypeError, ValueError):
                self.feeling_charges = {}
                self.feeling_updated_at = time.time()
        else:
            feeling = data.get("feeling", "")
            try:
                active = float(data.get("feeling_until", 0.0)) > time.time()
            except (TypeError, ValueError):
                active = False
            if active and feeling in ("positive", "negative"):
                strength = 1.0 if data.get("feeling_intensity", "strong") == "strong" else 0.4
                self.feeling_charges = {"joy" if feeling == "positive" else "anger": strength}
            self.feeling_updated_at = time.time()
