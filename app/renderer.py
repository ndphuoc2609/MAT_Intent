"""Renderer: template trước, không gọi model viết lại ở baseline.
Claim check: mọi con số trong text phải thuộc facts_used / computed / user slots,
và phải đúng đối tượng / thuộc tính / đơn vị của fact đã chọn — không chỉ
"số có trong DB". Đường chứng minh: fact ID -> đối tượng -> thuộc tính ->
giá trị/đơn vị -> claim."""
from __future__ import annotations
import re
from .normalize import fmt_money, norm, VEHICLES

UNIT_MAP = {"VND": "đồng", "percent": "%", "percent_per_year": "%",
            "years": "năm", "L": "L", "xe": "xe", "dealer": "dealer", "spec": ""}
SPEC_KEYS = ("engine", "seats", "power_hp")

RENDER_STYLES = frozenset({"neutral", "warm", "professional", "concise"})
_STYLE_BY_TONE = {
    "neutral": "neutral", "trung tính": "neutral",
    "warm": "warm", "friendly": "warm", "ấm áp": "warm", "thân thiện": "warm",
    "professional": "professional", "formal": "professional",
    "chuyên nghiệp": "professional", "trang trọng": "professional",
    "concise": "concise", "direct": "concise", "ngắn gọn": "concise",
}
_MAX_TONE_DESCRIPTION_CHARS = 240
_DESCRIPTION_STYLE_PHRASES = {
    "neutral": ("trung tính", "neutral"),
    "warm": ("nhẹ nhàng", "ấm áp", "thân thiện", "warm", "friendly"),
    "professional": ("chuyên nghiệp", "trang trọng", "professional", "formal"),
    "concise": ("ngắn gọn", "concise", "direct"),
}
_NEGATED_TONE_PREFIX = re.compile(
    r"(?<!\w)(?:không|chẳng|chả|chưa|not|never)"
    r"(?:\s+(?:hề|quá|mấy|lắm|thật|thực|sự|phải|có|được|nên|còn|là|kiểu|"
    r"hẳn|khá|vẻ|đặc\s+biệt|very|really|too|quite|particularly)){0,2}\s*$"
)

# Variants are whole, engine-authored clarification prompts. Facts and any
# unlisted text pass through unchanged; this renderer never paraphrases claims.
_PROMPT_VARIANTS = {
    "warm": {
        "Bạn đang hỏi giá xe nào ạ?": "Bạn đang hỏi giá mẫu xe nào vậy?",
        "Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?":
            "Bạn muốn xem giá niêm yết hay giá lăn bánh nhỉ?",
        "Bạn đang ở khu vực nào (tỉnh/thành phố hoặc quận/huyện) ạ?":
            "Bạn đang ở tỉnh/thành phố hay quận/huyện nào vậy?",
    },
    "professional": {
        "Bạn đang hỏi giá xe nào ạ?": "Bạn muốn tham khảo giá mẫu xe nào?",
        "Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?":
            "Bạn muốn tham khảo giá niêm yết hay giá lăn bánh?",
        "Bạn đang ở khu vực nào (tỉnh/thành phố hoặc quận/huyện) ạ?":
            "Vui lòng cho biết tỉnh/thành phố hoặc quận/huyện của bạn.",
    },
    "concise": {
        "Bạn đang hỏi giá xe nào ạ?": "Bạn hỏi giá xe nào?",
        "Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?":
            "Bạn muốn giá niêm yết hay giá lăn bánh?",
        "Bạn đang ở khu vực nào (tỉnh/thành phố hoặc quận/huyện) ạ?":
            "Bạn ở tỉnh/thành phố hoặc quận/huyện nào?",
    },
}


def style_from_tone(tone: str | None) -> str:
    """Map an exact label or one unambiguous, known phrase to a safe style.

    Free-form persona text is not interpreted as instructions. Unknown values
    use the backward-compatible neutral style. Descriptions are length-limited;
    embedded phrases count only when exactly one style category is positive.
    """
    if not isinstance(tone, str):
        return "neutral"
    normalized = " ".join(tone.casefold().split())
    exact_style = _STYLE_BY_TONE.get(normalized)
    if exact_style:
        return exact_style
    if len(normalized) > _MAX_TONE_DESCRIPTION_CHARS:
        return "neutral"

    matched_styles = set()
    for style, phrases in _DESCRIPTION_STYLE_PHRASES.items():
        for phrase in phrases:
            pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
            for match in pattern.finditer(normalized):
                clause_prefix = re.split(
                    r"[,;.!?:]|\b(?:nhưng|tuy nhiên|but|however)\b",
                    normalized[:match.start()])[-1]
                if _NEGATED_TONE_PREFIX.search(clause_prefix):
                    continue
                matched_styles.add(style)
                break
            if style in matched_styles:
                break
    return next(iter(matched_styles)) if len(matched_styles) == 1 else "neutral"


def render(parts: list[str], persona: str = "tư vấn viên", *, style: str = "neutral") -> str:
    """Join engine text, optionally varying only known clarification prompts.

    ``persona`` remains accepted for existing callers. It is intentionally not
    used to create biographical claims or alter facts.
    """
    if not isinstance(style, str) or style not in RENDER_STYLES:
        style = "neutral"
    variants = _PROMPT_VARIANTS.get(style, {})
    return " ".join(variants.get(part, part) for part in parts if part)

def _parse_number(raw: str):
    if "." in raw and len(raw.split(".")[-1]) == 3 and raw.count(".") >= 1:
        return int(raw.replace(".", ""))
    v = raw.replace(",", ".")
    return float(v) if "." in v else int(v)

def claim_check(text: str, allowed_numbers: list, used_facts=None, allowed_vehicles=None) -> list[str]:
    """allowed_numbers: các số (int/float) được phép xuất hiện. Ngày/giờ và
    số thứ tự trong template (VD '5 năm' phải nằm trong allowed) do caller đưa.
    used_facts (nếu có): [{fact_id, vehicle, variant, location, attribute,
    value, unit}] — claim phải khớp đúng fact đã chọn, không chỉ khớp số.
    allowed_vehicles (nếu có): các xe mà reply được phép nhắc tới."""
    errs = []
    allowed = set()
    for v in allowed_numbers:
        allowed.add(str(int(v)) if float(v) == int(float(v)) else str(v))
        allowed.add(str(float(v)))
    # số trong text đã format dạng 699.000.000 -> bỏ dấu chấm nghìn để so
    for m in re.finditer(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?", text):
        raw = m.group(0)
        if "." in raw and len(raw.split(".")[-1]) == 3 and raw.count(".") >= 1:
            val = raw.replace(".", "")
        else:
            val = raw.replace(",", ".")
        vint = str(int(val)) if val.isdigit() else val
        if val not in allowed and raw not in allowed and vint not in allowed:
            errs.append(f"number {raw} not in allowed facts/computed/user values")
    if used_facts is None:
        return errs
    # 1. Xe nhắc tới trong reply phải thuộc request/fact đã chọn.
    if allowed_vehicles:
        nt = norm(text)
        for v in VEHICLES.values():
            if norm(v) in nt and v not in allowed_vehicles:
                errs.append(f"vehicle {v} mentioned but not in used facts/request")
    # 2. Số kèm đơn vị phải khớp đúng (giá trị, đơn vị) của fact đã chọn.
    for m in re.finditer(r"(\d{1,3}(?:\.\d{3})+|\d+(?:[.,]\d+)?)\s*(đồng|%|năm|L|xe)(?!\w)", text):
        raw, unit = m.group(1), m.group(2)
        try: val = _parse_number(raw)
        except Exception: continue
        ok = any(float(u.get("value")) == float(val)
                 and UNIT_MAP.get(u.get("unit"), u.get("unit")) == unit
                 for u in used_facts if isinstance(u.get("value"), (int, float)))
        if not ok:
            errs.append(f"number {raw}{unit} does not match a used fact value/unit")
    # 3. Cặp thuộc tính thông số (engine/seats/power_hp) phải khớp fact đã chọn.
    for m in re.finditer(r"\b(engine|seats|power_hp)\s+(\d+(?:[.,]\d+)?)(L)?", text):
        key, raw = m.group(1), m.group(2)
        try: val = float(raw)
        except Exception: continue
        ok = any(u.get("attribute") == key and isinstance(u.get("value"), (int, float))
                 and float(u["value"]) == val for u in used_facts)
        if not ok:
            errs.append(f"attribute {key}={raw} not in used facts")
    return errs


def claim_check_typed(text: str, claims) -> list[str]:
    """Typed claim binding (R10): số trong đoạn nói về xe X phải khớp claim
    của chính X (vehicle, và variant nếu đoạn nêu variant) — không chỉ khớp
    một số bất kỳ trong union. Số không khớp claim nào thì bỏ qua ở đây
    (claim_check union cũ bắt); số chỉ khớp claim của xe/phiên bản KHÁC thì
    báo lỗi hoán đổi. Làm việc trên text đã norm để khớp tên có dấu."""
    claims = [c for c in (claims or [])
              if isinstance(c, dict) and isinstance(c.get("value"), (int, float))]
    if not claims:
        return []
    nt = norm(text)
    vehicles = sorted({norm(c["vehicle"]) for c in claims if c.get("vehicle")},
                      key=len, reverse=True)
    variants = sorted({norm(c["variant"]) for c in claims if c.get("variant")},
                      key=len, reverse=True)
    mentions = []  # (pos, kind, label)
    for v in vehicles:
        start = 0
        while True:
            i = nt.find(v, start)
            if i < 0: break
            mentions.append((i, "vehicle", v)); start = i + 1
    for v in variants:
        start = 0
        while True:
            i = nt.find(v, start)
            if i < 0: break
            mentions.append((i, "variant", v)); start = i + 1
    mentions.sort()
    errs = []
    for m in re.finditer(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?", nt):
        try: val = float(_parse_number(m.group(0)))
        except Exception: continue
        # Xe gần nhất đứng trước số này; variant gần nhất sau xe đó.
        veh = None; veh_pos = -1
        for (pos, kind, label) in mentions:
            if kind == "vehicle" and pos < m.start() and pos > veh_pos:
                veh, veh_pos = label, pos
        if veh is None: continue  # mở đầu chưa nêu xe: union check lo
        var = None
        for (pos, kind, label) in mentions:
            if kind == "variant" and veh_pos < pos < m.start():
                var = label
        def _match(c, strict_variant):
            if c.get("vehicle") is not None and norm(c["vehicle"]) != veh: return False
            if strict_variant and var is not None and c.get("variant") is not None \
                    and norm(c["variant"]) != var: return False
            return float(c["value"]) == val
        if any(_match(c, True) for c in claims): continue
        others = [c for c in claims if float(c["value"]) == val]
        if others:
            o = others[0]
            errs.append(f"number {m.group(0)} in {veh}/{var or '-'} segment matches "
                        f"only {o.get('vehicle')}/{o.get('variant')} claim (possible swap)")
    return errs
