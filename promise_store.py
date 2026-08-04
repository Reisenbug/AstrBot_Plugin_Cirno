"""琪露诺答应下来的事：等某个人再出现时，她要做什么。

只做一种形态——「盯着某人，他一冒头就 @ 某人说句话」。这是唯一有人验收的承诺：
你交代完会记得，她不兑现你立刻就发现。她自说自话的flag没人验收，不在这里管。

24小时过期。她是笨蛋妖精，记不了那么久；而且过了一天你自己也忘了。
兑现一次就消费掉，不重复触发——群里刷屏时不会连炸。
"""

import time

PROMISE_TTL = 86400
MAX_PROMISES = 8


class PromiseStore:
    def __init__(self):
        self._items: list[dict] = []

    def add(self, *, watch_qq: str, watch_name: str, notify_qq: str,
            notify_name: str, what: str, umo: str) -> None:
        self._items = [
            p for p in self._items
            if not (p["watch_qq"] == watch_qq and p["umo"] == umo)
        ]
        self._items.append({
            "watch_qq": str(watch_qq),
            "watch_name": watch_name,
            "notify_qq": str(notify_qq),
            "notify_name": notify_name,
            "what": what[:80],
            "umo": umo,
            "created_at": time.time(),
        })
        if len(self._items) > MAX_PROMISES:
            self._items = self._items[-MAX_PROMISES:]

    def _prune(self) -> None:
        cutoff = time.time() - PROMISE_TTL
        self._items = [p for p in self._items if p["created_at"] >= cutoff]

    def take_match(self, speaker_qq: str, umo: str) -> dict | None:
        """说话的人正好是被盯的那个 → 取出这条承诺并消费掉。"""
        self._prune()
        for i, p in enumerate(self._items):
            if p["watch_qq"] == str(speaker_qq) and p["umo"] == umo:
                return self._items.pop(i)
        return None

    def pending(self) -> list[dict]:
        self._prune()
        return list(self._items)

    def to_list(self) -> list[dict]:
        self._prune()
        return list(self._items)

    def from_list(self, data) -> None:
        if not isinstance(data, list):
            return
        self._items = [
            p for p in data
            if isinstance(p, dict) and p.get("watch_qq") and p.get("umo")
        ][-MAX_PROMISES:]
        self._prune()
