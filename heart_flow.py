import math
import time

from .recall_memory import extract_keywords


class HeartFlow:
    """Per-session topic interest tracker.

    Interest rises when the topic overlaps with recent messages and decays
    over time. Used to modulate random-reply probability: low interest →
    bot stays quiet even if the random roll would have triggered a reply.
    """

    DECAY_HALF_LIFE = 300  # seconds until interest halves
    KEYWORD_BOOST = 0.15   # per overlapping keyword
    MAX_INTEREST = 1.0
    MIN_INTEREST = 0.0
    HISTORY_SIZE = 6       # recent messages to track keywords from

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def _get(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "interest": 0.5,
                "updated_at": time.time(),
                "recent_kw": [],
            }
        return self._sessions[session_id]

    def _decay(self, state: dict) -> float:
        elapsed = time.time() - state["updated_at"]
        factor = math.exp(-elapsed * math.log(2) / self.DECAY_HALF_LIFE)
        return max(self.MIN_INTEREST, state["interest"] * factor)

    def update(self, session_id: str, message: str):
        state = self._get(session_id)
        state["interest"] = self._decay(state)

        kw = extract_keywords(message)
        if kw:
            recent_kw_set = set(state["recent_kw"])
            overlap = len(set(kw) & recent_kw_set)
            boost = min(overlap * self.KEYWORD_BOOST, 0.4)
            state["interest"] = min(self.MAX_INTEREST, state["interest"] + boost)
            state["recent_kw"] = (state["recent_kw"] + kw)[-self.HISTORY_SIZE * 5:]

        state["updated_at"] = time.time()

    def get_interest(self, session_id: str) -> float:
        state = self._get(session_id)
        return self._decay(state)

    def should_engage(self, session_id: str, base_chance: float) -> bool:
        """Return True if bot should engage, scaling base_chance by interest."""
        interest = self.get_interest(session_id)
        adjusted = base_chance * (0.3 + 0.7 * interest)
        import random
        return random.random() < adjusted

    def on_bot_reply(self, session_id: str):
        """Boost interest when bot actually replies (topic was engaging)."""
        state = self._get(session_id)
        state["interest"] = self._decay(state)
        state["interest"] = min(self.MAX_INTEREST, state["interest"] + 0.1)
        state["updated_at"] = time.time()

    def get_debug(self, session_id: str) -> dict:
        return {"interest": round(self.get_interest(session_id), 3)}
