"""Chuẩn hoá tiếng Việt phục vụ matching/evidence — không phải bộ hiểu ngữ nghĩa."""
from __future__ import annotations
import re, unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").replace("đ", "d").replace("Đ", "D")

def norm(s: str) -> str:
    s = strip_diacritics(s.lower())
    return re.sub(r"\s+", " ", s).strip()

VEHICLES = {"creta": "Creta", "tucson": "Tucson", "santa fe": "Santa Fe",
            "santafe": "Santa Fe", "venue": "Venue", "palisade": "Palisade",
            "accent": "Accent", "elantra": "Elantra", "custin": "Custin"}
VARIANTS = {"tieu chuan": "Tiêu chuẩn", "cao cap": "Cao cấp", "dac biet": "Đặc biệt",
            "ban cao": "Cao cấp", "ban thap": "Tiêu chuẩn", "ban giua": "Đặc biệt"}
COLORS = {"trang": "Trắng", "den": "Đen", "do": "Đỏ", "xanh": "Xanh",
          "bac": "Bạc", "xam": "Xám"}
LOCATIONS = {"ha noi": "Hà Nội", "hn": "Hà Nội", "ho chi minh": "TP.HCM",
             "hcm": "TP.HCM", "sai gon": "TP.HCM", "da nang": "Đà Nẵng"}

def canon_vehicle(raw: str | None):
    if not raw: return None
    return VEHICLES.get(norm(raw), raw)

def canon_variant(raw: str | None):
    if not raw: return None
    n = norm(raw)
    # "Bản X" là tiền tố trình bày, không phải một phiên bản khác: "bản tiêu
    # chuẩn" phải về "Tiêu chuẩn" như "bản cao" về "Cao cấp" (trước đây chỉ
    # có entry "ban cao" nên "ban tieu chuan" rơi qua lookup -> fact miss).
    # Thử nguyên chuỗi trước (giữ "ban cao"), rồi mới strip tiền tố "ban ".
    if n in VARIANTS: return VARIANTS[n]
    if n.startswith("ban "): n = n[4:]
    return VARIANTS.get(n, raw)

def _strip_prefixes(n: str) -> str:
    for pre in ("mau ", "o ", "tai ", "xe "):
        if n.startswith(pre): n = n[len(pre):]
    return n

def canon_color(raw: str | None):
    if not raw: return None
    n = _strip_prefixes(norm(raw))
    return COLORS.get(n, raw)

def canon_location(raw: str | None):
    if not raw: return None
    n = _strip_prefixes(norm(raw))
    return LOCATIONS.get(n, raw)

PRICE_KINDS = {"niem yet": "list", "list": "list", "lan banh": "otr", "otr": "otr",
               "gia lan banh": "otr", "gia niem yet": "list"}
def canon_price_kind(raw):
    if raw is None: return None
    return PRICE_KINDS.get(norm(str(raw)))

def canon_slot(kind: str, raw):
    if raw is None: return None
    if kind == "vehicle": return canon_vehicle(str(raw))
    if kind == "variant": return canon_variant(str(raw))
    if kind == "color": return canon_color(str(raw))
    if kind == "location": return canon_location(str(raw))
    if kind == "price_kind": return canon_price_kind(raw)
    return raw

YES = {"oke", "ok", "u", "uk", "dc", "duoc", "dong y", "chot", "xac nhan", "vang",
       "co", "co a", "da co", "da co a", "vang co", "vang co a", "👍", "👌"}
NO = {"khong", "ko", "k", "khong dong y", "huy", "thoi", "👎"}

def yes_no(text: str):
    """Trả về True/False/None. Chỉ dùng khi đã có pending yes/no cụ thể."""
    n = re.sub(r"[,.!?;:]+", " ", norm(text))
    n = " ".join(n.split())
    if n in YES: return True
    if n in NO: return False
    if re.fullmatch(r"(oke|ok|u|dc|duoc|dong y|chot)", n): return True
    if not re.search(r"\b(khong|ko|k|chua|doi|huy)\b", n) and n.startswith(
            ("duoc xac nhan", "minh xac nhan", "xac nhan", "co xac nhan",
             "dong y", "minh dong y")):
        return True
    return None

WEEKDAYS = {"thu hai": 0, "thu 2": 0, "thu ba": 1, "thu 3": 1, "thu tu": 2,
            "thu 4": 2, "thu nam": 3, "thu 5": 3, "thu sau": 4, "thu 6": 4,
            "thu bay": 5, "thu 7": 5, "chu nhat": 6, "chu nhut": 6}

def parse_time_expression(text: str, now: datetime):
    """Phân tích thời gian thành (kind, value), timezone Asia/Ho_Chi_Minh:
    - ("exact", datetime): ngày/giờ xác định. Giờ mặc định (sáng 9h, chiều 15h,
      khác 10h) CHỈ là đề xuất, không phải giờ khách đã chốt.
    - ("range", (start, end, label, [options])): khoảng có các ngày cụ thể để
      chọn (VD cuối tuần này -> thứ 7, chủ nhật).
    - ("time_only", hour): chỉ có buổi, không có ngày -> engine kết hợp với
      ngày của đề xuất hiện có nếu có, ngược lại hỏi lại.
    - ("past", datetime): thời điểm đã qua -> không được đặt slot.
    - ("vague", reason): không đủ thông tin -> hỏi lại, không đoán.
    Không dùng guard chặn mọi chuỗi có 'tuần/tháng': thứ + tuần sau, ngày +
    tháng cụ thể đều được phân tích; chỉ 'cuối tháng', 'tuần sau', 'tháng sau'
    không kèm ngày mới là vague."""
    base = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    raw = text.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", raw):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ("vague", "invalid_datetime")
        d = d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
        return ("past", d) if d < base else ("exact", d)
    n = norm(text)
    hour = 15 if re.search(r"\bchieu\b", n) else 9 if re.search(r"\bsang\b", n) \
        else 19 if re.search(r"\btoi\b", n) else 10
    clock = re.search(r"\b(?:luc\s*)?(\d{1,2})(?:\s*h\s*(\d{1,2})?|\s*:\s*(\d{2})|\s+gio(?:\s+(\d{1,2}))?)\b", n)
    minutes = 0
    if clock:
        hour = int(clock.group(1))
        minutes = int(clock.group(2) or clock.group(3) or clock.group(4) or 0)
        if hour < 12 and ("chieu" in n or "toi" in n):
            hour += 12
        if hour > 23 or minutes > 59:
            return ("vague", "invalid_time")
    # Ngày cụ thể: "ngay 3 thang 11", "ngay 3 thang 11 nam 2026", "3/11", "3-11".
    m = re.search(r"ngay\s+(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam\s+(\d{4}))?", n) \
        or re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?", n)
    if m:
        y = int(m.group(3)) if m.group(3) else base.year
        try:
            d = base.replace(year=y, month=int(m.group(2)), day=int(m.group(1)),
                             hour=hour, minute=minutes, second=0, microsecond=0)
        except ValueError:
            return ("vague", "invalid_date")
        return ("past", d) if d < base else ("exact", d)
    # Thứ trong tuần: "thu ba tuan sau" -> thứ ba của tuần sau (tính từ thứ 2).
    wd = None
    for name, num in WEEKDAYS.items():
        if re.search(r"\b" + re.escape(name) + r"\b", n):
            wd = num; break
    if wd is not None:
        monday = base.date() - timedelta(days=base.weekday())
        if "tuan sau" in n:
            day = monday + timedelta(days=7 + wd)
        elif "tuan nay" in n:
            day = monday + timedelta(days=wd)
        else:
            day = base.date() + timedelta(days=(wd - base.weekday()) % 7)
        d = datetime(day.year, day.month, day.day, hour, minutes, tzinfo=TZ)
        return ("past", d) if d < base else ("exact", d)
    if "cuoi tuan" in n:
        monday = base.date() - timedelta(days=base.weekday())
        sat, sun = monday + timedelta(days=5), monday + timedelta(days=6)
        opts = [d for d in (sat, sun) if d >= base.date()]
        if not opts:
            return ("vague", "weekend_passed")
        return ("range", (datetime(sat.year, sat.month, sat.day, 10, tzinfo=TZ),
                          datetime(sun.year, sun.month, sun.day, 10, tzinfo=TZ),
                          "cuoi tuan nay", opts))
    if "cuoi thang" in n:
        return ("vague", "month_end_unspecified")
    if "tuan sau" in n or "thang sau" in n:
        return ("vague", "week_or_month_unspecified")
    if re.search(r"\bmai\b", n):
        d = base + timedelta(days=1)
        return ("exact", d.replace(hour=hour, minute=minutes, second=0, microsecond=0))
    if re.search(r"\bhom nay\b", n) or re.search(r"\bnay\b", n):
        d = base.replace(hour=hour, minute=minutes, second=0, microsecond=0)
        return ("past", d) if d < base else ("exact", d)
    if clock:
        return ("time_only", (hour, minutes))
    if re.search(r"\b(sang|chieu|toi)\b", n):
        return ("time_only", hour)
    return ("vague", "no_date_info")

def parse_relative_datetime(text: str, now: datetime):
    """Wrapper tương thích cũ: chỉ exact mới trả datetime, còn lại None."""
    kind, val = parse_time_expression(text, now)
    return val if kind == "exact" else None

def fmt_money(vnd: int) -> str:
    return f"{vnd:,} đồng".replace(",", ".")
