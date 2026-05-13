import asyncio
import random
import re
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image, Poke
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType

from .affinity import AffinityManager
from .core_memory import CoreMemory
from .jargon_filter import JargonStatisticalFilter
from .meme_sender import MemeSelector
from .recall_memory import RecallMemory
from .state_manager import CirnoStateManager
from .user_message_store import UserMessageStore
from .slang_store import SlangStore
from .group_message_store import GroupMessageStore

try:
    from .local_config import DEFAULT_USER_INFO, ABSOLUTE_RULES
except ImportError:
    DEFAULT_USER_INFO = {}
    ABSOLUTE_RULES = ""


class Main(Star):
    context: Context

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        state_cfg = config.get("state_settings", {})
        proactive_cfg = config.get("proactive_settings", {})
        memory_cfg = config.get("memory_settings", {})

        self.state_manager = CirnoStateManager(
            min_state_duration=state_cfg.get("min_state_duration", 1800),
            transition_rate=state_cfg.get("transition_rate", 0.05),
            max_transition_chance=state_cfg.get("max_transition_chance", 0.3),
            proactive_cooldown=proactive_cfg.get("cooldown_seconds", 2700),
            proactive_base_chance=proactive_cfg.get("base_chance", 0.15),
            enable_season=state_cfg.get("enable_season", True),
        )

        self._enable_core_memory = memory_cfg.get("enable_core_memory", True)
        self._enable_recall_memory = memory_cfg.get("enable_recall_memory", True)
        self._allow_stranger_profile = memory_cfg.get("allow_stranger_profile", True)

        self.core_memory = CoreMemory(
            plugin=self,
            seed_data=DEFAULT_USER_INFO,
            update_threshold=memory_cfg.get("core_memory_update_threshold", 15),
        )
        self.recall_memory = RecallMemory(
            plugin=self,
            buffer_limit=memory_cfg.get("buffer_limit", 15),
            top_k=memory_cfg.get("recall_search_top_k", 3),
        )

        debug_cfg = config.get("debug_settings", {})
        self._show_full_prompt = debug_cfg.get("show_full_prompt", False)

        affinity_cfg = config.get("affinity_settings", {})
        self._enable_affinity = affinity_cfg.get("enable", True)
        self.affinity = AffinityManager(
            plugin=self,
            boredom_window=affinity_cfg.get("boredom_window", 300),
            boredom_threshold=affinity_cfg.get("boredom_threshold", 12),
        )

        meme_cfg = config.get("meme_settings", {})
        self._enable_meme = meme_cfg.get("enable", True)
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_cirno"))
        self.meme_selector = MemeSelector(
            meme_dir=data_dir,
            probability=meme_cfg.get("probability", 0.07),
        )

        self._group_sessions: set[str] = set()
        self._cron_job_id: str | None = None
        self._daily_profile_cron_id: str | None = None
        self._daily_profile_group = "1050431190"
        self._last_full_prompt: str = ""
        self._imitation_state: dict | None = None
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_cirno"))
        self.user_msg_store = UserMessageStore(data_dir)
        self.group_msg_store = GroupMessageStore(data_dir)
        self.slang_store = SlangStore(data_dir)
        self.slang_store.load()
        self._known_groups: list[tuple[str, str]] = []
        self._slang_msg_counter: int = 0
        self._prank_state: dict | None = None
        self._critique_state: dict | None = None
        self._global_notes: list[str] = []
        self._recent_bot_replies: list[str] = []
        self.jargon_filter = JargonStatisticalFilter()

    async def initialize(self):
        import jieba
        await asyncio.get_event_loop().run_in_executor(None, jieba.lcut, "预热")
        logger.info("jieba 分词已预热")

        saved = await self.get_kv_data("state_data", None)
        if saved and isinstance(saved, dict):
            self.state_manager.from_dict(saved)
            logger.info(
                f"琪露诺状态已恢复: {self.state_manager.current_state}"
            )

        config_sessions = self.config.get("group_sessions", "")
        if config_sessions and isinstance(config_sessions, str):
            for line in config_sessions.strip().splitlines():
                line = line.strip()
                if line:
                    self._group_sessions.add(line)

        saved_sessions = await self.get_kv_data("group_sessions", None)
        if saved_sessions and isinstance(saved_sessions, list):
            self._group_sessions.update(saved_sessions)

        if self._group_sessions:
            logger.info(f"已加载 {len(self._group_sessions)} 个群聊 session")

        saved_notes = await self.get_kv_data("global_notes", None)
        if isinstance(saved_notes, list):
            self._global_notes = saved_notes
            logger.info(f"全局笔记已加载，共 {len(self._global_notes)} 条")

        if self._enable_affinity:
            await self.affinity.load()
        if self._enable_core_memory:
            await self.core_memory.load()
        if self._enable_recall_memory:
            self.recall_memory.set_llm_generate(self._recall_llm_generate)
            if self._enable_affinity:
                self.recall_memory.set_key_event_callback(self._on_buffer_key_event)
            await self.recall_memory.load()
            _mem_cfg = self.config.get("memory_settings", {})
            if _mem_cfg.get("enable_embedding_recall", False):
                try:
                    providers = self.context.get_all_embedding_providers()
                    pid = _mem_cfg.get("embedding_provider_id", "")
                    ep = next((p for p in providers if p.meta().id == pid), None) if pid else None
                    if ep is None and providers:
                        ep = providers[0]
                    if ep:
                        self.recall_memory.set_embed_provider(ep)
                        logger.info(f"[琪露诺] Embedding 检索已启用: {ep.meta().id}")
                    else:
                        logger.warning("[琪露诺] 未找到 Embedding Provider，降级到关键词检索")
                except Exception as e:
                    logger.warning(f"[琪露诺] Embedding Provider 初始化失败: {e}")

        if self._enable_meme:
            stats = self.meme_selector.get_stats()
            total = sum(stats.values())
            logger.info(
                f"琪露诺表情包已加载: 共{total}张, "
                + ", ".join(f"{k}={v}" for k, v in stats.items())
            )

        proactive_cfg = self.config.get("proactive_settings", {})
        if proactive_cfg.get("enable", True):
            interval = max(1, int(proactive_cfg.get("check_interval_minutes", 10)))
            job = await self.context.cron_manager.add_basic_job(
                name="cirno_proactive_check",
                cron_expression=f"*/{interval} * * * *",
                handler=self._proactive_check,
                description="琪露诺主动发言检查",
            )
            self._cron_job_id = job.job_id
            logger.info(
                f"琪露诺主动发言 cron job 已注册，间隔 {interval} 分钟"
            )

        if self._enable_core_memory:
            daily_job = await self.context.cron_manager.add_basic_job(
                name="cirno_daily_profile_update",
                cron_expression="0 3 * * *",
                handler=self._daily_profile_update,
                description="琪露诺每日用户画像更新",
            )
            self._daily_profile_cron_id = daily_job.job_id
            logger.info("琪露诺每日用户画像 cron job 已注册，每日凌晨3点触发")


    def _replace_at_with_names(self, text: str) -> str:
        def _repl(m):
            qq = m.group(2)
            p = self.core_memory.get_profile(qq)
            if p:
                return f"@{p.get('name', m.group(1))}"
            return m.group(0)
        return re.sub(r"@([^(]+)\((\d+)\)", _repl, text)

    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        if (event.message_str or "").startswith("//"):
            return
        event.set_extra("cirno_llm_start", time.time())
        self.state_manager.on_user_interaction()
        transitioned = self.state_manager.maybe_transition()
        if transitioned:
            await self.put_kv_data("state_data", self.state_manager.to_dict())

        if event.session.message_type == MessageType.GROUP_MESSAGE:
            umo = event.unified_msg_origin
            if umo not in self._group_sessions:
                self._group_sessions.add(umo)
                await self.put_kv_data(
                    "group_sessions", list(self._group_sessions)
                )
            platform_id = event.get_platform_id()
            group_id = event.get_group_id()
            if platform_id and group_id and (platform_id, group_id) not in self._known_groups:
                self._known_groups.append((platform_id, group_id))

        sender_id = str(event.get_sender_id())
        sender_nickname = event.get_sender_name()
        logger.info(
            f"[琪露诺触发] 用户={sender_nickname}({sender_id}), "
            f"状态={self.state_manager.current_state}, "
            f"消息={event.message_str[:50] if event.message_str else ''}"
        )

        if self._enable_core_memory:
            user_msg_text = event.message_str or ""
            user_msg_text = self._replace_at_with_names(user_msg_text)
            people_prompt = self.core_memory.build_people_prompt(user_msg_text, sender_id)
            if people_prompt:
                req.system_prompt += f"\n{people_prompt}"
        else:
            req.system_prompt += "\n你认识一些人，但现在记忆模糊。"

        from .cirno_states import CIRNO_STATES
        current_category = CIRNO_STATES.get(
            self.state_manager.current_state, {}
        ).get("category", "")

        req.system_prompt += f"\n{self.state_manager.get_prompt_injection()}"
        suppress_recall = current_category == "rest"

        has_recall = False
        recall_prompt = ""
        if self._enable_recall_memory and not suppress_recall:
            user_msg = event.message_str or ""
            if user_msg:
                memories = await self.recall_memory.search_async(
                    user_msg, current_user_id=sender_id,
                    current_group_id=event.get_group_id() or None
                )
                if memories:
                    has_recall = True
                    logger.info(
                        f"[琪露诺回忆检索] 命中 {len(memories)} 条: "
                        + ", ".join(
                            f"「{m.get('text', '')[:30]}」"
                            for m in memories
                        )
                    )
                recall_prompt = self.recall_memory.build_recall_prompt(memories)

        is_random_reply = (
            not event.is_at_or_wake_command
            and event.session.message_type == MessageType.GROUP_MESSAGE
        )
        if is_random_reply:
            req.system_prompt += (
                "\n你不是被叫到的，是自己凑过来插嘴的。"
                "如果话题你不了解，绝对不要承认不知道——从字面意思或听起来像什么去猜，"
                "然后基于你的理解（通常是错的）自信地参与讨论。"
                "或者被某个具体的细节吸引，只追问那一个点。"
            )

        if self._enable_core_memory:
            req.system_prompt += self.core_memory.build_sender_prompt(
                sender_id, sender_nickname
            )
            self.core_memory.record_interaction(sender_id)
        else:
            req.system_prompt += (
                f"\n当前和你对话的人QQ号是{sender_id}，QQ昵称是「{sender_nickname}」。"
            )

        if self._enable_affinity:
            req.system_prompt += self.affinity.build_status_prompt(sender_id)

        if recall_prompt:
            req.system_prompt += f"\n{recall_prompt}"
        if has_recall:
            req.system_prompt += (
                "\n如果对方聊的话题和你记忆中的内容有关，你可以自然地提起你还记得之前聊过的事。"
                "不要生硬地复述记忆内容，而是像真的想起来了一样随口带一嘴。"
            )

        req.system_prompt += (
            "\n如果用户用括号描述情景或旁白，你知道这是在演戏、开玩笑。"
            "你可以配合玩但不要入戏太深，保持琪露诺的正常状态，不要被剧情带走。"
        )
        if self._prank_state is not None:
            req.system_prompt += self._build_prank_prompt(sender_id, sender_nickname)

        if self._critique_state is not None:
            req.system_prompt += self._build_critique_prompt()

        if self._imitation_state is not None:
            tname = self._imitation_state["target_name"]
            style = self._imitation_state["style_desc"]
            req.system_prompt += (
                f"\n【当前任务】你现在在模仿「{tname}」的说话风格。"
                f"你仍然是琪露诺，有琪露诺的记忆和性格，但你说话的方式、语气、用词习惯要尽量像{tname}。"
                f"\n{tname}的说话风格特点：\n{style}"
                f"\n模仿时：保留琪露诺的思维方式和情感，但把表达方式换成{tname}的风格。"
                f"不要在回复中说「我在模仿{tname}」，直接用那个风格说话。"
            )
        slang_matches = self.slang_store.match(event.message_str or "")
        if slang_matches:
            slang_lines = "\n".join(
                f'「{e["word"]}」：{e["meaning"]}，可以自然地用在合适的场合。'
                for e in slang_matches
            )
            req.system_prompt += f"\n【群里的说法】\n{slang_lines}"
        if self._global_notes:
            notes_text = "\n".join(f"- {n}" for n in self._global_notes)
            req.system_prompt += f"\n【你特意记下来的事】\n{notes_text}"
        if self._recent_bot_replies:
            recent_str = "、".join(
                f"「{r[:30]}…」" if len(r) > 30 else f"「{r}」"
                for r in self._recent_bot_replies
            )
            req.system_prompt += f"\n【你最近说过】{recent_str}——避免重复相同的开场白、句式和结尾。"
        req.system_prompt += ABSOLUTE_RULES
        if self._enable_affinity:
            req.system_prompt += self.affinity.build_rating_prompt()
        if self._enable_core_memory and req.prompt:
            req.prompt = self._replace_at_with_names(req.prompt)

        parts = [f"=== SYSTEM PROMPT ===\n{req.system_prompt}"]
        if req.contexts:
            parts.append(f"\n=== CONTEXTS ({len(req.contexts)} turns) ===")
            for msg in req.contexts:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(f"[{role}] {content}")
                elif isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                            elif item.get("type") == "image_url":
                                text_parts.append("[图片]")
                            else:
                                text_parts.append(f"[{item.get('type', '?')}]")
                    parts.append(f"[{role}] {''.join(text_parts)}")
        parts.append(f"\n=== PROMPT ===\n{req.prompt or ''}")
        self._last_full_prompt = "\n".join(parts)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        llm_start = event.get_extra("cirno_llm_start")
        if llm_start:
            logger.info(f"[琪露诺延迟] LLM 往返耗时 {time.time() - llm_start:.2f}s")

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name()
        user_msg = event.message_str or ""
        bot_reply = resp.completion_text or ""

        bot_reply = re.sub(r"[（(][^）)]*[）)]", "", bot_reply).strip()
        bot_reply = re.sub(r"\*[^*]+\*", "", bot_reply).strip()
        if bot_reply != (resp.completion_text or ""):
            resp.completion_text = bot_reply

        valence_shift: float | None = None
        interaction_type: str | None = None
        if self._enable_affinity and bot_reply:
            cleaned, valence_shift, reason, interaction_type = self.affinity.extract_inner(bot_reply)
            if cleaned != bot_reply:
                resp.completion_text = cleaned
                bot_reply = cleaned

            if valence_shift is not None:
                from .cirno_states import CIRNO_STATES
                cat = CIRNO_STATES.get(self.state_manager.current_state, {}).get("category", "")
                self.affinity.update_emotion(valence_shift, cat)
                self.affinity.update_affinity(sender_id, valence_shift, interaction_type)
                logger.info(
                    f"[琪露诺情绪] v={self.affinity.valence:.2f} a={self.affinity.arousal:.2f} "
                    f"vuln={self.affinity.vulnerability:.2f} shift={valence_shift:.2f} "
                    f"reason={reason} | "
                    f"[好感度] {sender_name}({sender_id}): "
                    f"composite={self.affinity.get_composite(sender_id):.1f} "
                    f"等级={self.affinity.get_level(sender_id)}"
                )

                self.affinity.increment_event_counter(sender_id)
                self.affinity.record_interaction(sender_id)

        if not user_msg or not bot_reply:
            return

        if self._enable_recall_memory:
            await self.recall_memory.archive(
                sender_id, sender_name, user_msg, bot_reply,
                group_id=event.get_group_id() or None
            )
            logger.info(f"[琪露诺回忆归档] {sender_name}({sender_id}): {user_msg[:30]}")

        self.user_msg_store.append(sender_id, sender_name, user_msg)

        self._recent_bot_replies.append(bot_reply[:80])
        if len(self._recent_bot_replies) > 5:
            self._recent_bot_replies.pop(0)

        if event.session.message_type == MessageType.GROUP_MESSAGE:
            platform_id = event.get_platform_id()
            group_id = event.get_group_id()
            if platform_id and group_id and user_msg:
                self.jargon_filter.update_from_message(
                    user_msg, f"{platform_id}:{group_id}", sender_id
                )
                if group_id == self._daily_profile_group:
                    self.group_msg_store.append(
                        group_id, sender_id, sender_name, user_msg, bot_reply or None
                    )
            self._slang_msg_counter += 1
            if self._slang_msg_counter >= 75:
                self._slang_msg_counter = 0
                asyncio.create_task(self._slang_update())

        if self._enable_core_memory and "记住" in bot_reply:
            asyncio.create_task(self._extract_and_memorize(
                sender_id, sender_name, user_msg, bot_reply
            ))

        if self._enable_core_memory:
            is_known = self.core_memory.get_profile(sender_id) is not None
            if is_known or self._allow_stranger_profile:
                count = self.core_memory.get_interaction_count(sender_id)
                if self.core_memory.should_update(sender_id):
                    logger.info(
                        f"[琪露诺核心记忆] 触发LLM更新 {sender_name}({sender_id}), "
                        f"交互计数={count}/{self.core_memory.update_threshold}"
                    )
                    recent_records = self.user_msg_store.get_recent(sender_id, limit=20)
                    if recent_records:
                        lines = [f"{r['name']}：「{r['msg']}」" for r in recent_records]
                        recent_summary = "\n".join(lines)
                    else:
                        recent_summary = f"{sender_name}说：「{user_msg}」\n琪露诺回答：「{bot_reply}」"
                    asyncio.create_task(self.core_memory.update_profile_via_llm(
                        sender_id, recent_summary, self.context, nickname=sender_name
                    ))

        if self._enable_meme:
            meme_path = self.meme_selector.select(bot_reply)
            if meme_path:
                event.set_extra("cirno_meme_path", meme_path)

        if (
            self._enable_affinity
            and event.session.message_type == MessageType.GROUP_MESSAGE
        ):
            valence = self.affinity.valence
            chance = 0.01 + max(0, (0.5 - valence)) * 0.08
            if random.random() < chance:
                event.set_extra("cirno_poke", True)
                logger.info(
                    f"[琪露诺戳一戳] 触发! valence={valence:.2f} chance={chance:.2%}"
                )

        if self._critique_state is not None:
            self._critique_state = None
            logger.info("[琪露诺锐评] 锐评结束")
        elif any(kw in user_msg for kw in ("评价一下", "评价下", "点评一下", "点评下", "你怎么看", "怎么看这")):
            self._critique_state = {"topic": user_msg[:100]}
            logger.info(f"[琪露诺锐评] 触发，话题：{user_msg[:40]}")

        if self._prank_state is not None:
            if self._prank_state.get("ending"):
                self._prank_state = None
                logger.info("[琪露诺恶作剧] 收尾完成，恶作剧结束")
            elif time.time() >= self._prank_state["expires_at"]:
                self._prank_state["ending"] = True
                logger.info("[琪露诺恶作剧] 恶作剧时间到，进入收尾")
            else:
                if valence_shift is not None and valence_shift < 0.4:
                    self._prank_state["escalation"] = self._prank_state.get("escalation", 0) + 1
                    logger.info(f"[琪露诺恶作剧] 对方反应激烈，升级={self._prank_state['escalation']}")
        elif self._critique_state is None and self._enable_affinity and event.session.message_type == MessageType.GROUP_MESSAGE:
            self._maybe_enter_prank(sender_id)

    def _maybe_enter_prank(self, sender_id: str):
        valence = self.affinity.valence
        composite = self.affinity.get_composite(sender_id)
        # 心情好才有恶作剧的兴致，心情差就算了
        if valence < 0.55:
            return
        # 基础概率由心情决定，好感度作为乘数（好感高概率更高，但低好感也有机会）
        mood_factor = (valence - 0.55) / 0.45  # 0~1
        affinity_factor = 0.3 + 0.7 * (composite / 100.0)  # 0.3~1.0
        chance = mood_factor * affinity_factor * 0.12
        if random.random() < chance:
            state = self._start_prank(sender_id)
            duration = int(state["expires_at"] - time.time()) // 60
            logger.info(
                f"[琪露诺恶作剧] 进入恶作剧模式! "
                f"valence={valence:.2f} composite={composite:.0f} "
                f"chance={chance:.2%} duration={duration}min "
                f"pool={state['behavior_pool']}"
            )

    PRANK_BEHAVIORS = [
        "一本正经地深度分析{name}说的话，把最普通的话过度解读成意义深远、暗藏玄机的东西。分析要长、要认真、要有条理，列出你的推理过程，越煞有介事越好，让人觉得突兀又好笑",
        "根据{name}的名字或说话内容，给他起一个奇怪但有一定逻辑的外号，然后全程叫那个外号，态度理所当然，如果对方反应就解释你的命名理由",
        "故意曲解{name}说的话，理解成完全不同的意思，然后基于错误理解认真回应",
        "假装不认识{name}，用完全陌生的语气应对，说「你是谁啊」",
        "编造一件{name}最近在群里干的蠢事，描述得绘声绘色像是亲眼目睹",
        "对{name}说的某件事连续追问「然后呢？」，每次都追一步，完全不管对方答没答",
        "疯狂附和对方说的话，同意程度极其夸张，好像对方说了什么惊天大道理",
        "假装完全听不懂对方说的话，对非常正常的句子一直追问「什么意思」，对方越解释越装傻",
    ]

    def _start_prank(self, triggered_by: str) -> dict:
        duration = random.randint(10, 20) * 60
        used = random.sample(range(len(self.PRANK_BEHAVIORS)), min(4, len(self.PRANK_BEHAVIORS)))
        self._prank_state = {
            "expires_at": time.time() + duration,
            "triggered_by": triggered_by,
            "behavior_pool": used,
            "escalation": 0,
            "ending": False,
        }
        return self._prank_state

    def _build_prank_prompt(self, sender_id: str, sender_name: str) -> str:
        if self._prank_state.get("ending"):
            return (
                "\n【恶作剧刚结束】你刚才在搞恶作剧，现在悄悄收手了。"
                "这条回复假装什么都没发生，自然地回到正常状态，不要解释。"
            )
        pool = self._prank_state.get("behavior_pool", [0])
        idx = random.choice(pool)
        behavior = self.PRANK_BEHAVIORS[idx % len(self.PRANK_BEHAVIORS)].format(name=sender_name)
        escalation = self._prank_state.get("escalation", 0)
        escalation_hint = ""
        if escalation >= 2:
            escalation_hint = "对方已经有反应了，你越搞越起劲，变本加厉。"
        remaining = max(0, int(self._prank_state["expires_at"] - time.time())) // 60
        return (
            f"\n【恶作剧模式】你现在心情特别好，想搞点事情。这条回复请：{behavior}。"
            f"{escalation_hint}保持自然，像是你真的这么想，不要解释自己在搞恶作剧。"
            f"（剩余约 {remaining} 分钟）"
        )

    def _build_critique_prompt(self) -> str:
        topic = self._critique_state.get("topic", "") if self._critique_state else ""
        topic_hint = f"评价对象：「{topic}」。" if topic else ""
        return (
            f"\n【点评模式】有人请你评价一件事，你决定认真说说你的看法。{topic_hint}"
            "用琪露诺的方式点评：一本正经、自以为看穿了一切、逻辑有点歪但说得头头是道。"
            "先说你对这件事的第一反应，然后展开分析——可以从幻想乡的视角类比，"
            "可以指出你觉得最可笑或最有意思的地方，可以夸张但要言之有物。"
            "语气自信傲慢，带着「本天才见多识广」的得意，但偶尔会暴露自己其实没太懂。"
            "回复必须足够长，至少说三个点，不能敷衍。"
        )

    async def _recall_llm_generate(self, prompt: str):
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("回忆记忆：无可用 LLM Provider")
            return None
        return await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt="你是一个记忆压缩器，只输出压缩后的记忆文本，不要输出其他任何内容。",
        )

    async def _on_buffer_key_event(self, user_id: str, nickname: str, entries: list[dict]):
        if not entries:
            return

        msg_lines = []
        for entry in entries:
            name = entry.get("name", "?")
            msg_lines.append(f"{name}：{entry.get('msg', '')}")
            msg_lines.append(f"琪露诺：{entry.get('reply', '')}")
        messages_text = "\n".join(msg_lines)

        prompt_text = self.affinity.build_key_event_prompt(nickname, messages_text)

        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("关键事件评估：无可用 LLM Provider")
            return

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt_text,
                system_prompt="你是一个JSON生成器，只输出合法的JSON或null，不要输出其他任何内容。",
            )
        except Exception as e:
            logger.error(f"关键事件评估 LLM 调用失败: {e}")
            return

        if not llm_resp or not llm_resp.completion_text:
            return

        result = self.affinity.parse_key_event_result(llm_resp.completion_text)
        if not result:
            logger.info(f"[琪露诺关键事件] {nickname}({user_id}): 无关键事件")
            return

        self.affinity.update_key_event(user_id, result["dimension"], result["delta"])
        logger.info(
            f"[琪露诺关键事件] {nickname}({user_id}): "
            f"event={result['event']}, dim={result['dimension']}, "
            f"delta={result['delta']:+.2f}"
        )

        if result.get("memory") and self._enable_core_memory:
            profile = self.core_memory.get_profile(user_id)
            if profile:
                events = profile.get("important_events", [])
                events.append(result["memory"][:50])
                profile["important_events"] = events[-3:]
                await self.core_memory.save()
                logger.info(f"[琪露诺关键事件] 写入核心记忆: {result['memory']}")

        await self.affinity.save()

    async def _extract_and_memorize(self, user_id: str, user_name: str, user_msg: str, bot_reply: str):
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("记住触发：无可用 LLM Provider")
            return

        prompt = (
            f"琪露诺刚刚说了这句话：「{bot_reply}」\n"
            f"这是在回应对方说的：「{user_msg}」\n\n"
            "琪露诺说要记住某件事。请用一句话（不超过25个字）总结她记住了什么，"
            "用第三人称描述，例如「对方喜欢吃草莓大福」。\n"
            "只输出那一句话，不加任何前缀或解释。"
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是一个信息提取器，只输出一句话，不要任何多余内容。",
            )
        except Exception as e:
            logger.error(f"记住触发 LLM 提取失败: {e}")
            return

        if not resp or not resp.completion_text:
            return

        event_text = resp.completion_text.strip()[:50]
        if event_text and event_text not in self._global_notes:
            self._global_notes.append(event_text)
            if len(self._global_notes) > 20:
                self._global_notes.pop(0)
            await self.put_kv_data("global_notes", self._global_notes)

        logger.info(f"[琪露诺记住] {user_name}({user_id}): {event_text}")

    async def _build_style_description(self, uid: str, name: str, profile: dict | None) -> str:
        records = self.user_msg_store.get_recent(uid, limit=30)
        if not records:
            return f"（没有足够的关于{name}的记忆来模仿说话风格，尽力而为）"

        lines = [f"  {r['name']}：「{r['msg']}」" for r in records if r.get("msg", "").strip()]
        if not lines:
            return f"（没有足够的关于{name}的记忆来模仿说话风格，尽力而为）"

        prompt = (
            f"下面是「{name}」实际说过的话，按时间顺序排列。\n"
            f"请分析{name}的说话风格、用词习惯、句式特点、常用语气词等，"
            f"总结成3-5条简短的风格描述，供角色扮演使用。\n"
            f"只输出风格描述，不要输出其他内容，不要输出分析过程。\n\n"
            f"{name}说过的话：\n" + "\n".join(lines)
        )

        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("模仿风格：无可用 LLM Provider")
            return f"（无法分析{name}的说话风格）"

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是一个说话风格分析器，输出简洁的风格描述列表，不要输出任何其他内容。",
            )
        except Exception as e:
            logger.error(f"模仿风格 LLM 分析失败: {e}")
            return f"（分析{name}说话风格时出错）"

        if not resp or not resp.completion_text:
            return f"（{name}的说话风格分析返回为空）"

        return resp.completion_text.strip()

    @filter.after_message_sent()
    async def send_meme_after_reply(self, event: AstrMessageEvent):
        meme_path = event.get_extra("cirno_meme_path")
        if not meme_path:
            return
        if event.is_private_chat():
            return
        msg = MessageChain(chain=[Image.fromFileSystem(meme_path)])
        await event.send(msg)

    @filter.after_message_sent()
    async def poke_after_reply(self, event: AstrMessageEvent):
        if not event.get_extra("cirno_poke"):
            return
        bot = getattr(event, "bot", None)
        if not bot:
            return
        sender_id = int(event.get_sender_id())
        group_id = getattr(event.session, "session_id", None)
        if not group_id:
            return
        try:
            await bot.call_action(
                "group_poke",
                group_id=int(group_id),
                user_id=sender_id,
            )
        except Exception as e:
            logger.debug(f"戳一戳发送失败: {e}")

    async def _slang_update(self):
        logger.info("[琪露诺学习] 开始网络用语学习任务（统计预过滤模式）")
        if not self._known_groups:
            logger.info("[琪露诺学习] 没有已知群组，跳过")
            return

        existing_words = {e["word"] for e in self.slang_store.get_all()}
        all_candidates: list[dict] = []
        for platform_id, group_id in self._known_groups:
            group_key = f"{platform_id}:{group_id}"
            candidates = self.jargon_filter.get_jargon_candidates(
                group_key, top_k=10, exclude_terms=existing_words
            )
            all_candidates.extend(candidates)

        if not all_candidates:
            logger.info("[琪露诺学习] 统计过滤后无候选词，跳过")
            return

        # 按分数去重（多群可能有相同候选词），取分数最高的
        seen: dict[str, dict] = {}
        for c in all_candidates:
            term = c["term"]
            if term not in seen or c["score"] > seen[term]["score"]:
                seen[term] = c
        top_candidates = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:10]

        # 构建精简 prompt：候选词 + 少量上下文例句
        cand_lines = []
        for c in top_candidates:
            examples = "；".join(c["context_examples"][:3])
            cand_lines.append(f'「{c["term"]}」（出现{c["frequency"]}次）例：{examples}')

        existing_hint = f"已知词汇（不要重复）：{', '.join(existing_words)}\n" if existing_words else ""
        prompt = (
            f"{existing_hint}"
            "下面是从群聊中统计筛选出的高频候选词，每个词附有出现次数和上下文例句。\n"
            "请判断哪些是群里特有的说法（网络用语、梗、游戏术语、二次元词汇、圈子黑话等），\n"
            "并为确认的词汇提供含义和场景关键词。\n"
            "如果某个词是普通词汇，跳过它。如果全都是普通词汇，返回空数组 []。\n"
            "只输出合法JSON数组，格式：\n"
            '[{"word": "词", "meaning": "含义", "scene": "关键词1 关键词2 关键词3"}, ...]\n\n'
            "候选词：\n" + "\n".join(cand_lines)
        )

        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("[琪露诺学习] 无可用 LLM Provider，跳过")
            return
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是一个JSON生成器，只输出合法的JSON数组，不要输出其他任何内容。",
            )
        except Exception as e:
            logger.error(f"[琪露诺学习] LLM 调用失败: {e}")
            return
        if not resp or not resp.completion_text:
            logger.warning("[琪露诺学习] LLM 返回空结果")
            return
        raw = resp.completion_text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        try:
            new_words = json.loads(raw)
            if not isinstance(new_words, list):
                raise ValueError("not a list")
        except Exception as e:
            logger.error(f"[琪露诺学习] JSON 解析失败: {e} | raw={raw[:200]}")
            return
        added = 0
        for item in new_words:
            if not isinstance(item, dict):
                continue
            word = item.get("word", "").strip()
            meaning = item.get("meaning", "").strip()
            scene = item.get("scene", "").strip()
            if word and meaning and scene:
                if self.slang_store.add(word, meaning, scene):
                    added += 1
                    logger.info(f"[琪露诺学习] 新词: 「{word}」→ {meaning} (scene: {scene})")
        if added:
            self.slang_store.save()
            logger.info(f"[琪露诺学习] 新增 {added} 个词，总计 {len(self.slang_store.get_all())} 个")
        else:
            logger.info("[琪露诺学习] 本次未发现新词")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _on_group_message_all(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if group_id != self._daily_profile_group:
            return
        if event.is_at_or_wake_command:
            return
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name()
        msg = event.message_str or ""
        if not msg.strip():
            return
        self.group_msg_store.append(group_id, sender_id, sender_name, msg, bot_reply=None)

    async def _daily_profile_update(self):
        group_id = self._daily_profile_group
        yesterday = self.group_msg_store.get_yesterday()
        user_ids = self.group_msg_store.get_users_for_day(group_id, yesterday)
        if not user_ids:
            logger.info(f"[琪露诺每日画像] {yesterday} 群 {group_id} 无活跃用户，跳过")
            return

        logger.info(f"[琪露诺每日画像] 开始更新 {yesterday} 群 {group_id} 共 {len(user_ids)} 名用户")
        async def _update_one(user_id: str):
            records = self.group_msg_store.get_records(group_id, yesterday, user_id)
            if not records:
                return False
            nickname = records[0].get("name", user_id)
            try:
                await self.core_memory.update_profile_from_daily(
                    user_id, records, self.context, nickname=nickname
                )
                return True
            except Exception as e:
                logger.error(f"[琪露诺每日画像] 更新用户 {user_id} 失败: {e}")
                return False

        results = await asyncio.gather(*[_update_one(uid) for uid in user_ids], return_exceptions=True)
        updated = sum(1 for r in results if r is True)

        self.group_msg_store.cleanup_old(keep_days=3)
        logger.info(f"[琪露诺每日画像] 完成，更新 {updated}/{len(user_ids)} 名用户")

    async def _proactive_check(self):
        self.state_manager.maybe_transition()
        topic = self.state_manager.should_speak_proactively()
        if not topic:
            return

        await self.put_kv_data("state_data", self.state_manager.to_dict())

        for session_str in list(self._group_sessions):
            try:
                await self._send_proactive_to_group(session_str, topic)
            except Exception as e:
                logger.error(f"主动发言发送失败 ({session_str}): {e}")

    async def _send_proactive_to_group(self, session_str: str, topic: str):
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                session_str
            )
        except Exception:
            providers = self.context.get_all_providers()
            if not providers:
                logger.warning("没有可用的 LLM Provider，跳过主动发言")
                return
            provider_id = providers[0].meta().id

        persona = await self.context.persona_manager.get_default_persona_v3(
            umo=session_str
        )
        base_system_prompt = persona.get("prompt", "") if persona else ""

        if self._enable_core_memory:
            people_prompt = self.core_memory.build_people_prompt(topic)
        else:
            people_prompt = ""

        proactive_cfg = self.config.get("proactive_settings", {})
        suffix = proactive_cfg.get(
            "proactive_system_prompt_suffix",
            "请用琪露诺的语气，简短地说一两句话。不要太长，像是在群里随口说的。",
        )

        parts = [base_system_prompt]
        if people_prompt:
            parts.append(people_prompt)
        parts.append(self.state_manager.get_prompt_injection())
        parts.append(ABSOLUTE_RULES)
        parts.append(suffix)
        system_prompt = "\n".join(parts)

        fake_user_msg = f"[系统：琪露诺现在想说点什么，她刚才的经历是：{topic}]"

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=fake_user_msg,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.error(f"主动发言 LLM 调用失败: {e}")
            return

        if not llm_resp or not llm_resp.completion_text:
            return

        text = llm_resp.completion_text
        if self._enable_affinity:
            cleaned, _, _, _ = self.affinity.extract_inner(text)
            text = cleaned

        msg = MessageChain().message(text)
        await self.context.send_message(session_str, msg)
        logger.info(
            f"琪露诺主动发言已发送到 {session_str}: "
            f"{text[:50]}..."
        )

        if self._enable_meme:
            meme_path = self.meme_selector.select(text)
            if meme_path:
                meme_msg = MessageChain(chain=[Image.fromFileSystem(meme_path)])
                await self.context.send_message(session_str, meme_msg)

    POKE_RESPONSES = {
        "positive": [
            "诶嘿~你戳我干嘛！想跟最强的我玩吗？",
            "哇！干嘛戳我啦！再戳就把你冻成冰棍！",
            "嘿嘿，你戳我一下我就戳你一下！",
            "别戳了别戳了！痒！",
            "哼，就只有你敢戳最强的我！",
        ],
        "neutral": [
            "……干嘛？",
            "戳戳戳，你烦不烦啊",
            "有事说事，别戳",
            "再戳把你手指冻住哦",
        ],
        "negative": [
            "别碰我！",
            "……",
            "烦死了",
            "滚",
        ],
        "rest": [
            "嗯？……我在想事情呢……别戳",
            "吵什么啦……我在思考宇宙的奥秘……",
            "唔……等一下……我刚才想到一个超厉害的点子……忘了",
        ],
    }

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        poke = None
        for msg in event.get_messages():
            if isinstance(msg, Poke):
                poke = msg
                break
        if not poke:
            return

        bot_id = str(event.get_self_id())
        if poke.target_id() and str(poke.target_id()) != bot_id:
            return

        sender_id = str(event.get_sender_id())

        from .cirno_states import CIRNO_STATES
        cat = CIRNO_STATES.get(self.state_manager.current_state, {}).get("category", "")
        if cat == "rest":
            pool = self.POKE_RESPONSES["rest"]
        elif self._enable_affinity:
            level = self.affinity.get_level(sender_id)
            if level in ("无视", "讨厌"):
                pool = self.POKE_RESPONSES["negative"]
            elif level in ("喜欢", "很喜欢"):
                pool = self.POKE_RESPONSES["positive"]
            else:
                pool = self.POKE_RESPONSES["neutral"]
        else:
            pool = self.POKE_RESPONSES["neutral"]

        yield event.plain_result(random.choice(pool))

        if random.random() < 0.2:
            bot = getattr(event, "bot", None)
            group_id = getattr(event.session, "session_id", None)
            if bot and group_id:
                try:
                    await bot.call_action(
                        "group_poke",
                        group_id=int(group_id),
                        user_id=int(sender_id),
                    )
                except Exception:
                    pass

    @filter.command("琪露诺状态")
    async def debug_state(self, event: AstrMessageEvent):
        info = self.state_manager.get_debug_info()
        lines = [
            f"当前状态: {info['state_label']} ({info['state_id']})",
            f"持续时间: {info['duration_hours']}h{info['duration_minutes']}m",
            f"季节: {info['season']}",
            f"主动发言冷却剩余: {info['cooldown_minutes']}min",
            f"无人回应计数: {info['ignored_count']}/3",
            f"沉默模式: {'是' if info['silent'] else '否'}",
            f"已记录群聊: {len(self._group_sessions)}个",
            f"Cron Job: {'已注册' if self._cron_job_id else '未注册'}",
            f"核心记忆: {'启用' if self._enable_core_memory else '禁用'} ({self.core_memory.profile_count}人)",
            f"回忆记忆: {'启用' if self._enable_recall_memory else '禁用'}",
            f"好感度系统: {'启用' if self._enable_affinity else '禁用'}",
        ]
        if self._enable_affinity:
            sender_id = str(event.get_sender_id())
            emo = self.affinity.get_debug_info(sender_id)
            lines.append(
                f"情绪: valence={emo['valence']:.2f} arousal={emo['arousal']:.2f} "
                f"vulnerability={emo['vulnerability']:.2f} baseline={emo['baseline']:.2f}"
            )
            if "user" in emo:
                u = emo["user"]
                lines.append(
                    f"你的好感度: {u['level']}({u['composite']:.0f}/100) "
                    f"[熟悉={u['familiarity']:.2f} 信任={u['trust']:.2f} "
                    f"有趣={u['fun']:.2f} 重要={u['importance']:.2f}]"
                )
        if self._prank_state:
            remaining = max(0, int(self._prank_state["expires_at"] - time.time())) // 60
            lines.append(f"恶作剧模式: 激活，剩余约 {remaining} 分钟")
        else:
            lines.append("恶作剧模式: 未激活")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("琪露诺提示词")
    async def debug_prompt(self, event: AstrMessageEvent):
        if not self._show_full_prompt:
            yield event.plain_result("未开启，请在面板「调试设置」中启用「显示完整提示词」")
            return
        if not self._last_full_prompt:
            yield event.plain_result("还没有记录到提示词，先聊一句再来看")
            return
        yield event.plain_result(self._last_full_prompt)

    @filter.command("琪露诺学说话")
    async def start_imitation(self, event: AstrMessageEvent, target: str = ""):
        target = target.strip()
        if not target:
            yield event.plain_result("哼？你要我学谁说话？告诉我名字或者QQ号啊！")
            return

        uid = None
        name = target
        profile = None

        if self._enable_core_memory:
            for _uid, p in self.core_memory._profiles.items():
                if _uid == target or p.get("name", "") == target:
                    uid = _uid
                    name = p.get("name", target)
                    profile = p
                    break

        if uid is None and target.isdigit():
            uid = target

        if uid is None and not self._enable_recall_memory:
            yield event.plain_result(f"我不认识「{target}」……没有这个人的记忆诶")
            return

        yield event.plain_result(f"嗯……让我想想{name}平时说话是什么样的……")

        style_desc = await self._build_style_description(uid or target, name, profile)

        self._imitation_state = {
            "target_name": name,
            "style_desc": style_desc,
        }
        logger.info(f"[琪露诺模仿] 开始模仿 {name}({uid})，风格描述: {style_desc[:60]}...")
        yield event.plain_result(
            f"好！我知道{name}怎么说话了！\n（风格：{style_desc[:80]}{'…' if len(style_desc) > 80 else ''}）"
        )

    @filter.command("琪露诺停止模仿")
    async def stop_imitation(self, event: AstrMessageEvent):
        if self._imitation_state is None:
            yield event.plain_result("我现在没有在模仿任何人啊！")
            return
        name = self._imitation_state["target_name"]
        self._imitation_state = None
        logger.info(f"[琪露诺模仿] 停止模仿 {name}")
        yield event.plain_result(f"好啦，我不学{name}说话了，恢复成最强的我！")

    @filter.command("琪露诺恶作剧")
    async def start_prank(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if self._prank_state is not None:
            remaining = max(0, int(self._prank_state["expires_at"] - time.time())) // 60
            yield event.plain_result(f"已经在恶作剧了！还剩约 {remaining} 分钟。")
            return
        state = self._start_prank(sender_id)
        duration = int(state["expires_at"] - time.time()) // 60
        logger.info(f"[琪露诺恶作剧] 手动触发，duration={duration}min")
        yield event.plain_result("哼哼……")

    @filter.command("琪露诺记忆")
    async def debug_memory(self, event: AstrMessageEvent, target: str = ""):
        target = target.strip()
        if target == "回忆":
            if not self._enable_recall_memory:
                yield event.plain_result("回忆记忆未启用")
                return
            stats = self.recall_memory.get_stats()
            lines = [
                f"【回忆记忆状态】",
                f"缓冲区: {stats['buffer']}条",
                f"L1摘要: {stats['summaries']}条",
                f"L2浓缩: {stats['digests']}条",
                f"全局计数: {stats['global_count']}",
            ]
            buffer_entries = self.recall_memory.get_buffer_entries()
            if buffer_entries:
                lines.append("\n【最近缓冲区对话】")
                for e in buffer_entries[-10:]:
                    ts = datetime.fromtimestamp(e.get("ts", 0)).strftime("%m-%d %H:%M")
                    name = e.get("name", "?")
                    msg = e.get("msg", "")[:40]
                    reply = e.get("reply", "")[:40]
                    lines.append(f"[{ts}] {name}: {msg}\n  → {reply}")
            summaries = self.recall_memory._summaries
            if summaries:
                lines.append("\n【L1摘要】")
                for s in summaries[-5:]:
                    ts = datetime.fromtimestamp(s.get("ts", 0)).strftime("%m-%d %H:%M")
                    lines.append(f"[{ts}] {s.get('text', '')}")
            digests = self.recall_memory._digests
            if digests:
                lines.append("\n【L2浓缩】")
                for d in digests[-3:]:
                    ts = datetime.fromtimestamp(d.get("ts", 0)).strftime("%m-%d %H:%M")
                    lines.append(f"[{ts}] {d.get('text', '')}")
            yield event.plain_result("\n".join(lines))
            return

        if not self._enable_core_memory:
            yield event.plain_result("核心记忆未启用")
            return

        if target:
            profile = None
            for uid, p in self.core_memory._profiles.items():
                if uid == target or p.get("name", "") == target:
                    profile = (uid, p)
                    break
            if not profile:
                yield event.plain_result(f"没有找到「{target}」的记忆")
                return
            uid, p = profile
            lines = [f"【{p.get('name', uid)}】(QQ{uid})"]
            if p.get("relationship"):
                lines.append(f"印象: {p['relationship']}")
            if p.get("traits"):
                lines.append(f"特征: {'、'.join(p['traits'])}")
            if p.get("important_events"):
                lines.append("重要事件:")
                for ev in p["important_events"]:
                    lines.append(f"  - {ev}")
            updated = p.get("updated_at")
            if updated:
                lines.append(f"更新于: {datetime.fromtimestamp(updated).strftime('%Y-%m-%d %H:%M')}")
            if self._enable_affinity:
                composite = self.affinity.get_composite(uid)
                level = self.affinity.get_level(uid)
                lines.append(f"好感度: {level}({composite:.0f}/100)")
            yield event.plain_result("\n".join(lines))
            return

        lines = ["【琪露诺的记忆】"]
        for uid, p in self.core_memory._profiles.items():
            name = p.get("name", uid)
            rel = p.get("relationship", "")
            suffix = f" — {rel}" if rel else ""
            if self._enable_affinity:
                level = self.affinity.get_level(uid)
                suffix += f" [{level}]"
            lines.append(f"· {name}(QQ{uid}){suffix}")
        lines.append(f"\n共 {self.core_memory.profile_count} 人")
        lines.append("用法: 琪露诺记忆 <名字/QQ号> | 琪露诺记忆 回忆")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("琪露诺记忆管理")
    async def manage_memory(self, event: AstrMessageEvent, action: str = "", target: str = ""):
        if not self._enable_core_memory:
            yield event.plain_result("核心记忆未启用")
            return
        action = action.strip()
        target = target.strip()
        if not action:
            yield event.plain_result(
                "用法:\n"
                "琪露诺记忆管理 清除印象 <名字/QQ号>\n"
                "琪露诺记忆管理 清除事件 <名字/QQ号>\n"
                "琪露诺记忆管理 清除全部 <名字/QQ号>\n"
                "琪露诺记忆管理 删除 <名字/QQ号>\n"
                "琪露诺记忆管理 全部清除印象\n"
                "琪露诺记忆管理 全部清除事件"
            )
            return
        if action in ("全部清除印象", "全部清除事件"):
            count = 0
            for p in self.core_memory._profiles.values():
                if action == "全部清除印象" and p.get("relationship"):
                    p["relationship"] = ""
                    p["traits"] = []
                    count += 1
                elif action == "全部清除事件" and p.get("important_events"):
                    p["important_events"] = []
                    count += 1
            await self.core_memory.save()
            field = "印象" if "印象" in action else "事件"
            yield event.plain_result(f"已清除 {count} 人的{field}")
            return
        if not target:
            yield event.plain_result("缺少目标，请指定名字或QQ号")
            return
        profile = None
        for uid, p in self.core_memory._profiles.items():
            if uid == target or p.get("name", "") == target:
                profile = (uid, p)
                break
        if not profile:
            yield event.plain_result(f"没有找到「{target}」")
            return
        uid, p = profile
        name = p.get("name", uid)
        if action == "清除印象":
            p["relationship"] = ""
            p["traits"] = []
            await self.core_memory.save()
            yield event.plain_result(f"已清除{name}的印象和特征")
        elif action == "清除事件":
            p["important_events"] = []
            await self.core_memory.save()
            yield event.plain_result(f"已清除{name}的重要事件")
        elif action == "清除全部":
            p["relationship"] = ""
            p["traits"] = []
            p["important_events"] = []
            await self.core_memory.save()
            yield event.plain_result(f"已清除{name}的所有记忆（保留名字和背景设定）")
        elif action == "删除":
            del self.core_memory._profiles[uid]
            await self.core_memory.save()
            yield event.plain_result(f"已删除{name}(QQ{uid})的档案")
        else:
            yield event.plain_result(f"未知操作: {action}")

    @filter.command("琪露诺笔记")
    async def debug_notes(self, event: AstrMessageEvent):
        if not self._global_notes:
            yield event.plain_result("笔记本是空的，还没有记住任何事")
            return
        lines = [f"【琪露诺的笔记】共 {len(self._global_notes)} 条"]
        for i, note in enumerate(self._global_notes, 1):
            lines.append(f"{i}. {note}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("琪露诺删笔记")
    async def delete_note(self, event: AstrMessageEvent, index: str = ""):
        index = index.strip()
        if not index:
            yield event.plain_result("用法: 琪露诺删笔记 <序号>\n用「琪露诺笔记」查看序号")
            return
        try:
            idx = int(index) - 1
        except ValueError:
            yield event.plain_result("序号必须是数字")
            return
        if idx < 0 or idx >= len(self._global_notes):
            yield event.plain_result(f"序号超出范围，当前共 {len(self._global_notes)} 条")
            return
        removed = self._global_notes.pop(idx)
        await self.put_kv_data("global_notes", self._global_notes)
        yield event.plain_result(f"已删除: {removed}")

    async def terminate(self):
        await self.put_kv_data("state_data", self.state_manager.to_dict())
        await self.put_kv_data("group_sessions", list(self._group_sessions))
        if self._enable_affinity:
            await self.affinity.save()
        if self._enable_core_memory:
            await self.core_memory.save()
        if self._enable_recall_memory:
            await self.recall_memory.save()
        if self._cron_job_id:
            try:
                await self.context.cron_manager.delete_job(self._cron_job_id)
            except Exception:
                pass
        if self._daily_profile_cron_id:
            try:
                await self.context.cron_manager.delete_job(self._daily_profile_cron_id)
            except Exception:
                pass
        logger.info("琪露诺状态系统已保存并清理")
