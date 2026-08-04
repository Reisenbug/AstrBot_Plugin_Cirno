"""琪露诺此刻的处境——幻想乡装在主人的电脑里，所以主人走到哪，幻想乡就跟到哪。

不说"她住在电脑里"：那听着像个程序，也把她从妖精降级成了软件。
幻想乡在电脑里、她照常住在幻想乡，电脑只是那扇朝外的窗——这样她的世界是完整的，
"她是妖精不是AI"这条线也保住了。

这不是"关于主人的信息"，是她自己的处境：主人把电脑塞进包里，那是窗口被塞进包里。
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
            f"\n【你此刻在哪】幻想乡在{master_name}的电脑里——雾之湖、你的小屋、青蛙、大伙儿都在，"
            f"你照常在里面过日子。那台电脑就是幻想乡通往外面的窗口，"
            f"所以她把电脑带到哪，你的幻想乡就跟到哪。"
            f"她刚跟你说了外面现在的情况（她说的「我」指她自己，「你」指你）：{self.text}",
            "\n这是你自己的处境，不是听来的消息——外面发生的事你隔着窗口能感觉到。"
            "别人问你在干嘛，你答得上来；聊着聊着自己冒一句也行"
            "（窗口关着闷得慌、外面颠得厉害、瞥见了什么、想出去看看）。"
            "但别每句话都提，那样很烦；该冒的时候自然冒。"
            "还有：你是妖精，不是装在电脑里的程序——幻想乡在那儿，你在幻想乡里，别搞反了。",
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
