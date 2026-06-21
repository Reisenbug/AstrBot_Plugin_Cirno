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


async def speak_in_group(self, event, group: str, words: str) -> str:
    """去指定群说话。仅限申桐触发、且只能往琪露诺自己在的群发。"""
    event = _real_event(event)
    try:
        from .local_config import MASTER_ID
    except ImportError:
        MASTER_ID = ""
    sender_id = str(event.get_sender_id())
    if not MASTER_ID or sender_id != MASTER_ID:
        return "只有大妖精能让我去别的群说话，别人可不行。"
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
