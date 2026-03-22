import os
import random

MOOD_KEYWORDS: list[tuple[str, list[str]]] = [
    ("smug", ["最强", "当然", "哼哼", "简单", "厉害", "天才", "完美"]),
    ("angry", ["笨蛋", "可恶", "哼", "生气", "讨厌", "烦", "不许"]),
    ("confused", ["什么", "不懂", "为什么", "奇怪", "⑨", "不明白"]),
    ("shy", ["才不是", "哎呀", "别说了", "讨厌啦", "脸红", "呜"]),
    ("sad", ["难过", "呜呜", "不理我", "孤单", "寂寞", "委屈"]),
    ("sleepy", ["困", "睡", "累", "不想动", "迷糊", "好懒"]),
    ("excited", ["好玩", "冒险", "出发", "探险", "发现", "找到了"]),
    ("happy", ["开心", "高兴", "好吃", "喜欢", "太好了", "哈哈", "嘻嘻"]),
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CATEGORIES = [mood for mood, _ in MOOD_KEYWORDS]


class MemeSelector:
    def __init__(self, meme_dir: str, probability: float = 0.35):
        self.probability = max(0.0, min(1.0, probability))
        self.meme_dir = os.path.join(meme_dir, "memes")
        for cat in CATEGORIES:
            os.makedirs(os.path.join(self.meme_dir, cat), exist_ok=True)

    def select(self, bot_reply: str) -> str | None:
        if random.random() >= self.probability:
            return None
        mood = self._detect_mood(bot_reply)
        if not mood:
            return None
        return self._pick_image(mood)

    def _detect_mood(self, text: str) -> str | None:
        for mood, keywords in MOOD_KEYWORDS:
            for kw in keywords:
                if kw in text:
                    return mood
        return None

    def _pick_image(self, category: str) -> str | None:
        cat_dir = os.path.join(self.meme_dir, category)
        if not os.path.isdir(cat_dir):
            return None
        try:
            files = [
                f for f in os.listdir(cat_dir)
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS
            ]
        except OSError:
            return None
        if not files:
            return None
        return os.path.join(cat_dir, random.choice(files))

    def get_stats(self) -> dict[str, int]:
        stats = {}
        for cat in CATEGORIES:
            cat_dir = os.path.join(self.meme_dir, cat)
            if not os.path.isdir(cat_dir):
                stats[cat] = 0
                continue
            try:
                stats[cat] = sum(
                    1 for f in os.listdir(cat_dir)
                    if os.path.splitext(f)[1].lower() in IMAGE_EXTS
                )
            except OSError:
                stats[cat] = 0
        return stats
