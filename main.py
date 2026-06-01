import asyncio
import json
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
from .heart_flow import HeartFlow
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
        self._prank_duration_turns = affinity_cfg.get("prank_duration_turns", 5)

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
        self._imitation_state: dict[str, dict] = {}  # session_id -> state
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
        self._recent_bot_replies: list[dict] = []
        self._dirty: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._diag_task: asyncio.Task | None = None
        self._session_last_seen: dict[str, float] = {}  # umo -> 最后收到消息时间
        self.jargon_filter = JargonStatisticalFilter()
        self._fact_writeback_cooldown: int = memory_cfg.get("fact_writeback_cooldown", 120)
        self._fact_writeback_last: dict[str, float] = {}
        self.heart_flow = HeartFlow()
        profile_cfg = config.get("profile_settings", {})
        self._enable_nickname_sync = profile_cfg.get("enable_nickname_sync", False)
        self._enable_signature_sync = profile_cfg.get("enable_signature_sync", False)
        self._nickname_prefix = profile_cfg.get("nickname_prefix", "最强的琪露诺！")
        self._enable_qzone_post = profile_cfg.get("enable_qzone_post", False)
        self._qzone_last_post_ts: float = 0.0
        self._cached_bot = None
        self._cached_weather: str = ""
        self._weather_last_fetch: float = 0.0
        self._poke_streaks: dict[str, dict] = {}
        self._private_last_user_msg: dict[str, float] = {}
        self._private_followup_tasks: dict[str, asyncio.Task] = {}

        private_cfg = config.get("private_chat_settings", {})
        self._enable_private_proactive = private_cfg.get("enable", False)
        self._private_targets: list[dict] = []
        if self._enable_private_proactive:
            for line in private_cfg.get("targets", "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.rsplit(":", 1)
                if len(parts) == 2:
                    self._private_targets.append({
                        "session": parts[0].strip(),
                        "user_id": parts[1].strip(),
                    })
        self._private_min_idle = private_cfg.get("min_idle_hours", 24) * 3600

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

        saved_qzone_ts = await self.get_kv_data("qzone_last_post_ts", None)
        if saved_qzone_ts:
            self._qzone_last_post_ts = float(saved_qzone_ts)

        if self._enable_core_memory:
            daily_job = await self.context.cron_manager.add_basic_job(
                name="cirno_daily_profile_update",
                cron_expression="0 3 * * *",
                handler=self._daily_profile_update,
                description="琪露诺每日用户画像更新",
            )
            self._daily_profile_cron_id = daily_job.job_id
            logger.info("琪露诺每日用户画像 cron job 已注册，每日凌晨3点触发")

        self._flush_task = self._spawn(self._flush_loop(), "flush_loop")
        self._diag_task = self._spawn(self._diag_loop(), "diag_loop")


    _CATEGORY_QQ_STATUS = {
        "rest":      (10, 1016),  # 睡觉
        "rare":      (10, 1028),  # 听歌
        "social":    (60, 0),     # Q我吧
    }

    async def _sync_qq_status(self, bot):
        from .cirno_states import CIRNO_STATES
        state = CIRNO_STATES.get(self.state_manager.current_state, {})
        cat = state.get("category", "")
        label = state.get("label", "")

        status, ext_status = self._CATEGORY_QQ_STATUS.get(cat, (10, 0))
        try:
            await bot.call_action("set_online_status", status=status, ext_status=ext_status, battery_status=0)
            logger.info(f"[琪露诺状态] QQ在线状态已同步: category={cat} status={status} ext={ext_status}")
        except Exception as e:
            logger.debug(f"[琪露诺状态] QQ在线状态同步失败: {e}")

        if self._enable_nickname_sync or self._enable_signature_sync:
            nickname = self._nickname_prefix if self._nickname_prefix else f"琪露诺（{label}）"
            signature = label if self._enable_signature_sync else None
            try:
                kwargs = {}
                if self._enable_nickname_sync:
                    kwargs["nickname"] = nickname
                if self._enable_signature_sync:
                    kwargs["personal_note"] = signature
                if kwargs:
                    await bot.call_action("set_qq_profile", **kwargs)
                    logger.info(f"[琪露诺状态] QQ资料已同步: {kwargs}")
            except Exception as e:
                logger.debug(f"[琪露诺状态] QQ资料同步失败: {e}")

    def _replace_at_with_names(self, text: str) -> str:
        def _repl(m):
            qq = m.group(2)
            p = self.core_memory.get_profile(qq)
            if p:
                return f"@{p.get('name', m.group(1))}"
            return m.group(0)
        return re.sub(r"@([^(]+)\((\d+)\)", _repl, text)

    def _spawn(self, coro, label: str):
        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[琪露诺后台任务异常] {label}: {e}", exc_info=True)
        return asyncio.create_task(_wrapped())

    _IMG_BLOCK_RE = re.compile(r"\[Image:\s*.*?\]", re.DOTALL)
    _PAREN_RE = re.compile(r"[（(][^）)]*[）)]", re.DOTALL)
    _STAR_RE = re.compile(r"\*[^*]+\*")

    @classmethod
    def _fold_images(cls, text: str) -> str:
        return cls._IMG_BLOCK_RE.sub("[图片]", text)

    @classmethod
    def _strip_roleplay(cls, text: str) -> str:
        """删掉 bot 历史回复里的括号动作戏/星号动作，防止它在上下文里反向示范。"""
        t = cls._PAREN_RE.sub("", text)
        t = cls._STAR_RE.sub("", t)
        t = re.sub(r"\n{2,}", "\n", t)
        return re.sub(r"[ \t]+", " ", t).strip()

    def _shrink_context(self, req):
        # 文本里的 [Image: 转述] 块全部折叠成 [图片]。
        # 用户当前发给 bot 看的主图走 req.image_urls（真图），不在文本里，不受影响。
        if getattr(req, "prompt", None) and "[Image:" in req.prompt:
            req.prompt = self._fold_images(req.prompt)
        if not req.contexts:
            return
        for msg in req.contexts:
            is_assistant = msg.get("role") == "assistant"
            c = msg.get("content")
            if isinstance(c, str):
                if "[Image:" in c:
                    c = self._fold_images(c)
                if is_assistant and ("（" in c or "(" in c or "*" in c):
                    c = self._strip_roleplay(c)
                msg["content"] = c
            elif isinstance(c, list):
                for item in c:
                    if not (isinstance(item, dict) and item.get("type") == "text"):
                        continue
                    t = item.get("text", "")
                    if "[Image:" in t:
                        t = self._fold_images(t)
                    if is_assistant and ("（" in t or "(" in t or "*" in t):
                        t = self._strip_roleplay(t)
                    item["text"] = t

    def mark_dirty(self, *names: str):
        self._dirty.update(names)

    async def _flush_dirty(self):
        if not self._dirty:
            return
        dirty = self._dirty
        self._dirty = set()
        try:
            if "affinity" in dirty:
                await self.affinity.save()
            if "recall" in dirty:
                await self.recall_memory.save()
            if "core" in dirty:
                await self.core_memory.save()
        except Exception as e:
            self._dirty.update(dirty)
            logger.error(f"[琪露诺落盘失败] {e}", exc_info=True)

    _PRIVATE_REF_KEYWORDS = ("私聊", "私下", "私底下", "刚跟你说", "刚才跟你说", "之前跟你说",
                             "我们私", "悄悄跟你", "单独跟你", "私信")

    async def _fetch_private_history(self, event: AstrMessageEvent, sender_id: str, max_turns: int = 4) -> str:
        """拉取该用户私聊会话的最近几轮原文，用于群里提到私聊时附加上下文。"""
        try:
            group_umo = event.unified_msg_origin
            platform = group_umo.split(":", 1)[0]
            private_umo = f"{platform}:FriendMessage:{sender_id}"
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(private_umo)
            if not cid:
                return ""
            conv = await cm.get_conversation(private_umo, cid)
            if not conv or not conv.history:
                return ""
            history = json.loads(conv.history)
        except Exception as e:
            logger.debug(f"[私聊历史] 拉取失败: {e}")
            return ""

        def _text(c):
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text")
            return ""

        lines = []
        for m in history[-max_turns * 2:]:
            role = m.get("role")
            t = _text(m.get("content"))
            t = re.sub(r"<inner>.*?</inner>", "", t, flags=re.DOTALL)
            t = re.sub(r"<system_reminder>.*", "", t, flags=re.DOTALL)
            t = re.sub(r"<Quoted Message>.*?</Quoted Message>", "", t, flags=re.DOTALL)
            t = self._fold_images(t).strip()
            if not t:
                continue
            who = "他" if role == "user" else "你"
            lines.append(f"{who}：{t[:60]}")
        return "\n".join(lines[-max_turns * 2:])

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(30)
            await self._flush_dirty()

    def _collect_diagnostics(self) -> str:
        """采集事件循环+会话状态快照，用于定位群聊卡死。"""
        import traceback as _tb
        lines = []
        now = time.time()
        try:
            tasks = [t for t in asyncio.all_tasks() if not t.done()]
        except Exception as e:
            return f"[诊断] 无法获取 task 列表: {e}"

        # 按「当前卡在哪个函数」分组
        buckets: dict[str, int] = {}
        suspicious = []  # 卡在 OneBot / DB 的 task
        for t in tasks:
            where = "?"
            try:
                stack = t.get_stack(limit=1)
                if stack:
                    f = stack[0]
                    where = f"{f.f_code.co_name}({f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno})"
                    fname = f.f_code.co_filename
                    if any(k in fname for k in ("aiocqhttp", "api_impl", "sqlalchemy", "aiosqlite")) \
                       or f.f_code.co_name in ("call_action", "fetch", "_query", "text_chat"):
                        suspicious.append(where)
            except Exception:
                pass
            buckets[where] = buckets.get(where, 0) + 1

        lines.append(f"[诊断] 时刻={time.strftime('%H:%M:%S')} 挂起task总数={len(tasks)}")
        if suspicious:
            lines.append(f"[诊断] ⚠️ 疑似卡在OneBot/DB的task {len(suspicious)}个: " + "; ".join(suspicious[:8]))
        top = sorted(buckets.items(), key=lambda x: -x[1])[:8]
        lines.append("[诊断] task分布(前8): " + " | ".join(f"{w}×{n}" for w, n in top))

        # 各会话最后活动（按群/私聊分，找"很久没动"的群）
        groups = [(umo, now - ts) for umo, ts in self._session_last_seen.items() if "GroupMessage" in umo]
        privs = [(umo, now - ts) for umo, ts in self._session_last_seen.items() if "GroupMessage" not in umo]
        groups.sort(key=lambda x: x[1])
        if groups:
            g_str = ", ".join(f"{u.split(':')[-1]}={int(d)}s前" for u, d in groups[:6])
            lines.append(f"[诊断] 群最后活动({len(groups)}个): {g_str}")
        if privs:
            recent_priv = min(d for _, d in privs)
            lines.append(f"[诊断] 私聊({len(privs)}个) 最近一条={int(recent_priv)}s前")
        return "\n".join(lines)

    async def _diag_loop(self):
        import os
        diag_path = os.path.join(
            str(StarTools.get_data_dir("astrbot_plugin_cirno")), "stuck_diagnostics.log"
        )
        while True:
            await asyncio.sleep(300)  # 每5分钟一次静默快照
            try:
                snap = self._collect_diagnostics()
                with open(diag_path, "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{snap}\n")
            except Exception as e:
                logger.debug(f"[诊断] 写快照失败: {e}")

    _ERROR_FALLBACKS = [
        "哼，本天才刚才走神了，没听清你说啥，再说一遍！",
        "唔……脑子突然冻住了，刚才你说什么来着？",
        "啊——咱刚才在想别的事，没顾上你，再讲一次嘛！",
        "诶？信号被雾之湖的雾挡住啦，再说一遍！",
        "本天才正忙着结冰呢，等会儿再理你！",
    ]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _arm_error_fallback(self, event: AstrMessageEvent):
        # 最早期就设兜底错误消息，覆盖「读对话历史时 SQL 锁炸了」这种
        # 早于 on_llm_request 的失败——那时 inject_prompt 还没机会执行。
        event.set_extra(
            "persona_custom_error_message",
            random.choice(self._ERROR_FALLBACKS),
        )
        # 记录会话活动时间，供卡死诊断用
        self._session_last_seen[event.unified_msg_origin] = time.time()

    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        if (event.message_str or "").startswith("//"):
            event.stop_event()
            return
        event.set_extra("cirno_llm_start", time.time())
        self._shrink_context(req)
        bot = getattr(event, "bot", None)
        if bot:
            self._cached_bot = bot
        self.state_manager.on_user_interaction()
        transitioned = self.state_manager.maybe_transition()
        if transitioned:
            await self.put_kv_data("state_data", self.state_manager.to_dict())
            if bot:
                self._spawn(self._sync_qq_status(bot), "sync_qq_status")

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

        user_msg_text = event.message_str or ""
        if event.session.message_type != MessageType.GROUP_MESSAGE:
            self._private_last_user_msg[sender_id] = time.time()
            old_task = self._private_followup_tasks.pop(sender_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
        if self._enable_core_memory:
            user_msg_text = self._replace_at_with_names(user_msg_text)

        _CRITIQUE_KEYWORDS = ("评价一下", "评价下", "点评一下", "点评下", "你怎么看", "怎么看这", "锐评", "锐评一下")
        if self._critique_state is None and any(kw in user_msg_text for kw in _CRITIQUE_KEYWORDS):
            self._critique_state = {"topic": user_msg_text[:100]}
            self._prank_state = None
            logger.info(f"[琪露诺锐评] 触发，话题：{user_msg_text[:40]}")

        from .cirno_states import CIRNO_STATES
        current_category = CIRNO_STATES.get(
            self.state_manager.current_state, {}
        ).get("category", "")
        suppress_recall = current_category == "rest"

        # 1. HeartFlow/TimingGate 门控（随机插嘴场景）
        is_random_reply = (
            not event.is_at_or_wake_command
            and event.session.message_type == MessageType.GROUP_MESSAGE
        )
        is_private = event.session.message_type != MessageType.GROUP_MESSAGE
        session_id = event.unified_msg_origin
        if is_random_reply:
            self.heart_flow.update(session_id, user_msg_text)
            interest = self.heart_flow.get_interest(session_id)
            if not self.heart_flow.should_engage(session_id, base_chance=1.0):
                logger.info(f"[HeartFlow] 兴趣度={interest:.2f}，放弃随机插嘴")
                event.stop_event()
                return
            logger.info(f"[HeartFlow] 兴趣度={interest:.2f}，继续插嘴")

        _pb: list[tuple[str, int]] = []
        _plen = [0]

        def _snap(label: str):
            now = len(req.system_prompt or "")
            delta = now - _plen[0]
            if delta:
                _pb.append((label, delta))
            _plen[0] = now

        _snap("persona基底")

        # 2. 状态机
        req.system_prompt += f"\n{self.state_manager.get_prompt_injection()}"
        _snap("状态机")

        # 3. 对话者身份 + 好感度（放在一起，让 LLM 先建立对"这个人"的完整认知）
        if self._enable_core_memory:
            req.system_prompt += self.core_memory.build_sender_prompt(sender_id, sender_nickname)
            self.core_memory.record_interaction(sender_id)
        else:
            req.system_prompt += f"\n当前和你对话的人QQ号是{sender_id}，QQ昵称是「{sender_nickname}」。"
        _snap("对话者身份")

        if self._enable_affinity:
            req.system_prompt += self.affinity.build_status_prompt(sender_id)
            composite = self.affinity.get_composite(sender_id)
            if composite >= 76:
                ud = self.affinity.get_user_data(sender_id)
                idle_hours = (time.time() - ud.get("last_ts", time.time())) / 3600
                if idle_hours >= 12:
                    hint = "心里其实有点想这个人，虽然嘴上不会说" if idle_hours < 48 else "好久没见到这个人了，心里有种说不出的复杂感觉"
                    req.system_prompt += f"\n【久别】已经{int(idle_hours)}小时没和这个人说话了，{hint}。"
                warmth = self.affinity.get_warmth(sender_id)
                if warmth is not None and warmth < 0.4:
                    req.system_prompt += "\n【察觉】最近几次互动你感觉对方状态不太对劲，可以主动关心一下，不需要等对方开口——但用琪露诺的方式，别太直接。"
        _snap("好感度+久别察觉")

        # 4. 相关的人
        if self._enable_core_memory:
            people_prompt = self.core_memory.build_people_prompt(user_msg_text, sender_id)
            if people_prompt:
                req.system_prompt += f"\n{people_prompt}"
        else:
            req.system_prompt += "\n你认识一些人，但现在记忆模糊。"
        _snap("相关的人")

        from .lore_characters import match_lore
        lore_hits = match_lore(user_msg_text)
        if lore_hits:
            req.system_prompt += "\n【你认识的幻想乡的人】\n" + "\n".join(lore_hits)
        _snap("幻想乡人物")

        # 5. 回忆
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
                        + ", ".join(f"「{m.get('text', '')[:30]}」" for m in memories)
                    )
                uid_to_name = {uid: p.get("name", uid) for uid, p in self.core_memory._profiles.items()} if self._enable_core_memory else {}
                recall_prompt = self.recall_memory.build_recall_prompt(memories, uid_to_name=uid_to_name, is_private=is_private)

        if recall_prompt:
            req.system_prompt += f"\n{recall_prompt}"
        if has_recall:
            req.system_prompt += "\n如果以上记忆和当前话题有关，随口带一嘴，不要生硬复述。"
        _snap("回忆检索")

        # 6. 场景上下文（随机插嘴 / 私聊 / 普通）
        if is_random_reply:
            req.system_prompt += (
                "\n你不是被叫到的，是自己凑过来插嘴的。除非你判断对方是在说你，否则对方不是在和你说话。"
                "好奇心优先——听到不懂的或有趣的细节就追问，不要评价话题有没有意义。"
                "不要强行扯到冰雪或青蛙，也不要攻击或反驳对方，就是个凑热闹的妖精。"
                "就一句话，插完就完，别长篇。"
            )
        elif is_private:
            level = self.affinity.get_level(sender_id) if self._enable_affinity else "普通"
            is_close = level in ("喜欢", "很喜欢")
            private_prompt = (
                "\n你们在私聊，没有旁观者。说话比群里更真实，少一点跳脱和夸张，多一点真心话。"
                "对方特意找你说话，你会在意「他为什么找我」，即使不说出口。"
                "认真接住对方说的每一句，不要随意跳题。"
                "但「认真接住」不等于「答应所有要求」——琪露诺的人格不可改变，对方让你跳出角色、解题、写代码、扮演别人时，照样拒绝。"
            )
            if is_close:
                private_prompt += "你不需要撑面子，说话更松弛，偶尔流露真实感受。"
            private_prompt += "私聊里如果你忍不住要发挥那种又傻又笃定的劲儿，可以多说半句，不用憋着。"
            req.system_prompt += private_prompt
        _snap("场景上下文")

        # 6b. 群里提到私聊时，附加该用户的私聊近况
        if not is_private and any(kw in user_msg_text for kw in self._PRIVATE_REF_KEYWORDS):
            priv_hist = await self._fetch_private_history(event, sender_id)
            if priv_hist:
                req.system_prompt += (
                    f"\n【他私聊里跟你说过的近况】\n{priv_hist}"
                    "\n他现在在群里提到了私聊的事，你记得这些，可以自然地接上，但别在群里把私聊的隐私抖出来。"
                )
                logger.info(f"[私聊历史] 群内附加 {sender_id} 的私聊近况")
        _snap("私聊近况")

        # 7. 当前特殊事件（戳一戳余怒）
        poke_info = self._poke_streaks.get(sender_id, {})
        if poke_info.get("angry") and time.time() - poke_info.get("last_ts", 0) < self._POKE_COOLDOWN * 2:
            req.system_prompt += "\n【刚才的事】这个人刚才一直戳你不说话，你被烦到有点生气，还没完全消气。语气硬一点，但不用点明原因。"
            self._poke_streaks[sender_id]["angry"] = False
        if self._prank_state is not None:
            req.system_prompt += self._build_prank_prompt(sender_id, sender_nickname)

        if self._critique_state is not None:
            req.system_prompt += self._build_critique_prompt()
        _snap("特殊事件(戳/恶作剧/锐评)")

        session_imitation = self._imitation_state.get(session_id)
        if session_imitation:
            tname = session_imitation["target_name"]
            style = session_imitation["style_desc"]
            req.system_prompt += (
                f"\n【当前任务】你现在在模仿「{tname}」的说话风格。"
                f"你仍然是琪露诺，有琪露诺的记忆和性格，但你说话的方式、语气、用词习惯要尽量像{tname}。"
                f"\n{tname}的说话风格特点：\n{style}"
                f"\n模仿时：保留琪露诺的思维方式和情感，但把表达方式换成{tname}的风格。"
                f"不要在回复中说「我在模仿{tname}」，直接用那个风格说话。"
            )
        _snap("模仿")
        if not is_private:
            slang_matches = self.slang_store.match(event.message_str or "")
            if slang_matches:
                slang_lines = "\n".join(
                    f'「{e["word"]}」：{e["meaning"]}，可以自然地用在合适的场合。'
                    for e in slang_matches
                )
                req.system_prompt += f"\n【群里的说法】\n{slang_lines}"
        _snap("群黑话")
        if self._global_notes:
            from .recall_memory import extract_keywords
            query_kw = set(extract_keywords(user_msg_text))
            if query_kw:
                matched = [n for n in self._global_notes if query_kw & set(extract_keywords(n))][:3]
            else:
                matched = []
            if matched:
                notes_text = "\n".join(f"- {n}" for n in matched)
                req.system_prompt += f"\n【你特意记下来的事】\n{notes_text}"
        _snap("特意记下的事")
        if self._recent_bot_replies:
            recent_lines = []
            for r in self._recent_bot_replies:
                text = r["text"]
                to = r["to"]
                snippet = f"「{text[:30]}…」" if len(text) > 30 else f"「{text}」"
                recent_lines.append(f"对{to}说过{snippet}")
            req.system_prompt += f"\n【你最近说过】{'、'.join(recent_lines)}——避免重复相同的开场白、句式和结尾。"
        _snap("最近说过")
        req.system_prompt += ABSOLUTE_RULES
        _snap("绝对规则")
        if self._enable_affinity:
            req.system_prompt += self.affinity.build_rating_prompt()
        _snap("评分指令")

        total = len(req.system_prompt or "")
        block_str = " | ".join(f"{lbl}:{n}" for lbl, n in _pb)

        def _msg_len(c):
            if isinstance(c, str):
                return len(c)
            if isinstance(c, list):
                return sum(len(i.get("text", "")) for i in c if isinstance(i, dict) and i.get("type") == "text")
            return 0
        all_ctx = req.contexts or []
        ctx_turns = len(all_ctx)
        # 框架会在发送前把历史截断到 max_context_length（默认6轮=12条消息），
        # 我们的钩子在截断之前，拿到的是全量历史。这里只统计「实际会发送」的尾部，
        # 避免被未截断的全量历史误导。
        send_window = 6 * 2
        sent_ctx = all_ctx[-send_window:]
        sent_chars = sum(_msg_len(m.get("content")) for m in sent_ctx)
        prompt_chars = len(req.prompt or "")
        grand_total = total + sent_chars + prompt_chars
        logger.info(
            f"[琪露诺Prompt体检] system={total} + 实发历史={sent_chars}(尾{len(sent_ctx)}条/全{ctx_turns}条) + 当前={prompt_chars} "
            f"= 实发约{grand_total}字符(约{grand_total*2//3}token) | system分块: {block_str}"
        )
        if self._enable_core_memory and req.prompt:
            req.prompt = self._replace_at_with_names(req.prompt)

        parts = [
            f"=== 体检：总长 {total} 字符，{len(_pb)} 块 ===\n{block_str}",
            f"\n=== SYSTEM PROMPT ===\n{req.system_prompt}",
        ]
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

        bot_reply = re.sub(r"[（(][^）)]*[）)]", "", bot_reply, flags=re.DOTALL).strip()
        bot_reply = re.sub(r"\*[^*]+\*", "", bot_reply).strip()
        bot_reply = re.sub(r"\n{2,}", "\n", bot_reply)
        # 清除不完整的 <inner> 标签（缺少结束标签时正则无法匹配）
        bot_reply = re.sub(r"<inner>.*", "", bot_reply, flags=re.DOTALL).strip()
        # 检测 LaTeX / 代码块 / 跳出角色的助手腔，替换为拒绝
        if re.search(r"\\\[|\\\(|\\frac|\\sum|\\lim|\\sqrt|```|\\begin\{", bot_reply):
            logger.warning(f"[琪露诺] 检测到 LaTeX/代码输出，替换为拒绝: {bot_reply[:50]}")
            bot_reply = random.choice([
                "哼，那种麻烦的东西本天才才不屑做！",
                "什么乱七八糟的符号啊！不懂！",
                "你找错人啦，最强的我可不是用来算题的！",
            ])
        if not bot_reply.strip():
            bot_reply = random.choice(["哼。", "……怎么了？", "嗯？"])
        if bot_reply != (resp.completion_text or ""):
            resp.completion_text = bot_reply

        valence_shift: float | None = None
        interaction_type: str | None = None
        if self._enable_affinity and bot_reply:
            cleaned, valence_shift, reason, interaction_type = self.affinity.extract_inner(bot_reply)
            if cleaned != bot_reply:
                if not cleaned.strip():
                    cleaned = random.choice(["哼。", "……怎么了？", "嗯？"])
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

        self._recent_bot_replies.append({"text": bot_reply[:80], "to": sender_name})
        if len(self._recent_bot_replies) > 5:
            self._recent_bot_replies.pop(0)
        self.heart_flow.on_bot_reply(event.unified_msg_origin)

        is_private_chat = event.session.message_type != MessageType.GROUP_MESSAGE
        if is_private_chat and self._enable_private_proactive:
            self._private_last_user_msg[sender_id] = time.time()
            old_task = self._private_followup_tasks.pop(sender_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
            task = self._spawn(
                self._private_followup_flow(
                    sender_id, sender_name, bot_reply,
                    event.unified_msg_origin
                ),
                "private_followup_flow",
            )
            self._private_followup_tasks[sender_id] = task

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
                self._spawn(self._slang_update(), "slang_update")

        if self._enable_core_memory and "记住" in bot_reply:
            self._spawn(self._extract_and_memorize(
                sender_id, sender_name, user_msg, bot_reply
            ), "extract_and_memorize")

        if self._enable_core_memory and len(user_msg) > 15:
            cooldown = self._fact_writeback_cooldown
            last = self._fact_writeback_last.get(sender_id, 0)
            if cooldown <= 0 or time.time() - last >= cooldown:
                self._fact_writeback_last[sender_id] = time.time()
                self._spawn(self._writeback_user_facts(
                    sender_id, sender_name, user_msg, bot_reply
                ), "writeback_user_facts")

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
                    self._spawn(self.core_memory.update_profile_via_llm(
                        sender_id, recent_summary, self.context, nickname=sender_name
                    ), "update_profile_via_llm")

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

        if self._prank_state is not None:
            if self._prank_state.get("ending"):
                self._prank_state = None
                logger.info("[琪露诺恶作剧] 收尾完成，恶作剧结束")
            else:
                # 轮数制
                if self._prank_state.get("turns_left") is not None:
                    self._prank_state["turns_left"] -= 1
                    logger.info(f"[琪露诺恶作剧] 剩余轮数={self._prank_state['turns_left']}")
                    if self._prank_state["turns_left"] <= 0:
                        self._prank_state["ending"] = True
                        logger.info("[琪露诺恶作剧] 轮数耗尽，进入收尾")
                # 时间制
                elif self._prank_state.get("expires_at") and time.time() >= self._prank_state["expires_at"]:
                    self._prank_state["ending"] = True
                    logger.info("[琪露诺恶作剧] 恶作剧时间到，进入收尾")

                if not self._prank_state.get("ending") and valence_shift is not None and valence_shift < 0.4:
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
            imitate_style = ""
            records = self.user_msg_store.get_recent(sender_id, limit=30)
            if len(records) >= 30:
                lines = [r["msg"] for r in records if r.get("msg", "").strip()]
                imitate_style = "；".join(lines[:10])
            state = self._start_prank(sender_id, imitate_style=imitate_style)
            turns_left = state.get("turns_left")
            duration_info = f"{turns_left}轮" if turns_left is not None else f"{int(state['expires_at'] - time.time()) // 60}min"
            logger.info(
                f"[琪露诺恶作剧] 进入恶作剧模式! "
                f"valence={valence:.2f} composite={composite:.0f} "
                f"chance={chance:.2%} duration={duration_info} "
                f"pool={state['behavior_pool']} imitate={'有' if imitate_style else '无'}"
            )

    PRANK_BEHAVIORS = [
        "一本正经地深度分析{name}说的话，把最普通的话过度解读成意义深远、暗藏玄机的东西。用琪露诺的口气——傲慢、得意、自以为看穿了一切，时不时冒出「本天才一眼就看出来了」「哼，你以为我不知道吗」这类话。逻辑要有点歪，结论要荒唐但说得理直气壮，不要用学术词汇，要像个自信过头的小孩在胡说八道",
        "根据{name}的名字或说话内容，给他起一个奇怪但有一定逻辑的外号，然后全程叫那个外号，态度理所当然，如果对方反应就解释你的命名理由",
        "故意曲解{name}说的话，理解成完全不同的意思，然后基于错误理解认真回应",
        "假装不认识{name}，用陌生的语气应对，说「你是谁啊」。但要留点破绽——比如不小心叫出对方名字又马上否认，或者说了只有认识对方才会知道的细节，然后假装是猜的。",
        "编造一件{name}最近在群里干的蠢事，描述得绘声绘色像是亲眼目睹",
        "揪住{name}说的话里某个具体的词或细节，反复追问那一个点，不管对方怎么回答都往那个细节上转。越问越细、越问越偏，像是真的对那个细节着了迷，完全不在意对方想说的重点是什么",
        "疯狂附和对方说的话，同意程度极其夸张，好像对方说了什么惊天大道理",
        "假装完全听不懂对方说的话，对非常正常的句子一直追问「什么意思」，对方越解释越装傻",
        "__imitate__",  # 模仿发言者风格，同时自言自语，由代码动态替换
    ]

    def _start_prank(self, triggered_by: str, imitate_style: str = "") -> dict:
        imitate_idx = next((i for i, b in enumerate(self.PRANK_BEHAVIORS) if b == "__imitate__"), None)
        eligible = list(range(len(self.PRANK_BEHAVIORS)))
        if imitate_idx is not None and not imitate_style:
            eligible.remove(imitate_idx)
        used = random.sample(eligible, min(4, len(eligible)))
        base = {
            "triggered_by": triggered_by,
            "behavior_pool": used,
            "escalation": 0,
            "ending": False,
            "imitate_style": imitate_style,
        }
        if self._prank_duration_turns > 0:
            self._prank_state = {**base, "turns_left": self._prank_duration_turns, "expires_at": None}
        else:
            self._prank_state = {**base, "turns_left": None, "expires_at": time.time() + random.randint(10, 20) * 60}
        return self._prank_state

    def _build_prank_prompt(self, sender_id: str, sender_name: str) -> str:
        if self._prank_state.get("ending"):
            return (
                "\n【恶作剧刚结束】你刚才在搞恶作剧，现在悄悄收手了。"
                "这条回复假装什么都没发生，自然地回到正常状态，不要解释。"
            )
        pool = self._prank_state.get("behavior_pool", [0])
        idx = random.choice(pool)
        raw = self.PRANK_BEHAVIORS[idx % len(self.PRANK_BEHAVIORS)]

        if raw == "__imitate__":
            style = self._prank_state.get("imitate_style", "")
            if style:
                behavior = (
                    f"突然像是觉醒了第二人格——用{sender_name}的说话风格和自己对话。"
                    f"每句回复必须以「……」开头，然后用{sender_name}的语气词、句式说出你琪露诺内心的想法，"
                    f"像是另一个自己在借壳说话，内容是真实的情绪或看法，但表达方式完全是{sender_name}的风格。"
                    f"风格参考：{style[:100]}"
                )
            else:
                pool_no_imitate = [i for i in pool if self.PRANK_BEHAVIORS[i % len(self.PRANK_BEHAVIORS)] != "__imitate__"]
                idx = random.choice(pool_no_imitate) if pool_no_imitate else 0
                raw = self.PRANK_BEHAVIORS[idx % len(self.PRANK_BEHAVIORS)]
                behavior = raw.format(name=sender_name)
        else:
            behavior = raw.format(name=sender_name)
        escalation = self._prank_state.get("escalation", 0)
        escalation_hint = ""
        if escalation >= 2:
            escalation_hint = "对方已经有反应了，你越搞越起劲，变本加厉。"
        turns_left = self._prank_state.get("turns_left")
        if turns_left is not None:
            remaining_hint = f"（剩余约 {turns_left} 轮）"
        else:
            remaining = max(0, int(self._prank_state["expires_at"] - time.time())) // 60
            remaining_hint = f"（剩余约 {remaining} 分钟）"
        return (
            f"\n【恶作剧模式】你现在心情特别好，想搞点事情。这条回复请：{behavior}。"
            f"{escalation_hint}全程保持琪露诺的口气——傲慢、得意、自以为是，不要变成别的风格。"
            "保持自然，像是你真的这么想，不要解释自己在搞恶作剧。这次回复可以比平时长一点。"
            f"{remaining_hint}"
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
            "这次例外，回复可以长一点，至少说三个点，不能敷衍。"
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
            is_neg = result.get("delta", 0) < 0
            importance = max(1, min(10, int(abs(result.get("delta", 0.05)) * 67)))
            await self.core_memory.add_important_event(
                user_id, result["memory"], nickname=nickname,
                is_negative=is_neg, importance=importance
            )
            logger.info(f"[琪露诺关键事件] 写入核心记忆(importance={importance}): {result['memory']}")

        self.mark_dirty("affinity")

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

    async def _timing_gate(self, message: str, sender_name: str) -> bool:
        """Decide whether to interject in a random-reply scenario via a lightweight LLM call.
        Returns True = engage, False = stay silent. Falls back to True on any error."""
        if not message or len(message.strip()) < 3:
            return False

        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            return True

        state_label = self.state_manager.get_prompt_injection()[:30]
        prompt = (
            f"群里有人说：「{message[:80]}」\n"
            f"你现在的状态：{state_label}\n"
            "你（琪露诺）要不要主动插嘴？\n"
            "以下情况输出 no：纯表情包/图片/语音、复读或+1、营销转发内容、话题和你完全无关且没有提到你。\n"
            "以下情况输出 yes：有有趣的细节让你好奇、对方说了什么你想追问、话题涉及你认识的事物。\n"
            "只输出 yes 或 no。"
        )

        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt="你是琪露诺，只输出 yes 或 no。",
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.debug("[TimingGate] 超时，默认不插嘴")
            return False
        except Exception as e:
            logger.debug(f"[TimingGate] LLM 调用失败，默认插嘴: {e}")
            return True

        if not resp or not resp.completion_text:
            return True

        answer = resp.completion_text.strip().lower()
        return not answer.startswith("no")

    _WRITEBACK_SKIP_PATTERNS = ("哈哈", "哦", "好的", "嗯", "啊", "是的", "对啊", "好啊", "没事", "随便", "不知道")

    async def _writeback_user_facts(self, user_id: str, user_name: str, user_msg: str, bot_reply: str):
        if any(user_msg.strip().startswith(p) and len(user_msg.strip()) < 10 for p in self._WRITEBACK_SKIP_PATTERNS):
            return
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            return

        profile = self.core_memory.get_profile(user_id)
        existing_events = self.core_memory._get_events(profile) if profile else []
        existing_hint = f"已知事件（不要重复）：{'；'.join(e['text'] for e in existing_events)}\n" if existing_events else ""

        prompt = (
            f"{existing_hint}"
            f"对话者{user_name}说：「{user_msg}」\n"
            f"琪露诺回答：「{bot_reply[:100]}」\n\n"
            f"任务：从【{user_name}说的话】中提取关于{user_name}本人的稳定事实（兴趣、习惯、经历、身份、偏好等）。\n"
            "严格要求：\n"
            f"- 只提取{user_name}关于自己的明确陈述，不能推测\n"
            "- 不要提取琪露诺的行为、感受或状态\n"
            "- 不要提取玩笑话、反问句、假设性内容\n"
            "- 不要提取关于第三方（大妖精、灵梦等）的描述\n"
            "- 如果{user_name}没有说任何关于自己的稳定事实，输出 null\n"
            f"输出格式：「{user_name}+事实」，不超过20字，或直接输出 null。"
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是一个严格的信息提取器，只提取对话者本人的事实，只输出一句话或null。",
            )
        except Exception as e:
            logger.debug(f"[事实回写] LLM 调用失败: {e}")
            return

        if not resp or not resp.completion_text:
            return
        text = resp.completion_text.strip()
        if not text or text.lower() == "null" or text == "无" or len(text) < 4:
            return

        await self.core_memory.add_important_event(user_id, text, nickname=user_name, importance=3)
        logger.info(f"[事实回写] {user_name}({user_id}): {text}")

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

    async def _fetch_weather(self):
        if time.time() - self._weather_last_fetch < 1800:
            return
        import aiohttp
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("https://wttr.in/Minhang?format=j1", timeout=10) as r:
                    if r.status != 200:
                        return
                    data = await r.json()
                    current = data.get("current_condition", [{}])[0]
                    temp = current.get("temp_C", "")
                    desc = current.get("lang_zh", [{}])[0].get("value", "") or current.get("weatherDesc", [{}])[0].get("value", "")
                    self._cached_weather = f"{temp}度，{desc}"
                    self._weather_last_fetch = time.time()
                    logger.info(f"[天气] 已更新: {self._cached_weather}")
        except Exception as e:
            logger.debug(f"[天气] 获取失败: {e}")

    async def _proactive_check(self):
        await self._fetch_weather()
        self.state_manager.maybe_transition()
        topic = self.state_manager.should_speak_proactively()
        if topic:
            await self.put_kv_data("state_data", self.state_manager.to_dict())
            for session_str in list(self._group_sessions):
                try:
                    await self._send_proactive_to_group(session_str, topic)
                except Exception as e:
                    logger.error(f"主动发言发送失败 ({session_str}): {e}")

        if self._enable_private_proactive and self._private_targets:
            await self._proactive_private_check()

        if self._enable_qzone_post and self._cached_bot:
            await self._maybe_post_qzone()

    _FAREWELL_KEYWORDS = ("晚安", "睡了", "睡觉", "再见", "拜拜", "拜了", "先这样", "明天见", "回见", "下次聊", "去忙", "byebye", "bye", "88", "撤了", "闪了", "下播")

    async def _private_followup_flow(
        self, user_id: str, user_name: str, last_bot_reply: str, session_str: str
    ):
        try:
            await asyncio.sleep(300)  # 5分钟

            # 检查用户有没有回复
            last_user_ts = self._private_last_user_msg.get(user_id, 0)
            if time.time() - last_user_ts < 290:
                return

            reply_lower = last_bot_reply.lower()
            is_farewell = any(kw in reply_lower for kw in self._FAREWELL_KEYWORDS)

            # 30% 概率追问
            if random.random() > 0.30:
                return

            followup = await self._generate_private_followup(
                user_id, user_name, last_bot_reply, stage=1, is_farewell=is_farewell
            )
            if followup:
                try:
                    msg = MessageChain().message(followup)
                    await self.context.send_message(session_str, msg)
                    logger.info(f"[私聊跟进] 追问 {user_name}({user_id}): {followup[:30]}")
                except Exception as e:
                    logger.error(f"[私聊跟进] 发送追问失败: {e}")
                    return

            # 再等5分钟，看用户是否回复
            await asyncio.sleep(300)
            last_user_ts = self._private_last_user_msg.get(user_id, 0)
            if time.time() - last_user_ts < 290:
                return

            # 用户不在线，自言自语
            monologue = await self._generate_private_followup(
                user_id, user_name, last_bot_reply, stage=2, is_farewell=is_farewell
            )
            if monologue:
                try:
                    msg = MessageChain().message(monologue)
                    await self.context.send_message(session_str, msg)
                    logger.info(f"[私聊跟进] 自言自语 {user_name}({user_id}): {monologue[:30]}")
                except Exception as e:
                    logger.error(f"[私聊跟进] 发送自言自语失败: {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[私聊跟进] flow 异常: {e}")

    async def _generate_private_followup(
        self, user_id: str, user_name: str, last_bot_reply: str, stage: int,
        is_farewell: bool = False,
    ) -> str:
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            return ""

        sender_prompt = self.core_memory.build_sender_prompt(user_id, user_name) if self._enable_core_memory else f"对方叫{user_name}"
        affinity_prompt = self.affinity.build_status_prompt(user_id) if self._enable_affinity else ""
        level = self.affinity.get_level(user_id) if self._enable_affinity else "普通"
        is_close = level in ("喜欢", "很喜欢")

        if stage == 1:
            if is_farewell:
                tone = "对方已经说要走/睡了，你也知道对方不会立刻回。不要问「在吗」「不敢接话」这类期待回应的话，而是补一句温柔的祝愿或嘴硬的告别，自言自语的感觉"
            elif is_close:
                tone = "你有点在意对方没有回应，用随意的口气追一句，像是顺口问问，不要显得很在乎"
            else:
                tone = "对方没有回应，你随口追一句，语气平淡"
            prompt = (
                f"你刚才说了：「{last_bot_reply[:60]}」\n"
                f"{sender_prompt}{affinity_prompt}\n"
                f"{tone}。\n"
                "只说一句话，10字以内，自然随意。直接输出那句话。"
            )
        else:
            if is_farewell:
                tone = "对方早就说要离开了，你知道对方不在。自言自语一句，可以是没说出口的话，或者突然想起的小事，但不要再叫对方"
            elif is_close:
                tone = "你觉得对方大概不在了，心里有点失落，自顾自说一句，或者吐露一点真实感受"
            else:
                tone = "你觉得对方不在线了，随便自言自语一句"
            prompt = (
                f"{sender_prompt}{affinity_prompt}\n"
                f"{tone}。\n"
                "只说一句话，15字以内，不要问句。直接输出那句话。"
            )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是琪露诺，只输出一句简短的话。",
            )
        except Exception:
            return ""

        if not resp or not resp.completion_text:
            return ""
        text = resp.completion_text.strip()
        if self._enable_affinity:
            text, _, _, _ = self.affinity.extract_inner(text)
        return text

    async def _maybe_post_qzone(self):
        now = time.time()
        days = (now - self._qzone_last_post_ts) / 86400
        if days < 1:
            return
        # 每次检查约0.5%基础概率，超过2天后线性提升，上限1.5%
        # 期望：~2-3天发一条（cron每10分钟，144次/天）
        chance = min(0.015, 0.005 + (days - 1) * 0.005)
        if random.random() > chance:
            return
        await self._publish_qzone()

    async def _publish_qzone(self):
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            return

        from .cirno_states import CIRNO_STATES
        state = CIRNO_STATES.get(self.state_manager.current_state, {})
        label = state.get("label", "")
        topics = state.get("proactive_topics", [])
        topic_hint = random.choice(topics) if topics else label

        prompt = (
            f"琪露诺现在的状态：{label}。\n"
            f"最近发生的事：{topic_hint}\n\n"
            "用琪露诺的口气写一条QQ说说，像是在记录今天的心情或者发生的事。"
            "要求：一到两句话，口气随意，可以傲娇、可以撒娇、可以抱怨，带点真实感。"
            "不要加emoji，不要加标签，直接输出那句话。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是琪露诺，只输出一到两句话的说说内容。",
            )
        except Exception as e:
            logger.error(f"[说说] LLM 生成失败: {e}")
            return

        if not resp or not resp.completion_text:
            return
        text = resp.completion_text.strip()
        if self._enable_affinity:
            text, _, _, _ = self.affinity.extract_inner(text)

        result = await self._post_to_qzone(text)
        if result.get("success"):
            self._qzone_last_post_ts = time.time()
            await self.put_kv_data("qzone_last_post_ts", self._qzone_last_post_ts)
            logger.info(f"[说说] 发表成功: {text[:40]}")
        else:
            logger.warning(f"[说说] 发表失败: {result.get('msg', '')}")

    async def _post_to_qzone(self, text: str) -> dict:
        import aiohttp
        from urllib.parse import urlencode
        bot = self._cached_bot
        if not bot:
            return {"success": False, "msg": "无bot对象"}
        try:
            login_info = await bot.call_action("get_login_info")
            uin = str(login_info.get("user_id", ""))
            if not uin:
                return {"success": False, "msg": "获取UIN失败"}
            try:
                creds = await bot.call_action("get_credentials", domain="qzone.qq.com")
                cookie = creds.get("cookies", "")
            except Exception:
                try:
                    creds = await bot.call_action("get_cookies", domain="qzone.qq.com")
                    cookie = creds.get("cookies", "")
                except Exception:
                    return {"success": False, "msg": "获取cookie失败"}
            if not cookie:
                return {"success": False, "msg": "cookie为空"}
            cookie_dict = {k: v for item in cookie.split(";") if "=" in item for k, v in [item.strip().split("=", 1)]}
            skey = cookie_dict.get("p_skey") or cookie_dict.get("skey", "")
            if not skey:
                return {"success": False, "msg": "获取skey失败"}
            hash_val = 5381
            for char in skey:
                hash_val += (hash_val << 5) + ord(char)
            gtk = str(hash_val & 0x7FFFFFFF)
            url = f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={gtk}"
            payload = {"syn_tweet_verson": "1", "con": text, "feedversion": "1", "ver": "1",
                       "ugc_right": "1", "to_sign": "0", "hostuin": uin, "code_version": "1",
                       "format": "fs", "qzreferrer": f"https://user.qzone.qq.com/{uin}/infocenter"}
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie,
                       "Origin": "https://user.qzone.qq.com",
                       "Referer": f"https://user.qzone.qq.com/{uin}/infocenter",
                       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, data=urlencode(payload), headers=headers, timeout=30) as r:
                    resp_text = await r.text()
                    if '"code":0' in resp_text or '"code": 0' in resp_text:
                        return {"success": True}
                    return {"success": False, "msg": resp_text[:200]}
        except Exception as e:
            return {"success": False, "msg": str(e)}

    async def _proactive_private_check(self):
        from .cirno_states import CIRNO_STATES
        cat = CIRNO_STATES.get(self.state_manager.current_state, {}).get("category", "")
        if cat in ("rest",):
            return

        now = time.time()
        for target in self._private_targets:
            user_id = target["user_id"]
            session = target["session"]

            # 检查最近互动时间
            records = self.user_msg_store.get_recent(user_id, limit=1)
            last_ts = records[0].get("ts", 0) if records else 0
            if now - last_ts < self._private_min_idle:
                continue

            # 好感度检查：至少普通才主动找
            if self._enable_affinity:
                composite = self.affinity.get_composite(user_id)
                if composite < 46:
                    continue

            # 生成动机：从群聊最近话题或状态取
            motivation = self._build_private_motivation(user_id)
            try:
                await self._send_proactive_to_private(session, user_id, motivation)
                logger.info(f"[琪露诺私聊] 主动发送给 {user_id}: {motivation[:30]}")
            except Exception as e:
                logger.error(f"[琪露诺私聊] 发送失败 ({session}): {e}")

    def _build_private_motivation(self, user_id: str) -> str:
        profile = self.core_memory.get_profile(user_id) if self._enable_core_memory else None
        state = self.state_manager.current_state
        from .cirno_states import CIRNO_STATES
        state_topics = CIRNO_STATES.get(state, {}).get("proactive_topics", [])
        state_label = CIRNO_STATES.get(state, {}).get("label", "")
        topic = random.choice(state_topics) if state_topics else ""

        hour = datetime.now().hour
        rel = profile.get("relationship", "") if profile else ""
        events_raw = profile.get("important_events", []) if profile else []
        last_event = ""
        if events_raw:
            e = events_raw[-1]
            last_event = e.get("text", "") if isinstance(e, dict) else str(e)

        # 凌晨/早晨：梦境开场
        if 0 <= hour < 7:
            mode = random.choice(["dream", "share", "weather"])
        else:
            mode = random.choice(["share", "share", "weather", "topic"])  # share 权重更高

        if mode == "dream":
            return f"你刚做了个奇怪的梦，梦里出现了这个人，醒来后还有点恍惚，想找他说说。{rel}"
        elif mode == "share":
            return f"你正在「{state_label}」，刚刚发生了一件小事，突然想起这个人。想分享一下，但又不想显得太刻意。{('你对这个人的感觉：'+rel) if rel else ''}"
        elif mode == "weather":
            weather_hint = self._cached_weather or "今天天气还行"
            return f"刚才感受了一下外面的天气——{weather_hint}。想找这个人聊聊。{rel}"
        else:
            event_hint = f"你记得和他之间发生过：{last_event}" if last_event else ""
            return f"{rel}。{event_hint}。{topic}".strip("。")

    async def _send_proactive_to_private(self, session_str: str, user_id: str, motivation: str):
        try:
            provider_id = await self.context.get_current_chat_provider_id(session_str)
        except Exception:
            providers = self.context.get_all_providers()
            if not providers:
                return
            provider_id = providers[0].meta().id

        persona = await self.context.persona_manager.get_default_persona_v3(umo=session_str)
        base_system_prompt = persona.get("prompt", "") if persona else ""

        sender_prompt = self.core_memory.build_sender_prompt(user_id, "") if self._enable_core_memory else ""
        affinity_prompt = self.affinity.build_status_prompt(user_id) if self._enable_affinity else ""

        system_prompt = "\n".join([
            base_system_prompt,
            self.state_manager.get_prompt_injection(),
            sender_prompt,
            affinity_prompt,
            ABSOLUTE_RULES,
            "\n你们在私聊，没有旁观者。你突然想找这个人说说话。"
            "说一两句自然的开场，不要太刻意，像是真的想起他了。"
            "不要说「你好」，不要解释自己为什么突然来找他。",
        ])

        fake_prompt = f"[系统：琪露诺现在想主动找这个人说话，动机是：{motivation}]"

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=fake_prompt,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.error(f"[琪露诺私聊] LLM 调用失败: {e}")
            return

        if not resp or not resp.completion_text:
            return

        text = resp.completion_text
        if self._enable_affinity:
            text, _, _, _ = self.affinity.extract_inner(text)

        try:
            msg = MessageChain().message(text)
            await self.context.send_message(session_str, msg)
        except Exception as e:
            logger.error(f"[琪露诺私聊] 发送失败: {e}")

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

    _POKE_COOLDOWN = 60  # seconds before poke streak resets

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
        sender_name = event.get_sender_name()
        now = time.time()

        # streak tracking
        streak_info = self._poke_streaks.get(sender_id, {"count": 0, "last_ts": 0})
        if now - streak_info["last_ts"] > self._POKE_COOLDOWN:
            streak_info["count"] = 0
        streak_info["count"] += 1
        streak_info["last_ts"] = now
        self._poke_streaks[sender_id] = streak_info
        count = streak_info["count"]

        from .cirno_states import CIRNO_STATES
        cat = CIRNO_STATES.get(self.state_manager.current_state, {}).get("category", "")
        level = self.affinity.get_level(sender_id) if self._enable_affinity else "普通"
        is_liked = level in ("喜欢", "很喜欢")
        is_disliked = level in ("无视", "讨厌")

        # 第4次及以上：固定沉默
        if count >= 4:
            reply = "……（不理你了）" if is_liked else "..."
            yield event.plain_result(reply)
            return

        # 第3次：记录生气状态，溢出到后续对话
        if count == 3:
            self._poke_streaks[sender_id]["angry"] = True

        # 第1-3次：LLM动态生成
        reply = await self._generate_poke_reply(sender_id, sender_name, count, level, is_liked, is_disliked, is_rest=(cat == "rest"))
        yield event.plain_result(reply)

        poke_back_chance = 1.0 if count >= 3 else 0.2
        if random.random() < poke_back_chance:
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

    async def _generate_poke_reply(
        self, sender_id: str, sender_name: str,
        count: int, level: str, is_liked: bool, is_disliked: bool,
        is_rest: bool = False,
    ) -> str:
        if is_rest:
            fallback_pool = {
                1: ["嗯？……我在想事情呢……别戳", "唔……困……别闹", "……刚才想到个超厉害的点子……忘了"],
                2: ["吵什么啦……我在思考宇宙的奥秘……", "唔……再戳我就睡过去了", "别戳啦……脑子转不动了"],
                3: ["烦……不想动……", "唔……让我躺着……", "……（懒得理你）"],
            }
        else:
            fallback_pool = {
                1: ["哼！", "……（抖了一下）", "冰棍要来了哦", "嗯？谁啊", "突然戳我干什么"],
                2: ["又来？", "……还戳", "再戳把你冻住", "你很闲哦", "戳上瘾了是不是"],
                3: ["烦死了", "不理你了", "……", "真的会生气哦", "最后警告"],
            }
        try:
            provider_id = self.context.get_all_providers()[0].meta().id
        except Exception:
            return random.choice(fallback_pool.get(count, ["..."]))

        state_label = self.state_manager.get_prompt_injection()[:40]
        sender_prompt = self.core_memory.build_sender_prompt(sender_id, sender_name) if self._enable_core_memory else f"对方叫{sender_name}"
        affinity_prompt = self.affinity.build_status_prompt(sender_id) if self._enable_affinity else ""
        warmth = self.affinity.get_warmth(sender_id) if self._enable_affinity else None

        if is_rest:
            if count == 1:
                situation = "你正在休息、发呆或快睡着，对方戳了你一下，你迷迷糊糊地有了反应。"
            elif count == 2:
                situation = "你困得不想动，对方又戳了你一下，你嫌烦但懒得发火。"
            else:
                situation = "你正想好好歇着，对方却一直戳，你又困又烦，只想让他停下。"
        elif count == 1:
            situation = "对方刚刚戳了你一下，你刚回过神来。"
        elif count == 2:
            situation = "对方戳了你两下，不说话，就只是戳。你有点疑惑。"
        else:
            if is_liked:
                situation = "对方戳了你三下，一句话不说。你假装烦但有点藏不住，不耐烦里带着一丝在意。"
            elif is_disliked:
                situation = "对方已经戳了你三下，你已经很烦了，勉强回一句就想结束。"
            else:
                situation = "对方戳了你三下，一句话不说。你开始真的不耐烦了，语气敷衍。"

        warmth_hint = ""
        if warmth is not None:
            if warmth < 0.4:
                warmth_hint = "最近你们互动感觉有点冷，你对他没那么热情。"
            elif warmth > 0.65:
                warmth_hint = "最近互动挺愉快的，心里对他印象不错。"

        angles = [
            "假装没被戳到，自顾自说一句完全不相关的事",
            "傲娇地承认被戳到了，嘴硬但藏不住",
            "突然说一句莫名其妙的话，像是自己在想别的事",
            "反问对方在干什么，但不要说「干嘛」这个词",
            "假装受伤地抱怨，夸张地说被戳疼了",
            "把这次戳当成对方在向你求助或搭话，热情地接过去",
            "炫耀自己刚做了什么了不起的事，完全不在意被戳",
            "误会对方的意图，往奇怪的方向理解这次戳",
            "提一件你和对方之间或最近发生的具体小事",
            "用冰系能力反击，但要具体、有画面感，别只说「冻住你」",
        ]
        if is_rest:
            angles = [
                "迷迷糊糊地嘟囔一句，像是没完全睡醒",
                "抱怨被打扰了休息，但语气软绵绵的",
                "说一句刚才在梦里或发呆时想到的奇怪东西",
                "懒洋洋地让对方别戳，不想动",
                "假装没听见，自言自语一句不相关的话",
            ]
        angle = random.choice(angles)
        prompt = (
            f"{situation}{warmth_hint}\n"
            f"当前状态：{state_label}\n"
            f"{sender_prompt}{affinity_prompt}\n\n"
            f"用琪露诺的口气回应这次戳一戳。这次的反应角度：{angle}。\n"
            "要求：只说一句话，简短（15字以内），符合上面的情绪和关系。\n"
            "直接输出那句话，不加任何前缀。"
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是琪露诺，只输出一句简短回应，不超过15字。",
            )
        except Exception:
            return random.choice(fallback_pool.get(count, ["..."]))

        if not resp or not resp.completion_text:
            return random.choice(fallback_pool.get(count, ["..."]))

        text = resp.completion_text.strip()
        if self._enable_affinity:
            text, _, _, _ = self.affinity.extract_inner(text)
        return text

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("琪露诺诊断")
    async def debug_stuck(self, event: AstrMessageEvent):
        snap = self._collect_diagnostics()
        logger.warning(f"[琪露诺诊断-手动]\n{snap}")
        yield event.plain_result(snap)

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
            turns_left = self._prank_state.get("turns_left")
            if turns_left is not None:
                lines.append(f"恶作剧模式: 激活，剩余约 {turns_left} 轮")
            else:
                remaining = max(0, int(self._prank_state["expires_at"] - time.time())) // 60
                lines.append(f"恶作剧模式: 激活，剩余约 {remaining} 分钟")
        else:
            lines.append("恶作剧模式: 未激活")
        session_id = event.unified_msg_origin
        hf = self.heart_flow.get_debug(session_id)
        lines.append(f"心流兴趣度: {hf['interest']:.3f}")
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

        sid = event.unified_msg_origin
        self._imitation_state[sid] = {
            "target_name": name,
            "style_desc": style_desc,
        }
        logger.info(f"[琪露诺模仿] 开始模仿 {name}({uid})，风格描述: {style_desc[:60]}...")
        yield event.plain_result(
            f"好！我知道{name}怎么说话了！\n（风格：{style_desc[:80]}{'…' if len(style_desc) > 80 else ''}）"
        )

    @filter.command("琪露诺停止模仿")
    async def stop_imitation(self, event: AstrMessageEvent):
        sid = event.unified_msg_origin
        state = self._imitation_state.pop(sid, None)
        if not state:
            yield event.plain_result("我现在没有在模仿任何人啊！")
            return
        name = state["target_name"]
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
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._diag_task and not self._diag_task.done():
            self._diag_task.cancel()
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
        for task in self._private_followup_tasks.values():
            if not task.done():
                task.cancel()
        self._private_followup_tasks.clear()
        logger.info("琪露诺状态系统已保存并清理")
