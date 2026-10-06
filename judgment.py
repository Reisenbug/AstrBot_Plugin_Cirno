"""她这轮该用什么姿态接话，交给一个只做判断的小模型。

为什么不写进规则：规则是"建议"，她可以不理，这一周反复验证过。判断是外部决定，
写进 prompt 的是结果不是选项。

为什么不让主模型判断：同源的判断和生成会互相污染语域，而且慢。主模型一次 4.8s，
这个 0.8s，还不占它那 10 次/分钟的配额。

@ 她必回，所以这里不判断"要不要开口"，只判断"怎么接"。
"""

import os

from astrbot.api import logger

try:
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        RetryPolicy,
        Score,
        TypeSafeError,
    )
except ImportError:  # SDK 没装就整个功能静默关掉
    AsyncTypeSafeClient = None
    TypeSafeError = Exception


REACTIONS = {
    "随口应一声": "对方没说什么实质内容，应一声就完事，不用找话题也不用接梗",
    "接住原话": "对方认真说了一件事，先接住这件事，不替他补意图",
    "损人打趣": "对方明显在闹，抓住话里的小破绽打趣他，不恶意攻击",
    "揪漏洞": "对方话里有说不通的地方，抓住它追问或拆台",
    "故意曲解": "对方留了玩笑空间，她歪着理解来逗人",
    "起哄拱火": "两个人之间有明确的玩笑，她在旁边添一句",
    "得意炫耀": "对方的话确实给了她显摆的机会，她顺势得意一下",
    "歪理绕人": "用幻想乡的逻辑把对方绕进去，一本正经地胡说",
    "反将一军": "对方想逗她或占便宜，她抓住对方的话调侃回去，反问或拆台，把对方逗得接不住",
    "嘴硬招架": "被戳中了或害羞了，嘴上不认，防守姿态",
    "凑上去好奇": "对方说的事她没见过或想知道，直接追着问",
}

# 判断的是"这轮有多少东西可接"，不是字数。长度由代码翻译。
SUBSTANCE_LEVELS = [
    "对方就是随口吭一声、丢个表情、刷个梗，没什么可接的",
    "对方说了件具体的事，接住这一件事就够",
    "对方抛了个真问题或真话题，她有得聊",
]

_LENGTH_HINT = [
    "对方没给多少内容，随口接住就够，不必找新话题。",
    "接住对方说的那件事，想说多少由这件事决定。",
    "对方真有得聊，可以顺着话题展开。",
]


class Judgment:
    def __init__(self, enabled: bool, api_key: str = "", model: str = ""):
        self._client = None
        self.enabled = bool(enabled) and AsyncTypeSafeClient is not None
        if not self.enabled:
            return
        key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            logger.warning("[琪露诺判断] 没有 API key，判断功能关闭")
            self.enabled = False
            return
        kwargs = {
            "api_key": key,
            # 判断挡在回复前面，宁可放弃也别拖着她不说话
            "retry": RetryPolicy(max_retries=1, timeout=3.0),
        }
        if model:
            kwargs["model"] = model
        self._client = AsyncTypeSafeClient(**kwargs)

    async def aclose(self):
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    def _build_state(self, plugin, event, peers, caption, sender_id) -> dict:
        from .cirno_states import CIRNO_STATES

        state_label = CIRNO_STATES.get(
            plugin.state_manager.current_state, {}
        ).get("label", "")
        test_feeling = event.get_extra("cirno_test_feeling")
        mood = plugin.mood_manager.get_debug_info(
            event.get_extra("cirno_test_mood"), test_feeling
        )
        valence = {
            ("positive", "strong"): 0.85,
            ("positive", "mild"): 0.65,
            ("negative", "strong"): 0.15,
            ("negative", "mild"): 0.35,
            ("neutral", "strong"): 0.5,
        }.get((mood["feeling"], mood["feeling_intensity"]), plugin.emotion.valence)

        her_view = {"熟不熟": "没什么印象"}
        prof = (
            plugin.core_memory.get_profile(sender_id)
            if plugin._enable_core_memory
            else None
        )
        if prof:
            if prof.get("relationship"):
                her_view = {
                    "熟不熟": "熟",
                    "她对这个人的感觉": prof["relationship"],
                }
            events = prof.get("important_events", [])[:2]
            if events:
                her_view["还记得的事"] = [
                    e.get("event", e) if isinstance(e, dict) else e for e in events
                ]

        return {
            "这条消息": {
                "谁说的": event.get_sender_name(),
                # 纯图片消息的 message_str 是空的，得把转述补上，
                # 否则判断模型是在对一条空消息打分。
                "内容": (event.message_str or "").strip() or caption or "[图片]",
            },
            "群里刚才在聊": peers,
            "她对这个人": her_view,
            "她此刻": {
                "在干嘛": state_label,
                "现在是什么状态": mood.get("mood_label", ""),
                "心里挂着的事": mood.get("note", ""),
                "精力": round(plugin.emotion.arousal, 2),
                "心情好坏": round(valence, 2),
                "刚被聊天影响成": mood.get("feeling", "none"),
            },
        }

    async def judge(self, plugin, event, peers, caption="", sender_id="") -> dict | None:
        """两个判断一次问完。它们读同一份 state 且互不依赖，并行返回，只付一次延迟。"""
        if not self.enabled or self._client is None:
            return None
        try:
            state = self._build_state(plugin, event, peers, caption, sender_id)
            resp = await self._client.system_one(
                state=state,
                questions={
                    "有多少可接": Score(
                        instructions=(
                            "对方这条消息给了琪露诺多少可以接的东西？"
                            "看的是内容的分量，不是字数长短。"
                        ),
                        criteria=SUBSTANCE_LEVELS,
                    ),
                    "挑哪种反应": Choice(
                        instructions=(
                            "琪露诺这次该挑哪一种反应？先看对方在对谁说什么，"
                            "不要替对方编意图。没话头时可以随口应一声；"
                            "认真说话时可以直接接住；有明确的玩笑或破绽时再逗回去。"
                            "她此刻的心情也会改变她想接什么、选哪种反应，不只是改变语气；"
                            "但别因此编造对方没说过的意图。"
                        ),
                        criteria=REACTIONS,
                    ),
                },
            )
        except TypeSafeError as e:
            logger.warning(f"[琪露诺判断] 调用失败: {e}")
            return None
        except Exception as e:
            logger.warning(f"[琪露诺判断] 异常: {e}")
            return None

        a = resp.answers
        return {
            "有多少可接": round(a["有多少可接"].score, 2),
            "可接confidence": round(a["有多少可接"].confidence, 3),
            "反应": a["挑哪种反应"].choice,
            "反应confidence": round(a["挑哪种反应"].confidence, 3),
        }

    @staticmethod
    def build_prompt(result: dict) -> str:
        """判断结果翻译成给她的指令。长度用连续值分档，不是模型直接说的字数。"""
        level = min(int(round(result["有多少可接"])), len(_LENGTH_HINT) - 1)
        reaction = result["反应"]
        desc = REACTIONS.get(reaction, "")
        return (
            f"\n【这次】{_LENGTH_HINT[level]}"
            f"\n挑这一个反应：{reaction}。{desc}。只做这一件事，别捎带别的。"
        )

    @staticmethod
    def format_for_trace(result: dict) -> str:
        return (
            f"有多少可接: {result['有多少可接']} (conf {result['可接confidence']})\n"
            f"反应: {result['反应']} (conf {result['反应confidence']})"
        )
