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
    bot = getattr(event, "bot", None) or getattr(self, "_cached_bot", None)
    if not bot or not keyword:
        return None
    try:
        groups = await bot.call_action("get_group_list")
    except Exception:
        return None
    kw = keyword.strip().lower()
    for g in groups or []:
        gid = str(g.get("group_id", ""))
        name = g.get("group_name", "") or ""
        if kw == gid or kw in name.lower():
            return gid, name
    return None


_speak_cooldown: dict = {}  # sender_id -> 上次跨群说话时间戳


async def speak_in_group(self, event, group: str, words: str) -> str:
    """去指定群说话。申桐无限制；其他人有频率限制（防刷屏）。"""
    import time
    event = _real_event(event)
    try:
        from .local_config import MASTER_ID
    except ImportError:
        MASTER_ID = ""
    sender_id = str(event.get_sender_id())
    # 申桐无限制；其他人 60 秒内只能让她跨群说一次
    if not MASTER_ID or sender_id != MASTER_ID:
        last = _speak_cooldown.get(sender_id, 0)
        if time.time() - last < 60:
            return "刚去别的群说过话啦，本天才才不当你的传话筒一直跑来跑去呢！"
        _speak_cooldown[sender_id] = time.time()
    hit = await _find_group(self, event, group)
    if not hit:
        return f"我没找到「{group}」这个群，要么不在那群，要么名字记错了。"
    gid, name = hit
    platform = event.unified_msg_origin.split(":", 1)[0]
    session = f"{platform}:GroupMessage:{gid}"
    try:
        await self.context.send_message(session, MessageChain().message(words))
    except Exception as e:
        logger.debug(f"[qq_actions] speak_in_group 失败: {e}")
        return f"想去「{name}」说话，但没发出去。"
    return f"已经去「{name}」群里说了：{words}"


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


_dm_cooldown: dict = {}  # sender_id -> 上次给别人发私聊的时间戳


async def message_friend(self, event, target: str, words: str) -> str:
    """给某个好友发私聊。申桐无限制；其他人有频率限制。"""
    import time
    event = _real_event(event)
    try:
        from .local_config import MASTER_ID
    except ImportError:
        MASTER_ID = ""
    sender_id = str(event.get_sender_id())
    if not MASTER_ID or sender_id != MASTER_ID:
        last = _dm_cooldown.get(sender_id, 0)
        if time.time() - last < 60:
            return "刚给别人发过私聊啦，本天才才不一直当你的信使呢！"
        _dm_cooldown[sender_id] = time.time()
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
