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
from .meme_sender import MemeSelector
from .recall_memory import RecallMemory
from .state_manager import CirnoStateManager

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
        self._last_full_prompt: str = ""

    async def initialize(self):
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

        if self._enable_affinity:
            await self.affinity.load()
        if self._enable_core_memory:
            await self.core_memory.load()
        if self._enable_recall_memory:
            self.recall_memory.set_llm_generate(self._recall_llm_generate)
            if self._enable_affinity:
                self.recall_memory.set_key_event_callback(self._on_buffer_key_event)
            await self.recall_memory.load()

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

    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        self.state_manager.on_user_interaction()
        self.state_manager.maybe_transition()

        if event.session.message_type == MessageType.GROUP_MESSAGE:
            umo = event.unified_msg_origin
            if umo not in self._group_sessions:
                self._group_sessions.add(umo)
                await self.put_kv_data(
                    "group_sessions", list(self._group_sessions)
                )

        sender_id = str(event.get_sender_id())
        sender_nickname = event.get_sender_name()
        logger.info(
            f"[琪露诺触发] 用户={sender_nickname}({sender_id}), "
            f"状态={self.state_manager.current_state}, "
            f"消息={event.message_str[:50] if event.message_str else ''}"
        )

        if self._enable_core_memory:
            user_msg_text = event.message_str or ""
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
        if self._enable_recall_memory and not suppress_recall:
            user_msg = event.message_str or ""
            if user_msg:
                memories = self.recall_memory.search(
                    user_msg, current_user_id=sender_id
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
                if recall_prompt:
                    req.system_prompt += f"\n{recall_prompt}"

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
            req.system_prompt += self.affinity.build_mood_prompt()
            req.system_prompt += self.affinity.build_status_prompt(sender_id)
            req.system_prompt += self.affinity.build_rating_prompt()

        if has_recall:
            req.system_prompt += (
                "\n如果对方聊的话题和你记忆中的内容有关，你可以自然地提起你还记得之前聊过的事。"
                "不要生硬地复述记忆内容，而是像真的想起来了一样随口带一嘴。"
            )

        req.system_prompt += ABSOLUTE_RULES
        self._last_full_prompt = req.system_prompt

        await self.put_kv_data("state_data", self.state_manager.to_dict())

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name()
        user_msg = event.message_str or ""
        bot_reply = resp.completion_text or ""

        bot_reply = re.sub(r"[（(][^）)]*[）)]", "", bot_reply).strip()
        bot_reply = re.sub(r"\*[^*]+\*", "", bot_reply).strip()
        if bot_reply != (resp.completion_text or ""):
            resp.completion_text = bot_reply

        if self._enable_affinity and bot_reply:
            cleaned, valence_shift, reason = self.affinity.extract_inner(bot_reply)
            if cleaned != bot_reply:
                resp.completion_text = cleaned
                bot_reply = cleaned

            if valence_shift is not None:
                from .cirno_states import CIRNO_STATES
                cat = CIRNO_STATES.get(self.state_manager.current_state, {}).get("category", "")
                self.affinity.update_emotion(valence_shift, cat)
                self.affinity.update_affinity(sender_id, valence_shift)
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
            await self.recall_memory.archive(sender_id, sender_name, user_msg, bot_reply)
            logger.info(f"[琪露诺回忆归档] {sender_name}({sender_id}): {user_msg[:30]}")

        if self._enable_core_memory:
            is_known = self.core_memory.get_profile(sender_id) is not None
            if is_known or self._allow_stranger_profile:
                count = self.core_memory.get_interaction_count(sender_id)
                if self.core_memory.should_update(sender_id):
                    logger.info(
                        f"[琪露诺核心记忆] 触发LLM更新 {sender_name}({sender_id}), "
                        f"交互计数={count}/{self.core_memory.update_threshold}"
                    )
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
                events.append(result["memory"])
                profile["important_events"] = events[-3:]
                await self.core_memory.save()
                logger.info(f"[琪露诺关键事件] 写入核心记忆: {result['memory']}")

        await self.affinity.save()

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
            cleaned, _, _ = self.affinity.extract_inner(text)
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
            if level in ("讨厌", "冷淡"):
                pool = self.POKE_RESPONSES["negative"]
            elif level in ("喜欢", "很喜欢", "最好的朋友"):
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
        logger.info("琪露诺状态系统已保存并清理")
