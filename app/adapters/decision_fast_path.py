"""Narrow Jev fast path for short, slotless social acknowledgements only."""
from __future__ import annotations

import math
import re

from ..normalize import norm
from ..semantics import validate_semantic


def social_ack_eligible(comment):
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 500:
        return False
    normalized = norm(comment)
    return bool(re.fullmatch(r"(?:(?:xin )?chao(?: ban)?|cam on(?: ban)?|thanks|thank you)[.! ]*|👍|👌",
                             normalized))


def social_ack_candidate(comment, answer, threshold):
    """Return a validated v1 social semantic only with an explicit calibrated cutoff."""
    if (not social_ack_eligible(comment) or type(threshold) not in (int, float) or
            not math.isfinite(threshold) or not 0 <= threshold <= 1):
        return None
    probability = answer.get("noul") if isinstance(answer, dict) else None
    if type(probability) not in (int, float) or not math.isfinite(probability) or probability < threshold:
        return None
    semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
                "scope": "in_scope",
                "requests": [{"topic": "social", "desired_outcome": "social_ack", "polarity": "positive"}],
                "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": comment.strip()}],
                "uncertainties": []}
    return semantic if not validate_semantic(semantic, comment, []) else None
