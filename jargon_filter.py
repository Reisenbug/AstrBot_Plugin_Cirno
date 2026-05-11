import math
import re
import time
from collections import defaultdict

from astrbot.api import logger

_MIN_TERM_LENGTH = 2
_MIN_FREQUENCY = 5
_MAX_CONTEXT_EXAMPLES = 10
_JIEBA_FREQ_THRESHOLD = 100
_W_IDF = 0.4
_W_BURST = 0.3
_W_CONCENTRATION = 0.3

_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没",
    "看", "好", "自", "这", "他", "她", "它", "们", "吗", "吧", "呢", "啊",
    "哦", "嗯", "呀", "哈", "那", "么", "什", "啦", "来", "对", "把", "让",
    "被", "给", "从", "还", "比", "得", "过", "可", "能", "为", "以", "而",
    "但", "或", "如", "与", "等", "及", "其", "之", "这个", "那个", "什么",
    "怎么", "哪里", "这里", "那里", "自己", "大家", "我们", "你们", "他们",
    "知道", "觉得", "感觉", "可以", "应该", "需要", "已经", "开始", "然后",
    "因为", "所以", "虽然", "如果", "不是", "没有", "今天", "昨天", "明天",
    "现在", "时间", "真的", "其实", "当然", "特别", "非常", "一直", "还是",
    "哈哈", "哈哈哈", "呵呵", "谢谢", "感谢", "抱歉", "不好意思",
})


class JargonStatisticalFilter:
    def __init__(self):
        self._group_term_freq: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._global_term_freq: dict[str, int] = defaultdict(int)
        self._user_term_freq: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )
        self._term_first_seen: dict[str, dict[str, float]] = defaultdict(dict)
        self._term_contexts: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self._jieba_loaded = False
        self._jieba_freq: dict[str, int] = {}

    def update_from_message(self, content: str, group_id: str, sender_id: str) -> None:
        if not content or not group_id:
            return
        tokens = self._tokenize(content)
        if not tokens:
            return
        now = time.time()
        gf = self._group_term_freq[group_id]
        uf = self._user_term_freq[group_id]
        fs = self._term_first_seen[group_id]
        ctx = self._term_contexts[group_id]
        for token in tokens:
            gf[token] += 1
            self._global_term_freq[token] += 1
            uf[token][sender_id] += 1
            if token not in fs:
                fs[token] = now
            if len(ctx[token]) < _MAX_CONTEXT_EXAMPLES:
                ctx[token].append(content)

    def get_jargon_candidates(
        self, group_id: str, top_k: int = 20, exclude_terms: set | None = None
    ) -> list[dict]:
        gf = self._group_term_freq.get(group_id)
        if not gf:
            return []
        exclude = exclude_terms or set()
        num_groups = max(len(self._group_term_freq), 1)
        candidates = []
        for term, freq in gf.items():
            if freq < _MIN_FREQUENCY or term in exclude:
                continue
            groups_containing = sum(1 for g in self._group_term_freq.values() if term in g)
            idf = math.log(num_groups / max(groups_containing, 1))
            burst = self._burst_score(term, group_id)
            unique_users = len(self._user_term_freq.get(group_id, {}).get(term, {}))
            concentration = 1.0 / max(unique_users, 1)
            score = idf * _W_IDF + burst * _W_BURST + concentration * _W_CONCENTRATION
            candidates.append({
                "term": term,
                "score": round(score, 4),
                "frequency": freq,
                "context_examples": self._term_contexts.get(group_id, {}).get(term, [])[:5],
            })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_k]

    def _burst_score(self, term: str, group_id: str) -> float:
        first = self._term_first_seen.get(group_id, {}).get(term, 0)
        if not first:
            return 0.0
        age_days = max((time.time() - first) / 86400.0, 1.0)
        freq = self._group_term_freq.get(group_id, {}).get(term, 0)
        return freq / age_days

    def _tokenize(self, text: str) -> list[str]:
        text = re.sub(r'@\S+', '', text)
        text = re.sub(r'https?://\S+', '', text)
        text = re.sub(r'\[.*?\]', '', text)
        self._ensure_jieba()
        import jieba
        tokens = []
        for word in jieba.cut(text):
            word = word.strip()
            if len(word) < _MIN_TERM_LENGTH:
                continue
            if word in _STOPWORDS:
                continue
            if re.match(r'^[\d\s]+$', word) or re.match(r'^[^\w]+$', word):
                continue
            if self._jieba_freq.get(word, 0) > _JIEBA_FREQ_THRESHOLD:
                continue
            tokens.append(word)
        return tokens

    def _ensure_jieba(self) -> None:
        if not self._jieba_loaded:
            try:
                import jieba
                if not jieba.dt.initialized:
                    jieba.initialize()
                self._jieba_freq = jieba.dt.FREQ
                self._jieba_loaded = True
            except Exception as e:
                logger.warning(f"[JargonFilter] jieba 加载失败: {e}")
