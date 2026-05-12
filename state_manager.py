import random
import time
from datetime import datetime

from astrbot.api import logger

from .cirno_states import CIRNO_STATES, SEASON_MODIFIERS


def _get_season() -> str:
    month = datetime.now().month
    if month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    elif month in (9, 10, 11):
        return "autumn"
    else:
        return "winter"


class CirnoStateManager:
    def __init__(
        self,
        *,
        min_state_duration: int = 1800,
        transition_rate: float = 0.05,
        max_transition_chance: float = 0.3,
        proactive_cooldown: int = 2700,
        proactive_base_chance: float = 0.15,
        enable_season: bool = True,
    ):
        self.current_state = next(iter(CIRNO_STATES))
        self.state_entered_at = time.time()
        self.last_proactive_msg = 0.0
        self.min_state_duration = min_state_duration
        self.transition_rate = transition_rate
        self.max_transition_chance = max_transition_chance
        self.proactive_cooldown = proactive_cooldown
        self.proactive_base_chance = proactive_base_chance
        self.enable_season = enable_season
        self.ignored_count = 0
        self.silent = False

    def maybe_transition(self) -> bool:
        now = time.time()
        if now - self.state_entered_at < self.min_state_duration:
            return False

        hours_in_state = (now - self.state_entered_at) / 3600
        transition_chance = min(
            self.max_transition_chance,
            self.transition_rate * hours_in_state,
        )

        if random.random() < transition_chance:
            old_state = self.current_state
            old_label = CIRNO_STATES[old_state]["label"]
            self._pick_new_state()
            if self.current_state != old_state:
                new_label = CIRNO_STATES[self.current_state]["label"]
                logger.info(
                    f"[琪露诺状态切换] {old_label}({old_state}) -> {new_label}({self.current_state})"
                    f" | 已持续 {hours_in_state:.1f}h, 切换概率 {transition_chance:.2%}"
                )
                return True
        return False

    @staticmethod
    def _is_active_hour(hours: tuple, hour: int) -> bool:
        if len(hours) == 2:
            start, end = hours
            if start < end:
                return start <= hour < end
            return hour >= start or hour < end
        return any(abs(hour - h) <= 1 for h in hours)

    def _pick_new_state(self):
        now = datetime.now()
        hour = now.hour
        is_weekday = now.weekday() < 5
        modifier = {}
        if self.enable_season:
            season = _get_season()
            modifier = SEASON_MODIFIERS.get(season, {})
        category_mult = modifier.get("category_weight_multiplier", {})
        state_override = modifier.get("state_weight_override", {})

        candidates = {}
        for state_id, state in CIRNO_STATES.items():
            if state_id == self.current_state:
                continue
            if not self._is_active_hour(state["active_hours"], hour):
                continue
            if state.get("weekday_only") and not is_weekday:
                continue
            if state_id in state_override:
                w = state_override[state_id]
            else:
                w = state["weight"]
                cat = state.get("category")
                if cat and cat in category_mult:
                    w *= category_mult[cat]
            candidates[state_id] = w

        if not candidates:
            return

        total = sum(candidates.values())
        if total <= 0:
            return
        r = random.random() * total
        cumulative = 0.0
        for state_id, weight in candidates.items():
            cumulative += weight
            if r <= cumulative:
                self.current_state = state_id
                self.state_entered_at = time.time()
                return

    LONELY_TOPICS = [
        "怎么没人陪我玩...",
        "咱无聊了...",
        "哼，你们都不理我，最强的我才不在乎呢……",
        "喂——有人吗——",
    ]

    def on_user_interaction(self):
        self.ignored_count = 0
        self.silent = False

    def should_speak_proactively(self) -> str | None:
        if self.silent:
            return None

        state = CIRNO_STATES[self.current_state]
        if not state["proactive_topics"]:
            return None

        now = time.time()
        if now - self.last_proactive_msg < self.proactive_cooldown:
            return None

        if random.random() > self.proactive_base_chance:
            return None

        self.ignored_count += 1
        self.last_proactive_msg = now

        if self.ignored_count >= 3:
            self.silent = True
            topic = random.choice(self.LONELY_TOPICS)
            logger.info(f"[琪露诺主动发言] 连续无人回应({self.ignored_count}次)，进入沉默模式，最后说：{topic}")
            return topic

        topic = random.choice(state["proactive_topics"])
        logger.info(f"[琪露诺主动发言] 状态={state['label']}，话题：{topic}")
        return topic

    def get_prompt_injection(self) -> str:
        state = CIRNO_STATES[self.current_state]
        text = f"【当前状态：{state['label']}】{state['prompt_inject']}"

        if self.enable_season:
            season = _get_season()
            modifier = SEASON_MODIFIERS.get(season)
            if modifier and modifier.get("extra_prompt"):
                text += f"\n{modifier['extra_prompt']}"

        return text

    def get_debug_info(self) -> dict:
        state = CIRNO_STATES.get(self.current_state, {"label": "未知"})
        duration = time.time() - self.state_entered_at
        cooldown_left = max(0, self.proactive_cooldown - (time.time() - self.last_proactive_msg))
        return {
            "state_id": self.current_state,
            "state_label": state["label"],
            "duration_hours": int(duration // 3600),
            "duration_minutes": int((duration % 3600) // 60),
            "season": _get_season() if self.enable_season else "disabled",
            "cooldown_minutes": int(cooldown_left // 60),
            "ignored_count": self.ignored_count,
            "silent": self.silent,
        }

    def to_dict(self) -> dict:
        return {
            "current_state": self.current_state,
            "state_entered_at": self.state_entered_at,
            "last_proactive_msg": self.last_proactive_msg,
            "ignored_count": self.ignored_count,
            "silent": self.silent,
        }

    def from_dict(self, data: dict):
        default_state = next(iter(CIRNO_STATES))
        self.current_state = data.get("current_state", default_state)
        try:
            self.state_entered_at = float(data.get("state_entered_at", time.time()))
        except (TypeError, ValueError):
            self.state_entered_at = time.time()
        try:
            self.last_proactive_msg = float(data.get("last_proactive_msg", 0.0))
        except (TypeError, ValueError):
            self.last_proactive_msg = 0.0
        try:
            self.ignored_count = int(data.get("ignored_count", 0))
        except (TypeError, ValueError):
            self.ignored_count = 0
        self.silent = bool(data.get("silent", False))
        if self.current_state not in CIRNO_STATES:
            self.current_state = default_state
            self.state_entered_at = time.time()
