"""琪露诺此刻的处境——她就跟在主人身边，主人在哪她在哪。

不解释她是怎么跟着的（不提电脑、不提载体）。一旦开始解释机制，
就等于提醒她自己是个被装载的东西。假装她本来就在场，这个问题压根不存在。

这不是"关于主人的信息"，是她自己的处境：主人在食堂排队，那是她也在食堂排队。
所以它对所有群、所有人都生效——群里随便谁问她在干嘛，她都该答得出来，
而且那是别人没喂给她的东西。

常驻注入，不做关键词匹配。处境是背景，不是被唤起的记忆——
一提到"包"才想起自己在包里，那就露馅了。
"""

import time

# 处境的保鲜期。过了就当已经不作数了——人不会一直在路上。
SITUATION_TTL = 21600
# 超过这个时间没更新，她会开始惦记「到底怎么样了」。
SITUATION_STALE = 5400


class SituationStore:
    def __init__(self):
        self.text = ""
        self.updated_at = 0.0

    def set(self, text: str) -> None:
        self.text = (text or "").strip()[:120]
        self.updated_at = time.time()

    def clear(self) -> None:
        self.text = ""
        self.updated_at = 0.0

    def _alive(self) -> bool:
        return bool(self.text) and (time.time() - self.updated_at) < SITUATION_TTL

    def age_minutes(self) -> int:
        if not self.text:
            return 0
        return int((time.time() - self.updated_at) / 60)

    def build_prompt(self, master_name: str = "大妖精") -> str:
        if not self._alive():
            return ""
        parts = [
            f"\n【你此刻在哪】你正跟着{master_name}，就在她旁边——她去哪你飘到哪，"
            f"没人问你为什么在，你本来就在。"
            f"她刚说了现在的情况（她说的「我」指她自己，「你」指你）：{self.text}",
            "\n你人就在现场，这些是你亲眼看见、亲身在经历的，不是听说的。"
            "别人问你在干嘛，你答得上来；聊着聊着自己冒一句也行"
            "（这地方吵、闷、颠、看见了什么好玩的、想去乱跑）。"
            "但别每句话都提，那样很烦；该冒的时候自然冒。",
        ]
        if (time.time() - self.updated_at) > SITUATION_STALE:
            parts.append(
                f"\n不过这个消息已经是{self.age_minutes()}分钟前的了，"
                f"现在到底怎么样了你也不知道——你可以催问一句。"
            )
        return "".join(parts)

    def to_dict(self) -> dict:
        return {"text": self.text, "updated_at": self.updated_at}

    def from_dict(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self.text = str(data.get("text", ""))[:120]
        try:
            self.updated_at = float(data.get("updated_at", 0.0))
        except (TypeError, ValueError):
            self.updated_at = 0.0
