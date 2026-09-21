"""把"她该不该开口、说多长、挑哪种反应"交给一个只做判断的小模型。

为什么不让主模型判断：TimingGate 现在就是拿主模型问"要不要插嘴"，慢到得设超时
（代码里有"超时，默认不插嘴"这个分支），还和主回复抢同一个限流配额。判断和生成
同源还有个副作用：让她自己决定要不要说话，等于给了她一个弃答选项，而那种选项会
被滥用。外部判断没这问题，决定权不在她手里。

现在是旁路：只把结果写进 trace，不改她的行为。先看判断准不准。
"""

import os

from astrbot.api import logger

try:
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        Noul,
        RetryPolicy,
        Score,
        TypeSafeError,
    )
except ImportError:  # SDK 没装就整个功能静默关掉
    AsyncTypeSafeClient = None
    TypeSafeError = Exception


# 挑哪种反应。选项直接取自 ABSOLUTE_RULES 里"情绪上来挑且只挑一个反应"那份清单，
# 那里本来就写好了每种的适用情形，这里只是把"建议"变成"外部决定"。
REACTIONS = {
    "损人打趣": "揪着对方话里的毛病打趣他，不留情但不恶意",
    "揪漏洞": "对方话里有说不通的地方，抓住它追问或拆台",
    "故意曲解": "明知道他什么意思，偏要歪着理解，把话题揽到自己身上",
    "起哄拱火": "两个人之间有戏，她在旁边添柴，把场子闹大",
    "得意炫耀": "有机会显摆自己最强，绝不放过",
    "歪理绕人": "用幻想乡的逻辑把对方绕进去，一本正经地胡说",
    "反将一军": "对方想逗她或占便宜，她反手把矛头转回去",
    "嘴硬招架": "被戳中了或害羞了，嘴上不认，防守姿态",
}

LENGTH_LEVELS = [
    "一个字或一个词的反应就够了，纯情绪，没有内容",
    "一句话，接住对方说的那一件事",
    "两三句，有来有回地闹起来",
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
            # 判断是旁路，宁可放弃也别拖慢主链路
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

    def _build_state(self, plugin, event, peers: list[str]) -> dict:
        """判断要读的东西：群里刚才在聊什么，她此刻是什么状态。

        心情只给 arousal（精力），不给 valence（情绪好坏）。valence 参与"说不说"
        会螺旋：心情低 -> 判断少说 -> 说得少 -> <inner> 还是负面 -> 心情更低。
        精力高低影响话多话少是自然的，不会自我强化。
        """
        from .cirno_states import CIRNO_STATES

        state_label = CIRNO_STATES.get(
            plugin.state_manager.current_state, {}
        ).get("label", "")
        return {
            "群里刚才在聊": peers,
            "这条消息": {
                "谁说的": event.get_sender_name(),
                "内容": event.message_str or "",
                "有没有叫琪露诺": bool(event.is_at_or_wake_command),
            },
            "琪露诺此刻": {
                "在干嘛": state_label,
                "精力": round(plugin.emotion.arousal, 2),
                "心里挂着的事": plugin.mood_manager.get_debug_info().get("note", ""),
            },
        }

    async def judge(self, plugin, event, peers: list[str]) -> dict | None:
        """三个判断一次问完。它们读同一份 state 且互不依赖，并行返回，只付一次延迟。"""
        if not self.enabled or self._client is None:
            return None
        state = self._build_state(plugin, event, peers)
        try:
            resp = await self._client.system_one(
                state=state,
                questions={
                    "该开口": Noul(
                        instructions=(
                            "琪露诺现在应该开口说话吗？她是个爱凑热闹、坐不住的冰精灵，"
                            "别人没招惹她她也会主动凑上去搭话。"
                            "注意：她精力差或心情不好的时候不是更沉默，而是更想去戳人、拆台。"
                        ),
                        criteria={
                            "true": "有人在跟她说话或提到她；话题热闹到她忍不住插一嘴；"
                            "有人说了句怪话或肉麻话可以起哄；有人在逗她",
                            "false": "两个人在认真聊她插不上的正事；话题已经过去了；"
                            "她刚说完话还没人接，再说就是自说自话；"
                            "群里在刷屏或复读，没有她的位置",
                        },
                    ),
                    "说多长": Score(
                        instructions=(
                            "琪露诺这次该说多长？她说话短促跳脱，想到什么说什么。"
                            "看群里现在的节奏：别人都在甩短句时她写小作文就很怪。"
                        ),
                        criteria=LENGTH_LEVELS,
                    ),
                    "挑哪种反应": Choice(
                        instructions=(
                            "琪露诺这次该挑哪一种反应？她一条回复只做一件事。"
                            "优先主动进攻型的，防守型（嘴硬招架）只偶尔对亲近的人用。"
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
            "该开口": round(a["该开口"].noul, 3),
            "说多长": {
                "score": round(a["说多长"].score, 2),
                "confidence": round(a["说多长"].confidence, 3),
            },
            "挑哪种反应": {
                "choice": a["挑哪种反应"].choice,
                "confidence": round(a["挑哪种反应"].confidence, 3),
            },
        }

    @staticmethod
    def format_for_trace(result: dict) -> str:
        length = result["说多长"]
        reaction = result["挑哪种反应"]
        level = LENGTH_LEVELS[min(int(round(length["score"])), len(LENGTH_LEVELS) - 1)]
        return (
            f"该开口: {result['该开口']}\n"
            f"说多长: {length['score']} (conf {length['confidence']}) -> {level}\n"
            f"挑哪种反应: {reaction['choice']} (conf {reaction['confidence']})"
        )
