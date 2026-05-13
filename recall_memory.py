import asyncio
import math
import time

import jieba

from astrbot.api import logger

STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "吗", "什么",
    "那", "啊", "呢", "吧", "嗯", "哦", "哈", "呀", "哪", "怎么",
    "可以", "这个", "那个", "但是", "因为", "所以", "如果", "虽然",
    "干", "嘛", "干嘛", "干啥", "啥", "谁", "哪里", "多少", "为啥",
    "来", "吗", "呗", "么", "吃", "玩", "做", "想", "能", "对",
    "还", "再", "又", "才", "被", "把", "给", "让", "跟", "比",
    "真", "太", "挺", "最", "更", "特别", "非常", "一下", "一点",
    "知道", "觉得", "感觉", "应该", "可能", "已经", "正在", "开始",
    "一些", "这样", "那么", "然后", "时候", "东西", "这么", "怎样",
    "这里", "那里", "什么样", "为什么", "不过", "而且", "或者", "以及",
    "关于", "通过", "一样",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "my", "your", "his", "its", "our", "their", "this", "that",
    "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "do", "did", "does", "have", "has", "had", "will", "would",
    "can", "could", "not", "no", "so", "if", "just", "like",
}

COMPRESS_PROMPT = (
    "你是琪露诺的记忆管理器。下面是琪露诺最近和别人聊天的原始对话。\n"
    "请把这些对话压缩成一段简短的模糊记忆，用第三人称描述。\n"
    "要求：\n"
    "- 提取核心话题和关键信息，丢弃表情、口癖、语气词\n"
    "- 记住谁说了什么、聊了什么话题\n"
    "- 用「琪露诺好像记得……」的口吻\n"
    "- 控制在2-3句话以内\n"
    "- 直接输出记忆内容，不要加任何前缀或解释\n"
    "- 不要记录食物口味、具体数字等琐碎细节，只保留人与人之间发生的事\n\n"
    "原始对话：\n{conversations}"
)

WEEK_SECONDS = 7 * 86400
L2_THRESHOLD = 15


def extract_keywords(text: str) -> list[str]:
    text = text.lower()
    words = jieba.lcut(text)
    return [w.strip() for w in words if w.strip() and w.strip() not in STOP_WORDS and len(w.strip()) > 1]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class RecallMemory:
    def __init__(self, plugin, buffer_limit: int = 15, top_k: int = 3):
        self._plugin = plugin
        self._buffer_limit = buffer_limit
        self._top_k = top_k
        self._buffer: list[dict] = []
        self._global_count = 0
        self._summaries: list[dict] = []
        self._digests: list[dict] = []
        self._key_event_callback = None
        self._llm_generate = None
        self._embed_provider = None

    def set_key_event_callback(self, callback):
        self._key_event_callback = callback

    def set_llm_generate(self, llm_generate):
        self._llm_generate = llm_generate

    def set_embed_provider(self, provider):
        self._embed_provider = provider

    async def load(self):
        raw = await self._plugin.get_kv_data("recall_summaries", None)
        self._summaries = raw if isinstance(raw, list) else []
        raw = await self._plugin.get_kv_data("recall_digests", None)
        self._digests = raw if isinstance(raw, list) else []
        raw = await self._plugin.get_kv_data("recall_global_count", None)
        self._global_count = raw if isinstance(raw, int) else 0

        await self._cleanup_old()
        await self._migrate_old_format()

        total = len(self._summaries) + len(self._digests)
        logger.info(
            f"回忆记忆已加载，L1={len(self._summaries)}条，"
            f"L2={len(self._digests)}条，全局计数={self._global_count}"
        )

    async def _migrate_old_format(self):
        raw_index = await self._plugin.get_kv_data("recall_months", None)
        if not isinstance(raw_index, list) or not raw_index:
            return
        logger.info(f"回忆记忆：检测到旧格式数据 ({len(raw_index)} 个月份)，正在清理...")
        for key in raw_index:
            await self._plugin.delete_kv_data(key)
        await self._plugin.delete_kv_data("recall_months")
        logger.info("回忆记忆：旧格式数据已清理")

    async def save(self):
        await self._plugin.put_kv_data("recall_summaries", self._summaries)
        await self._plugin.put_kv_data("recall_digests", self._digests)
        await self._plugin.put_kv_data("recall_global_count", self._global_count)

    async def archive(self, user_id: str, user_name: str, user_msg: str, bot_reply: str):
        self._buffer.append({
            "ts": time.time(),
            "uid": str(user_id),
            "name": user_name,
            "msg": user_msg[:200],
            "reply": bot_reply[:200],
        })
        self._global_count += 1

        if self._global_count % self._buffer_limit == 0 and len(self._buffer) >= self._buffer_limit:
            batch = self._buffer[-self._buffer_limit:]
            if self._key_event_callback:
                users_in_batch: dict[str, str] = {}
                for e in batch:
                    users_in_batch[e["uid"]] = e["name"]
                for uid, name in users_in_batch.items():
                    user_entries = [e for e in batch if e["uid"] == uid]
                    await self._key_event_callback(uid, name, user_entries)
            asyncio.create_task(self._compress(batch))

    def _find_related_summaries(self, batch: list[dict]) -> list[str]:
        users = {e["uid"] for e in batch}
        batch_kw = set()
        for e in batch:
            batch_kw.update(extract_keywords(e["msg"] + " " + e["reply"]))
        related = []
        for s in self._summaries:
            user_overlap = users & set(s.get("users", []))
            kw_overlap = batch_kw & set(s.get("kw", []))
            if user_overlap and len(kw_overlap) >= 2:
                related.append(s["text"])
        return related[-3:]

    async def _compress(self, batch: list[dict]):
        if not self._llm_generate:
            logger.warning("回忆记忆：无 LLM 生成函数，跳过压缩")
            return

        conv_lines = []
        for e in batch:
            conv_lines.append(f"{e['name']}：{e['msg']}")
            conv_lines.append(f"琪露诺：{e['reply']}")
        conversations = "\n".join(conv_lines)

        related = self._find_related_summaries(batch)
        if related:
            existing = "\n".join(f"- {r}" for r in related)
            prompt = COMPRESS_PROMPT.format(conversations=conversations) + (
                f"\n\n你之前关于这些人的记忆：\n{existing}\n"
                "如果新对话是旧记忆的延续或补充，把它们融合成一段更完整的记忆。"
                "不要重复旧内容，只补充新的部分。"
                "已经记过的事（包括具体细节、口味、数字等）不要再提。"
            )
        else:
            prompt = COMPRESS_PROMPT.format(conversations=conversations)

        try:
            resp = await self._llm_generate(prompt)
            if not resp or not resp.completion_text:
                logger.warning("回忆记忆：LLM 压缩返回空结果")
                return
        except Exception as e:
            logger.error(f"回忆记忆压缩失败: {e}")
            return

        all_kw: list[str] = []
        users = set()
        ts_min = batch[0]["ts"]
        ts_max = batch[-1]["ts"]
        for e in batch:
            all_kw.extend(extract_keywords(e["msg"] + " " + e["reply"]))
            users.add(e["uid"])
        kw_unique = list(dict.fromkeys(all_kw))[:30]

        summary_text = resp.completion_text.strip()
        vec = None
        if self._embed_provider:
            try:
                vec = await self._embed_provider.get_embedding(summary_text)
            except Exception as e:
                logger.warning(f"回忆记忆：embedding 生成失败: {e}")

        summary = {
            "ts": ts_max,
            "ts_start": ts_min,
            "text": summary_text,
            "kw": kw_unique,
            "users": list(users),
            "vec": vec,
        }
        self._summaries.append(summary)
        logger.info(f"回忆记忆：L1 压缩完成，当前 {len(self._summaries)} 条")

        if len(self._summaries) >= L2_THRESHOLD:
            await self._compress_l2()

        await self.save()

    async def _compress_l2(self):
        if not self._llm_generate:
            return

        batch = self._summaries[:L2_THRESHOLD]
        texts = "\n".join(f"- {s['text']}" for s in batch)
        prompt = (
            "你是琪露诺的记忆管理器。下面是琪露诺的多段模糊记忆。\n"
            "请将它们进一步浓缩为一段更概括的印象，2-3句话。\n"
            "用「琪露诺依稀记得……」的口吻，保留最重要的人和事。\n"
            "直接输出，不加前缀。\n\n"
            f"记忆片段：\n{texts}"
        )

        try:
            resp = await self._llm_generate(prompt)
            if not resp or not resp.completion_text:
                return
        except Exception as e:
            logger.error(f"回忆记忆 L2 压缩失败: {e}")
            return

        all_kw: list[str] = []
        all_users: set[str] = set()
        ts_min = batch[0].get("ts_start", batch[0]["ts"])
        ts_max = batch[-1]["ts"]
        for s in batch:
            all_kw.extend(s.get("kw", []))
            all_users.update(s.get("users", []))
        kw_unique = list(dict.fromkeys(all_kw))[:40]

        digest = {
            "ts": ts_max,
            "ts_start": ts_min,
            "text": resp.completion_text.strip(),
            "kw": kw_unique,
            "users": list(all_users),
        }
        self._digests.append(digest)
        self._summaries = self._summaries[L2_THRESHOLD:]
        logger.info(f"回忆记忆：L2 压缩完成，当前 L1={len(self._summaries)}, L2={len(self._digests)}")

    async def _cleanup_old(self):
        now = time.time()
        cutoff = now - WEEK_SECONDS
        before_s = len(self._summaries)
        before_d = len(self._digests)
        self._summaries = [s for s in self._summaries if s.get("ts", 0) > cutoff]
        self._digests = [d for d in self._digests if d.get("ts", 0) > cutoff]
        removed = (before_s - len(self._summaries)) + (before_d - len(self._digests))
        if removed:
            logger.info(f"回忆记忆：清理过期记录 {removed} 条")
            await self.save()

    def search(self, query: str, current_user_id: str | None = None, top_k: int | None = None) -> list[dict]:
        if top_k is None:
            top_k = self._top_k
        query_kw = set(extract_keywords(query))
        if not query_kw:
            return []

        now = time.time()
        scored: list[tuple[float, dict]] = []

        for entry in self._summaries + self._digests:
            entry_kw = set(entry.get("kw", []))
            if not entry_kw:
                continue

            overlap = len(query_kw & entry_kw)
            if overlap < 2:
                continue

            kw_score = overlap / max(len(query_kw), 1)
            age_hours = (now - entry.get("ts", now)) / 3600
            time_decay = math.exp(-age_hours / (24 * 7))
            user_bonus = 0.2 if current_user_id and current_user_id in entry.get("users", []) else 0.0
            score = kw_score * 0.7 + time_decay * 0.1 + user_bonus

            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    async def search_async(
        self, query: str, current_user_id: str | None = None, top_k: int | None = None
    ) -> list[dict]:
        if not self._embed_provider:
            return self.search(query, current_user_id, top_k)
        if top_k is None:
            top_k = self._top_k

        try:
            query_vec = await self._embed_provider.get_embedding(query)
        except Exception as e:
            logger.warning(f"回忆记忆：query embedding 失败，降级到关键词: {e}")
            return self.search(query, current_user_id, top_k)

        now = time.time()
        scored: list[tuple[float, dict]] = []
        for entry in self._summaries + self._digests:
            vec = entry.get("vec")
            if not vec:
                continue
            cosine = _cosine(query_vec, vec)
            age_hours = (now - entry.get("ts", now)) / 3600
            time_decay = math.exp(-age_hours / (24 * 7))
            user_bonus = 0.2 if current_user_id and current_user_id in entry.get("users", []) else 0.0
            score = cosine * 0.6 + time_decay * 0.2 + user_bonus * 0.2
            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [entry for _, entry in scored[:top_k]]

        if not results:
            return self.search(query, current_user_id, top_k)
        return results

    def get_recent_by_user(self, user_id: str, limit: int = 15) -> list[dict]:
        user_id = str(user_id)
        entries = [e for e in self._buffer if e.get("uid") == user_id]
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return entries[:limit]

    def build_recall_prompt(self, memories: list[dict]) -> str:
        if not memories:
            return ""
        lines = []
        for m in memories:
            text = m.get("text", "")
            if not text:
                continue
            ts = m.get("ts", 0)
            age_days = (time.time() - ts) / 86400
            if age_days < 1:
                time_hint = "今天"
            elif age_days < 2:
                time_hint = "昨天"
            elif age_days < 4:
                time_hint = "前几天"
            else:
                time_hint = "之前"
            lines.append(f"- {time_hint}：{text}")
        if not lines:
            return ""
        return "【你脑子里浮现出一些模糊的记忆片段】\n" + "\n".join(lines)

    def get_buffer_entries(self) -> list[dict]:
        return list(self._buffer)

    def get_stats(self) -> dict:
        return {
            "buffer": len(self._buffer),
            "summaries": len(self._summaries),
            "digests": len(self._digests),
            "global_count": self._global_count,
        }
