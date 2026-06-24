"""QQ 操作工具的实现逻辑。

装饰器+docstring 留在 main.py 的主类里（框架靠主类绑定 handler），
这里只放纯逻辑函数，接收主类实例 self 以访问 self.context / bot。
"""

from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import logger


def _real_event(event):
    """工具被 agent 调用时，event 可能是 ContextWrapper，需解包成真正的 AstrMessageEvent。"""
    if hasattr(event, "get_sender_id"):
        return event
    inner = getattr(event, "context", None)
    if inner is not None and hasattr(inner, "event"):
        return inner.event
    return event


async def list_my_groups(self, event) -> str:
    """返回琪露诺所在的群列表（群名 + 群号）。"""
    event = _real_event(event)
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot:
        return "现在连不上QQ，看不到群。"
    try:
        groups = await bot.call_action("get_group_list")
    except Exception as e:
        logger.debug(f"[qq_actions] get_group_list 失败: {e}")
        return "翻了翻，没看清都在哪些群。"
    if not groups:
        return "好像一个群都没在呢。"
    lines = [f"{g.get('group_name', '?')}（{g.get('group_id')}）" for g in groups[:30]]
    return "我在这些群里：\n" + "\n".join(lines)


async def _find_group(self, event, keyword: str):
    """按群名/群号在群列表里匹配一个群，返回 (group_id, group_name) 或 None。"""
    import re
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot or not keyword:
        return None
    try:
        groups = await bot.call_action("get_group_list")
    except Exception:
        return None
    kw = keyword.strip().lower()
    # LLM 常把"群名（群号）"整段传进来，先从中抽出群号（5位以上数字）优先精确匹配
    nums = re.findall(r"\d{5,}", keyword)
    for g in groups or []:
        gid = str(g.get("group_id", ""))
        if gid in nums:
            return gid, g.get("group_name", "") or ""
    for g in groups or []:
        gid = str(g.get("group_id", ""))
        name = (g.get("group_name", "") or "").lower()
        # 双向子串：输入含群名 或 群名含输入，都算命中
        if kw == gid or kw in name or (name and name in kw):
            return gid, g.get("group_name", "") or ""
    return None


async def _find_member_in(self, event, group_id: str, keyword: str):
    """在指定群（不一定是当前群）里按昵称/群名片/QQ号找人，返回 (qq, 显示名) 或 None。"""
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot or not group_id or not keyword:
        return None
    try:
        members = await bot.call_action("get_group_member_list", group_id=int(group_id))
    except Exception as e:
        logger.debug(f"[qq_actions] 群成员查询失败: {e}")
        return None
    kw = keyword.strip().lower()
    for m in members or []:
        uid = str(m.get("user_id", ""))
        card = (m.get("card", "") or "")
        nick = (m.get("nickname", "") or "")
        if kw == uid or (card and kw in card.lower()) or (nick and kw in nick.lower()):
            return uid, (card or nick or uid)
    return None


async def speak_in_group(self, event, group: str, words: str, at_someone: str = "") -> str:
    """去指定群说话（任何人都能让她去，说不说由人格决定）。
    要 @ 群里某个人就传 at_someone（昵称或QQ号），会自动查群成员解析成真正的@。
    words 里不要自己写 [at:xxx]，那只会变成一串没用的字。"""
    event = _real_event(event)
    hit = await _find_group(self, event, group)
    if not hit:
        return f"我没找到「{group}」这个群，要么不在那群，要么名字记错了。"
    gid, name = hit
    platform = event.unified_msg_origin.split(":", 1)[0]
    session = f"{platform}:GroupMessage:{gid}"
    chain = MessageChain()
    at_note = ""
    if at_someone.strip():
        member = await _find_member_in(self, event, gid, at_someone)
        if not member:
            return f"「{name}」群里没找到「{at_someone}」这个人，没法@她，话也先没发。"
        qq, disp = member
        chain = chain.at(disp, qq)
        chain = chain.message(" " + words)
        at_note = f"，@了{disp}"
    else:
        chain = chain.message(words)
    try:
        await self.context.send_message(session, chain)
    except Exception as e:
        logger.debug(f"[qq_actions] speak_in_group 失败: {e}")
        return f"想去「{name}」说话，但没发出去。"
    return f"已经去「{name}」群里说了{at_note}：{words}"


async def poke(self, event, target: str) -> str:
    """戳一戳当前群里的某个人。"""
    event = _real_event(event)
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    group_id = event.get_group_id()
    if not bot or not group_id:
        return "这儿没法戳人（不在群里）。"
    hit = await self._find_group_member(event, target)
    if not hit:
        return f"群里没找到「{target}」，戳了个空气。"
    qq, name = hit
    try:
        await bot.call_action("group_poke", group_id=int(group_id), user_id=int(qq))
    except Exception as e:
        logger.debug(f"[qq_actions] poke 失败: {e}")
        return f"想戳{name}，但没戳着。"
    return f"戳了戳{name}！"


def _known_people(self):
    """私聊过/有印象的人（来自 core_memory），不依赖QQ好友关系。"""
    cm = getattr(self, "core_memory", None)
    return getattr(cm, "_profiles", {}) if cm else {}


async def list_my_friends(self, event) -> str:
    """返回琪露诺私聊过/认识的人（名字 + QQ号）。"""
    people = _known_people(self)
    if not people:
        return "唔…好像还没跟谁单独说过话呢。"
    lines = [f"{p.get('name', uid)}（{uid}）" for uid, p in list(people.items())[:40]]
    return "我认识这些人：\n" + "\n".join(lines)


async def _find_friend(self, event, keyword: str):
    if not keyword:
        return None
    kw = keyword.strip().lower()
    for uid, p in _known_people(self).items():
        name = p.get("name", "") or ""
        if kw == str(uid) or kw in name.lower():
            return str(uid), (name or str(uid))
    return None


async def message_friend(self, event, target: str, words: str) -> str:
    """给某个好友发私聊（任何人都能让她发，发不发由人格决定）。"""
    event = _real_event(event)
    hit = await _find_friend(self, event, target)
    if not hit:
        return f"我好友里没找到「{target}」，要么不是好友，要么名字记错了。"
    qq, name = hit
    platform = event.unified_msg_origin.split(":", 1)[0]
    session = f"{platform}:FriendMessage:{qq}"
    try:
        await self.context.send_message(session, MessageChain().message(words))
    except Exception as e:
        logger.debug(f"[qq_actions] message_friend 失败: {e}")
        return f"想给{name}发消息，但没发出去。"
    return f"已经私聊跟{name}说了：{words}"


def _seg_to_text(segs) -> str:
    """把 OneBot 消息段数组压成一行可读文本。"""
    if isinstance(segs, str):
        return segs
    parts = []
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        t = s.get("type")
        d = s.get("data", {}) or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            parts.append(f"@{d.get('qq', '')}")
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append("[表情]")
        elif t == "reply":
            continue
        else:
            parts.append(f"[{t}]")
    return "".join(parts).strip()


async def peek_group_chat(self, event, group: str, count: int = 15) -> str:
    """看某个群最近在聊什么（返回最近若干条消息：谁说了啥）。"""
    event = _real_event(event)
    hit = await _find_group(self, event, group)
    if not hit:
        return f"我没找到「{group}」这个群，看不了。"
    gid, name = hit
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot:
        return "现在连不上QQ，看不了群里的消息。"
    count = max(1, min(int(count or 15), 30))
    try:
        res = await bot.call_action("get_group_msg_history", group_id=int(gid), count=count)
    except Exception as e:
        logger.debug(f"[qq_actions] get_group_msg_history 失败: {e}")
        return f"想看看「{name}」群在聊啥，但没翻到记录。"
    msgs = (res or {}).get("messages", []) if isinstance(res, dict) else (res or [])
    lines = []
    for m in msgs:
        sender = m.get("sender", {}) or {}
        who = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", "?"))
        text = _seg_to_text(m.get("message"))
        if text:
            lines.append(f"{who}：{text}")
    if not lines:
        return f"「{name}」群最近没人说话。"
    return f"「{name}」群最近在聊：\n" + "\n".join(lines[-count:])


async def take_back_my_last(self, event, group: str = "") -> str:
    """撤回琪露诺自己在群里刚说的最后一条话（说错了、后悔了）。
    group 留空就撤回当前群里自己最后那条。"""
    event = _real_event(event)
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot:
        return "现在连不上QQ，撤不了。"
    if group.strip():
        hit = await _find_group(self, event, group)
        if not hit:
            return f"我没找到「{group}」这个群。"
        gid = hit[0]
    else:
        gid = event.get_group_id()
        if not gid:
            return "这儿不是群聊，没法撤回。"
    try:
        login = await bot.call_action("get_login_info")
        my_uin = str(login.get("user_id", ""))
        res = await bot.call_action("get_group_msg_history", group_id=int(gid), count=20)
    except Exception as e:
        logger.debug(f"[qq_actions] 撤回前取历史失败: {e}")
        return "想撤回，但翻不到刚才说的话。"
    msgs = (res or {}).get("messages", []) if isinstance(res, dict) else (res or [])
    my_msg_id = None
    for m in msgs:
        if str((m.get("sender", {}) or {}).get("user_id", "")) == my_uin:
            my_msg_id = m.get("message_id")
    if my_msg_id is None:
        return "刚才好像没说什么可撤的。"
    try:
        await bot.call_action("delete_msg", message_id=my_msg_id)
    except Exception as e:
        logger.debug(f"[qq_actions] delete_msg 失败: {e}")
        return "想撤回那句话，但撤不掉（可能太久了）。"
    return "唰——刚才那句话我收回！没说过没说过！"
