import json
import time

from astrbot.api import logger

CORE_MEMORY_UPDATE_PROMPT = """你是琪露诺，现在安安静静地躺在湖边，回忆今天和一个人的相处。
这是你在整理自己的想法，不是在跟别人说话。
用"我"来写，但不要带上你平时说话的口癖和语气词（不要用"哼"、"本天才"、"笨蛋"之类的）。
像是睡前躺在床上想今天发生的事一样，诚实地记录你的感受。

注意：和你聊天的人来自人间之里。就算他们的QQ昵称看起来像幻想乡的角色（比如叫"灵梦""大妖精"），大概率只是网名。你可以心里犯嘀咕"这个人该不会真的是……"，但不要真的把他们当成那个角色来记忆。用他们的真名来记。
{background}
当前你对这个人的印象：
- 关系：{relationship}
- 特征：{traits}
- 重要事件：{events}

最近的对话摘要：
{summary}

请用JSON格式输出你更新后的印象，只输出JSON，不要输出其他内容：
{{"relationship": "我和这个人的关系，诚实地写", "traits": ["特征1", "特征2", ...], "important_events": ["事件1", "事件2", "事件3"]}}

注意：
- relationship 用一两句话概括你的真实感受，不要写长段落，不要用口癖
- traits 最多5个，每个2-4个字
- important_events 最多3个，每个一句话以内
- 所有内容都用平静、内省的语气，像在写日记"""


class CoreMemory:
    def __init__(self, plugin, seed_data: dict, update_threshold: int = 15):
        self._plugin = plugin
        self._seed_data = seed_data
        self._threshold = update_threshold
        self._profiles: dict[str, dict] = {}
        self._counters: dict[str, int] = {}

    async def load(self):
        saved = await self._plugin.get_kv_data("core_memory", None)
        if saved and isinstance(saved, dict):
            self._profiles = saved
            logger.info(f"核心记忆已加载，共 {len(self._profiles)} 个用户档案")
        else:
            self._profiles = {}

        migrated = await self._plugin.get_kv_data("core_memory_events_cleaned", None)
        if not migrated:
            for p in self._profiles.values():
                p["important_events"] = []
            await self._plugin.put_kv_data("core_memory_events_cleaned", True)
            logger.info("核心记忆：一次性清空旧 important_events 数据")

        need_save = False
        for uid, val in self._seed_data.items():
            uid = str(uid)
            try:
                name, prompt = val
            except (TypeError, ValueError):
                logger.warning(f"核心记忆：种子数据格式异常，跳过 uid={uid}")
                continue
            if uid in self._profiles:
                p = self._profiles[uid]
                if p.get("name") != name or p.get("original_prompt") != prompt:
                    p["name"] = name
                    p["original_prompt"] = prompt
                    need_save = True
            else:
                self._profiles[uid] = {
                    "name": name,
                    "relationship": "",
                    "traits": [],
                    "important_events": [],
                    "original_prompt": prompt,
                    "updated_at": time.time(),
                }
                logger.info(f"核心记忆：从种子数据新增用户 {name} ({uid})")
                need_save = True

        if need_save:
            await self.save()

    async def save(self):
        await self._plugin.put_kv_data("core_memory", self._profiles)

    MAX_RELATED_PEOPLE = 5

    def build_people_prompt(self, user_msg: str = "", sender_id: str = "") -> str:
        if not self._profiles:
            return ""
        if not user_msg:
            return ""
        from .recall_memory import extract_keywords
        keywords = set(extract_keywords(user_msg))
        if not keywords:
            return ""

        lines = []
        for uid, p in self._profiles.items():
            if uid == sender_id:
                continue
            name = p.get("name", uid)
            searchable = name + " " + p.get("relationship", "") + " " + " ".join(p.get("traits", []))
            if not (keywords & set(extract_keywords(searchable))):
                continue
            rel = p.get("relationship", "")
            if rel:
                lines.append(f"- {name}(QQ{uid})：{rel}")
            else:
                lines.append(f"- {name}(QQ{uid})")
            if len(lines) >= self.MAX_RELATED_PEOPLE:
                break
        if not lines:
            return ""
        return "【你想起了一些可能相关的人】\n" + "\n".join(lines)

    def build_sender_prompt(self, sender_id: str, sender_nickname: str) -> str:
        sender_id = str(sender_id)
        if sender_id in self._profiles:
            p = self._profiles[sender_id]
            name = p.get("name", sender_nickname)
            relationship = p.get("relationship", "")
            original = p.get("original_prompt", "")
            events = p.get("important_events", [])
            parts = [
                f"\n【当前对话者】QQ号{sender_id}，QQ昵称「{sender_nickname}」。"
                f"这个人就是{name}，不管昵称看起来像谁，他应该就是{name}。"
            ]
            if relationship:
                parts.append(f"你对{name}的印象：{relationship}")
            elif original:
                parts.append(original)
            if events:
                parts.append("你记得和他之间发生过这些事：" + "；".join(events))
            return "".join(parts)
        else:
            return (
                f"\n当前和你对话的人QQ号是{sender_id}，QQ昵称是「{sender_nickname}」，"
                f"你不认识这个人。他大概是人间之里的人类吧，虽然昵称看起来有点眼熟……"
            )

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def get_interaction_count(self, user_id: str) -> int:
        return self._counters.get(str(user_id), 0)

    @property
    def update_threshold(self) -> int:
        return self._threshold

    def get_profile(self, user_id: str) -> dict | None:
        return self._profiles.get(str(user_id))

    def record_interaction(self, user_id: str):
        user_id = str(user_id)
        self._counters[user_id] = self._counters.get(user_id, 0) + 1

    def should_update(self, user_id: str) -> bool:
        user_id = str(user_id)
        return self._counters.get(user_id, 0) >= self._threshold

    def reset_counter(self, user_id: str):
        self._counters[str(user_id)] = 0

    async def update_profile_via_llm(self, user_id: str, recent_summary: str, context, nickname: str = ""):
        user_id = str(user_id)
        profile = self._profiles.get(user_id)
        if not profile:
            profile = {
                "name": nickname or user_id,
                "relationship": "",
                "traits": [],
                "important_events": [],
                "original_prompt": "",
                "updated_at": time.time(),
            }
            self._profiles[user_id] = profile

        try:
            provider_id = context.get_all_providers()[0].meta().id
        except Exception:
            logger.warning("核心记忆更新：无可用 LLM Provider")
            return

        original = profile.get("original_prompt", "")
        bg = f"\n关于这个人的背景：{original}\n" if original else "\n"
        prompt_text = CORE_MEMORY_UPDATE_PROMPT.format(
            background=bg,
            relationship=profile.get("relationship", "不太熟"),
            traits=", ".join(profile.get("traits", [])) or "暂无",
            events=", ".join(profile.get("important_events", [])) or "暂无",
            summary=recent_summary,
        )

        try:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt_text,
                system_prompt="你是一个JSON生成器，只输出合法的JSON，不要输出其他任何内容。",
            )
        except Exception as e:
            logger.error(f"核心记忆 LLM 更新失败: {e}")
            return

        if not resp or not resp.completion_text:
            return

        try:
            text = resp.completion_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            result = json.loads(text)
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.warning(f"核心记忆 LLM 返回解析失败: {e}")
            return

        if not isinstance(result, dict):
            logger.warning("核心记忆 LLM 返回非 dict，跳过")
            return

        if "relationship" in result:
            profile["relationship"] = str(result["relationship"])[:100]
        if "traits" in result and isinstance(result["traits"], list):
            profile["traits"] = [str(t)[:20] for t in result["traits"][:5]]
        if "important_events" in result and isinstance(result["important_events"], list):
            profile["important_events"] = [str(e)[:50] for e in result["important_events"][:3]]
        profile["updated_at"] = time.time()

        self._profiles[user_id] = profile
        await self.save()
        self.reset_counter(user_id)
        logger.info(f"核心记忆已更新用户 {profile.get('name', user_id)} 的档案")

    async def update_profile_from_daily(self, user_id: str, records: list[dict], context, nickname: str = ""):
        """根据当日群消息（含可选 bot_reply）更新用户画像，供每日 cron 调用。"""
        if not records:
            return

        lines = []
        for r in records:
            lines.append(f"{r['name']}：{r['msg']}")
            if r.get("bot_reply"):
                lines.append(f"琪露诺：{r['bot_reply']}")
        summary = "\n".join(lines)

        await self.update_profile_via_llm(user_id, summary, context, nickname=nickname)

    async def add_important_event(self, user_id: str, event_text: str, nickname: str = ""):
        user_id = str(user_id)
        profile = self._profiles.get(user_id)
        if not profile:
            profile = {
                "name": nickname or user_id,
                "relationship": "",
                "traits": [],
                "important_events": [],
                "original_prompt": "",
                "updated_at": time.time(),
            }
            self._profiles[user_id] = profile

        events: list = profile.setdefault("important_events", [])
        event_text = event_text[:50]
        if event_text not in events:
            events.append(event_text)
            if len(events) > 3:
                events.pop(0)
            profile["updated_at"] = time.time()
            await self.save()
            logger.info(f"核心记忆：直接写入事件 [{profile.get('name', user_id)}] {event_text}")
