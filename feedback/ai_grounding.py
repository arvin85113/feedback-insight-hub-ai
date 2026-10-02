"""Evidence-grounded numbers for AI-generated text.

Generated prose may quote a number only when it restates a value carried by
the evidence the finding cites (its value, sample size, or a number printed in
its label).  Anything else is treated as an invented figure and rejected.
"""

import math
import re

# Chinese-numeral percentages cannot be checked reliably; require digits instead.
UNTRUSTED_CHINESE_NUMBER_RE = re.compile(r"百分之[零〇一二兩三四五六七八九十百千萬億]+")
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![A-Za-z0-9.])\d+(?:\.\d+)?")
_P_VALUE_THRESHOLDS = (0.05, 0.01, 0.001)


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _label_numbers(row):
    texts = [row.get("label"), row.get("test_name")]
    texts += [item for item in row.get("variables") or [] if isinstance(item, str)]
    tokens = set()
    for text in texts:
        if isinstance(text, str):
            tokens.update(token.replace(",", "") for token in _NUMBER_TOKEN_RE.findall(text))
    return tokens


def grounded_numbers(evidence_rows):
    """Return (candidate values, literal label tokens) usable in generated text."""

    values, literals = [], set()
    for row in evidence_rows:
        if not isinstance(row, dict):
            continue
        for key in ("value", "sample_size"):
            number = _numeric(row.get(key))
            if number is None:
                continue
            values.append(number)
            if number < 0:
                # Prose usually states the sign in words ("負相關"), so it quotes the magnitude.
                values.append(-number)
            elif number <= 1:
                values.append(number * 100)  # proportions quoted as percentages
        if row.get("metric_type") == "p_value" or row.get("kind") == "statistical_test":
            values.extend(_P_VALUE_THRESHOLDS)
        literals |= _label_numbers(row)
    return values, literals


def _token_is_grounded(token, values, literals):
    plain = token.replace(",", "")
    if plain in literals:
        return True
    number = float(plain)
    decimals = len(plain.split(".", 1)[1]) if "." in plain else 0
    return any(round(value, decimals) == number for value in values)


def number_tokens(text):
    """Arabic-numeral tokens in ``text`` (the same tokens the grounding check inspects)."""

    return _NUMBER_TOKEN_RE.findall(text)


class UngroundedNumbers(ValueError):
    """Validation failure caused by invented figures.

    ``str(exc)`` stays the short reason code used in metrics; ``numbers`` keeps
    only the offending number tokens (never surrounding text) for diagnosis.
    """

    def __init__(self, reason, numbers):
        super().__init__(reason)
        self.numbers = list(numbers)


def ungrounded_numbers(text, evidence_rows):
    """List number tokens in ``text`` that the cited evidence does not support."""

    if UNTRUSTED_CHINESE_NUMBER_RE.search(text):
        return [UNTRUSTED_CHINESE_NUMBER_RE.search(text).group(0)]
    tokens = _NUMBER_TOKEN_RE.findall(text)
    if not tokens:
        return []
    values, literals = grounded_numbers(evidence_rows)
    return [token for token in tokens if not _token_is_grounded(token, values, literals)]
