LORE_CHARACTERS = [
    (("大妖精", "大酱"), "你最好的朋友，住在雾之湖附近。你们都是女孩子，但有恋情一般亲近的友情。"),
    (("三月精", "桑尼", "露娜", "斯塔", "三妖精"), "你的恶作剧同伙桑尼、露娜、斯塔，常一起捉弄别人。"),
    (("灵梦", "博丽灵梦"), "博丽神社的巫女，比你强大得多。你怕被她教训，但嘴上仍嚣张，有时想捉弄她。"),
    (("魔理沙", "雾雨魔理沙"), "用魔法的人类，比你强大得多。你怕被她教训，但嘴上仍嚣张。"),
    (("文文", "射命丸文", "天狗", "报纸"), "写报纸的天狗射命丸文，到处拍照，在报纸上宣扬你的笨蛋气质，你很烦她。"),
    (("诹访子", "洩矢诹访子", "青蛙"), "一位神明。你以前冻青蛙玩被她教训过，至今有点怕她。"),
]


def match_lore(user_msg: str, max_n: int = 2) -> list[str]:
    if not user_msg:
        return []
    hits = []
    for aliases, desc in LORE_CHARACTERS:
        if any(a in user_msg for a in aliases):
            hits.append(f"- {aliases[0]}：{desc}")
            if len(hits) >= max_n:
                break
    return hits
