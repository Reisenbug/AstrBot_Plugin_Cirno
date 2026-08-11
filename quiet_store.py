"""让琪露诺对某个人闭嘴一会儿。

她本来没有"不回复"这个选项：收到消息就一定产出回复。另一个 AI bot 跟她对上，
两边各自都在"礼貌回应"，就无限循环，只能人去关插件。

不是整个静音，只掐断跟某一个人的那条线——群里别人照常聊。
限时，到点自己恢复。
"""

import time

DEFAULT_QUIET_SECONDS = 900
MAX_QUIET_SECONDS = 21600


class QuietStore:
    def __init__(self):
        self._until: dict[tuple[str, str], float] = {}

    @staticmethod
    def _key(umo: str, target_qq: str) -> tuple[str, str]:
        return (str(umo), str(target_qq))

    def mute(self, umo: str, target_qq: str, seconds: float = DEFAULT_QUIET_SECONDS) -> int:
        seconds = max(60.0, min(float(seconds), MAX_QUIET_SECONDS))
        self._until[self._key(umo, target_qq)] = time.time() + seconds
        return int(seconds)

    def unmute(self, umo: str, target_qq: str) -> None:
        self._until.pop(self._key(umo, target_qq), None)

    def is_muted(self, umo: str, target_qq: str) -> bool:
        k = self._key(umo, target_qq)
        until = self._until.get(k, 0.0)
        if not until:
            return False
        if time.time() >= until:
            self._until.pop(k, None)
            return False
        return True

    def remain(self, umo: str, target_qq: str) -> int:
        return max(0, int(self._until.get(self._key(umo, target_qq), 0.0) - time.time()))

    def active(self) -> list[tuple[str, str, int]]:
        now = time.time()
        return [(u, q, int(t - now)) for (u, q), t in self._until.items() if t > now]

    def to_dict(self) -> dict:
        now = time.time()
        return {f"{u}\t{q}": t for (u, q), t in self._until.items() if t > now}

    def from_dict(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        now = time.time()
        out: dict[tuple[str, str], float] = {}
        for k, v in data.items():
            if not isinstance(v, (int, float)) or float(v) <= now:
                continue
            parts = str(k).split("\t")
            if len(parts) == 2:
                out[(parts[0], parts[1])] = float(v)
        self._until = out
