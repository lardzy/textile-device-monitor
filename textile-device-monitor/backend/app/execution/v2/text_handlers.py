"""General text tokenization; choosing and composing business names stays in JSON."""

from functools import lru_cache
import logging
import re
import unicodedata


@lru_cache(maxsize=1)
def _tokenizer():
    import jieba

    jieba.setLogLevel(logging.WARNING)
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    return tokenizer


def segment(context):
    texts = context.input_data.get("texts") or [context.input_data.get("text") or ""]
    texts = [unicodedata.normalize("NFKC", value) for value in texts]
    limit = context.node["config"].get("max_tokens", 200)

    def collect(values):
        seen = set()
        tokens = []
        for value in values:
            word = value.strip()
            if not word or not any(char.isalnum() for char in word) or word in seen:
                continue
            if len(tokens) == limit:
                return tokens, True
            seen.add(word)
            tokens.append(word)
        return tokens, False

    try:
        tokenizer = _tokenizer()
        tokens, truncated = collect(word for text in texts for word in tokenizer.cut(text, cut_all=False))
        method = "jieba"
    except Exception:
        # Optional dictionary failures must still allow an operator to type a value.
        tokens, truncated = collect(word for text in texts for word in re.split(r"[\s,，、;；/\\|()\[\]【】_-]+", text))
        method = "separator_fallback"
    return {"tokens": tokens, "method": method, "truncated": truncated}


NATIVE_HANDLERS = {("text.segment", 1): segment}
