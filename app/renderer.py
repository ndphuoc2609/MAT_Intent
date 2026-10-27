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

def render(parts: list[str], persona: str = "tư vấn viên") -> str:
    body = " ".join(p for p in parts if p)
    return body

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
