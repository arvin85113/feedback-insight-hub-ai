import json
from collections import Counter
from pathlib import Path
import re
from typing import Any, Callable, TypeVar

try:
    import jieba
except ImportError:  # pragma: no cover - optional dependency fallback
    jieba = None


BASE_DIR = Path(__file__).resolve().parent
STOPWORDS_PATH = BASE_DIR / "data" / "stopwords.txt"
SYNONYMS_PATH = BASE_DIR / "data" / "synonyms.json"
POSITIVE_WORDS_PATH = BASE_DIR / "data" / "positive_words.txt"
NEGATIVE_WORDS_PATH = BASE_DIR / "data" / "negative_words.txt"
NEGATION_WORDS_PATH = BASE_DIR / "data" / "negation_words.txt"
INTENSIFIERS_PATH = BASE_DIR / "data" / "intensifiers.json"

DEFAULT_STOP_WORDS = {
    "我們",
    "你們",
    "這個",
    "那個",
    "非常",
    "feedback",
    "問卷",
    "改善",
}
ANALYSIS_VERSION = "v2"

_T = TypeVar("_T")
# path.resolve() 字串 -> (簽名, 快取內容)；簽名變化時重讀（檔案新增/刪除/儲存）
_file_load_cache: dict[str, tuple[tuple, Any]] = {}


def _path_cache_signature(path: Path) -> tuple:
    try:
        st = path.stat()
        return (True, st.st_mtime_ns)
    except OSError:
        return (False, 0.0)


def _cached_data(path: Path, factory: Callable[[], _T]) -> _T:
    key = str(path.resolve())
    sig = _path_cache_signature(path)
    hit = _file_load_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]  # type: ignore[return-value]
    data = factory()
    _file_load_cache[key] = (sig, data)
    return data


def load_stop_words():
    def _read():
        if not STOPWORDS_PATH.exists():
            return DEFAULT_STOP_WORDS

        words = set()
        for line in STOPWORDS_PATH.read_text(encoding="utf-8").splitlines():
            token = line.strip().lower()
            if not token or token.startswith("#"):
                continue
            words.add(token)
        return words or DEFAULT_STOP_WORDS

    return _cached_data(STOPWORDS_PATH, _read)


def load_synonyms():
    def _read():
        if not SYNONYMS_PATH.exists():
            return {}
        try:
            raw = json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return {str(k).lower(): str(v).lower() for k, v in raw.items()}

    return _cached_data(SYNONYMS_PATH, _read)


def load_positive_words():
    def _read():
        if not POSITIVE_WORDS_PATH.exists():
            return {"滿意", "推薦", "喜歡", "方便", "快速", "友善", "舒適", "乾淨"}
        return {
            line.strip().lower()
            for line in POSITIVE_WORDS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    return _cached_data(POSITIVE_WORDS_PATH, _read)


def load_negative_words():
    def _read():
        if not NEGATIVE_WORDS_PATH.exists():
            return {"不滿意", "慢", "昂貴", "髒亂", "困難", "糟糕", "抱怨", "失望"}
        return {
            line.strip().lower()
            for line in NEGATIVE_WORDS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    return _cached_data(NEGATIVE_WORDS_PATH, _read)


def load_negation_words():
    def _read():
        if not NEGATION_WORDS_PATH.exists():
            return {"不", "不是", "不太", "沒有", "沒", "別", "非"}
        return {
            line.strip().lower()
            for line in NEGATION_WORDS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    return _cached_data(NEGATION_WORDS_PATH, _read)


def load_intensifiers():
    def _read():
        if not INTENSIFIERS_PATH.exists():
            return {"很": 1.2, "非常": 1.6, "超": 1.5, "太": 1.4, "有點": 0.8}
        try:
            raw = json.loads(INTENSIFIERS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"很": 1.2, "非常": 1.6, "超": 1.5, "太": 1.4, "有點": 0.8}
        return {str(k).lower(): float(v) for k, v in raw.items()}

    return _cached_data(INTENSIFIERS_PATH, _read)


def _regex_tokenize(text):
    return re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", text)


def _jieba_tokenize(text):
    if jieba is None:
        return _regex_tokenize(text)
    return [token for token in jieba.cut(text, cut_all=False) if token and not token.isspace()]


def tokenize_feedback(text, *, stop_words=None, synonyms=None):
    normalized = (text or "").lower()
    raw_tokens = _jieba_tokenize(normalized)
    active_stop_words = stop_words or load_stop_words()
    active_synonyms = synonyms or load_synonyms()
    tokens = []
    for token in raw_tokens:
        clean_token = token.strip()
        if len(clean_token) < 2:
            continue
        canonical = active_synonyms.get(clean_token, clean_token)
        if canonical in active_stop_words:
            continue
        tokens.append(canonical)
    return tokens


def keyword_counts(values, *, stop_words=None, synonyms=None):
    counts = Counter()
    for value in values:
        counts.update(tokenize_feedback(value, stop_words=stop_words, synonyms=synonyms))
    return counts


def build_analysis_text(value):
    tokens = tokenize_feedback(value)
    return " ".join(tokens) if tokens else None


def estimate_sentiment_score(text, *, profile=None):
    normalized = (text or "").lower()
    raw_tokens = (re.findall(r"[a-z]+(?:'[a-z]+)?", normalized) if profile is not None else
                  [token.strip() for token in _jieba_tokenize(normalized) if token and token.strip()])
    if not raw_tokens:
        return None

    # Sentiment should prefer original wording to avoid losing polarity
    # when business-oriented synonym mapping normalizes words (e.g. 偏高 -> 價格).
    tokens = raw_tokens
    positive_words = set(profile["positive"]) if profile is not None else load_positive_words()
    negative_words = set(profile["negative"]) if profile is not None else load_negative_words()
    negation_words = set(profile["negation"]) if profile is not None else load_negation_words()
    intensifiers = profile["intensifiers"] if profile is not None else load_intensifiers()

    score_sum = 0.0
    sentiment_hits = 0
    for idx, token in enumerate(tokens):
        if token in positive_words:
            base = 1.0
        elif token in negative_words:
            base = -1.0
        else:
            continue

        sentiment_hits += 1
        window_tokens = tokens[max(0, idx - 2) : idx]
        negation_count = sum(1 for item in window_tokens if item in negation_words)
        intensity_weight = 1.0
        for item in window_tokens:
            intensity_weight *= intensifiers.get(item, 1.0)
        if negation_count % 2 == 1:
            base *= -1.0
        score_sum += base * intensity_weight

    # Fallback phrase-level detection for terms that might not be segmented as expected.
    if sentiment_hits == 0:
        if profile is not None:
            return None  # Unknown coverage is not neutral; do not substring-match English.
        positive_phrase_hits = sum(1 for term in positive_words if len(term) >= 2 and term in normalized)
        negative_phrase_hits = sum(1 for term in negative_words if len(term) >= 2 and term in normalized)
        if positive_phrase_hits == 0 and negative_phrase_hits == 0:
            return 0.0
        score = (positive_phrase_hits - negative_phrase_hits) / max(
            positive_phrase_hits + negative_phrase_hits, 1
        )
        score = max(-1.0, min(1.0, score))
        return round(score, 3)

    if sentiment_hits == 0:
        return 0.0
    score = score_sum / sentiment_hits
    score = max(-1.0, min(1.0, score))
    return round(score, 3)
