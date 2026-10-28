"""Facts DB (SAMPLE, giả lập hoàn toàn) + resolver + calculator. Code chọn fact,
không thực thi SQL do model sinh. Phân biệt valid (trong khoảng) và fresh (TTL)."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .normalize import norm


class DealerDirectoryError(RuntimeError):
    """Local dealer data could not be read safely."""


DEALER_DIRECTORY_URL = "https://hyundai.thanhcong.vn/thong-tin-dai-ly"

# Tất cả giá là SAMPLE — không dùng tư vấn thật. Đơn vị VND.
SEED_FACTS = [
    # list price
    dict(id="f_list_creta_std", type="list_price", vehicle="Creta", variant="Tiêu chuẩn",
         location=None, value=599_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_list_creta_high", type="list_price", vehicle="Creta", variant="Cao cấp",
         location=None, value=699_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_list_tucson_high", type="list_price", vehicle="Tucson", variant="Cao cấp",
         location=None, value=899_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    # promo (hết hạn 2026-10-31 để test expired)
    dict(id="f_promo_creta_oct", type="promo", vehicle="Creta", variant=None,
         location=None, value=20_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-10-01", valid_to="2026-10-31", ttl_hours=720, version=1),
    # stock
    dict(id="f_stock_creta_high_white", type="stock", vehicle="Creta", variant="Cao cấp",
         location="Hà Nội", value=1, unit="xe", extra={"color": "Trắng"}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=24, version=1),
    dict(id="f_stock_creta_std_white", type="stock", vehicle="Creta", variant="Tiêu chuẩn",
         location="Hà Nội", value=1, unit="xe", extra={"color": "Trắng"}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=24, version=1),
    # fees cho lăn bánh
    dict(id="f_fee_reg_hn", type="reg_rate", vehicle=None, variant=None,
         location="Hà Nội", value=12, unit="percent", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_fee_reg_hcm", type="reg_rate", vehicle=None, variant=None,
         location="TP.HCM", value=10, unit="percent", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_fee_plate_hn", type="plate_fee", vehicle=None, variant=None,
         location="Hà Nội", value=20_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_fee_plate_hcm", type="plate_fee", vehicle=None, variant=None,
         location="TP.HCM", value=20_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_fee_road", type="road_fee", vehicle=None, variant=None,
         location=None, value=1_560_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    # loan + dealer + service + registration
    dict(id="f_loan_rate", type="loan_rate", vehicle=None, variant=None,
         location=None, value=8.5, unit="percent_per_year", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=720, version=1),
    dict(id="f_dealer_hn", type="dealer", vehicle=None, variant=None,
         location="Hà Nội", value=1, unit="dealer",
         extra={"name": "Đại lý SAMPLE Hà Nội", "address": "123 Đường Mẫu, Hà Nội",
                "hours": "8:00-18:00"}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_dealer_hcm", type="dealer", vehicle=None, variant=None,
         location="TP.HCM", value=1, unit="dealer",
         extra={"name": "Đại lý SAMPLE TP.HCM", "address": "456 Đường Mẫu, TP.HCM",
                "hours": "8:00-18:00"}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_warranty", type="warranty", vehicle=None, variant=None,
         location=None, value=5, unit="years", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_reg_fee_hn", type="registration_fee", vehicle=None, variant=None,
         location="Hà Nội", value=2_000_000, unit="VND", source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
]

def _in_range(f, now):
    if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
    local = ZoneInfo("Asia/Ho_Chi_Minh")
    vf = datetime.fromisoformat(f["valid_from"])
    vt = datetime.fromisoformat(f["valid_to"] + "T23:59:59.999999"
                                if len(f["valid_to"]) == 10 else f["valid_to"])
    vf = vf.replace(tzinfo=local if len(f["valid_from"]) == 10 else vf.tzinfo or timezone.utc)
    vt = vt.replace(tzinfo=local if len(f["valid_to"]) == 10 else vt.tzinfo or timezone.utc)
    return vf <= now <= vt


def load_dealers(path):
    """Read the supplied ``dealers`` table without creating or modifying it.

    Rows have no verification or validity metadata, so the returned facts do
    not claim either. A missing/invalid file is reported as DealerDirectoryError.
    """
    if path is None:
        return []
    db = Path(path)
    if not db.is_file():
        raise DealerDirectoryError("dealer directory file is missing")
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dealers)")}
            if not {"name", "mailing_address"}.issubset(columns):
                raise DealerDirectoryError("dealer directory schema needs name and mailing_address")
            rows = conn.execute(
                "SELECT name, mailing_address FROM dealers ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
    except DealerDirectoryError:
        raise
    except sqlite3.Error:
        raise DealerDirectoryError("dealer directory database is unreadable") from None

    facts = []
    for name, address in rows:
        if not isinstance(name, str) or not name.strip() or not isinstance(address, str) or not address.strip():
            continue
        name, address = name.strip(), address.strip()
        fact_id = hashlib.sha256(f"{name}\0{address}".encode("utf-8")).hexdigest()[:20]
        facts.append(dict(id=f"dealer:{fact_id}", type="dealer", vehicle=None, variant=None,
                          location=None, value=name, unit="dealer",
                          extra={"name": name, "address": address},
                          source="local_dealer_directory"))
    return facts


def _place_text(value):
    """Normalize Vietnamese place names and common administrative abbreviations."""
    value = norm(str(value or ""))
    value = re.sub(r"\btp\.?\s*hcm\b|\btphcm\b|\bhcm\b|\bsai gon\b", "ho chi minh", value)
    value = re.sub(r"\bthanh pho\b", " ", value)
    value = re.sub(r"\btp\.?\b", " ", value)
    value = re.sub(r"\bq\.?\s*(?=\d)", "quan ", value)
    value = re.sub(r"\bquan\b|\bq\.?\b", "quan", value)
    value = re.sub(r"\bh\.?\s*(?=\d)", "huyen ", value)
    value = re.sub(r"\bhuyen\b|\bh\.?\b", "huyen", value)
    value = re.sub(r"\bp\.?\s*(?=\d)", "phuong ", value)
    value = re.sub(r"\bphuong\b|\bp\.?\b", "phuong", value)
    value = re.sub(r"\btinh\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _contains_place(text, place):
    source = _place_text(text).split()
    target = _place_text(place).split()
    if not source or not target:
        return False
    return any(source[i:i + len(target)] == target for i in range(len(source) - len(target) + 1))


_DEALER_PHONE = re.compile(r"(?<!\w)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\w)")
_ADDRESS_MARKER = re.compile(
    r"\b(?:so\s+\d+|duong|quoc\s+lo|ql|lo|km|khu|phuong|xa|tp|thanh\s+pho|"
    r"tinh|quan|q\.?\s*(?=[a-z0-9])|huyen|ap|thon|to|kcn)\b"
)
_NUMBERED_ADDRESS = re.compile(r"^\d+[a-z]?(?:\s*[-/]\s*\d+[a-z]?)*(?=\s+\D)")


def _fold_with_offsets(text):
    """Fold Vietnamese text while preserving spans into the original string."""
    folded, offsets = [], []
    for original_index, char in enumerate(text):
        base = unicodedata.normalize("NFD", char)
        base = "".join(c for c in base if unicodedata.category(c) != "Mn")
        base = base.replace("đ", "d").replace("Đ", "D").lower()
        for item in base:
            if item.isspace():
                if folded and folded[-1] != " ":
                    folded.append(" "); offsets.append(original_index)
            else:
                folded.append(item); offsets.append(original_index)
    if folded and folded[-1] == " ":
        folded.pop(); offsets.pop()
    return "".join(folded), offsets


def _address_start(folded):
    numbered = _NUMBERED_ADDRESS.match(folded)
    if numbered:
        return numbered.start()
    marker = _ADDRESS_MARKER.search(folded)
    return marker.start() if marker else None


def _original_index(offsets, folded_index):
    return offsets[folded_index] if folded_index < len(offsets) else offsets[-1] + 1


def dealer_matches_location(fact, location):
    """Match only explicit, sanitized address text; never use contact names."""
    if not location:
        return False
    ex = fact.get("extra") or {}
    text = dealer_public_address(ex.get("address"))
    # Legacy injected fixtures may expose only a location column. Directory
    # names are intentionally excluded: some are stale relative to addresses.
    if fact.get("location"):
        text += " " + str(fact["location"])
    # Comma-separated district + city inputs require every supplied part.
    parts = [part.strip() for part in re.split(r"[,;/]+", str(location)) if part.strip()]
    return bool(parts) and all(_contains_place(text, part) for part in parts)


def dealer_public_address(address):
    """Return safe address lines; ambiguous lines containing phones are omitted."""
    lines = []
    for line in str(address or "").splitlines():
        line = line.strip()
        if not line:
            continue
        normalized, offsets = _fold_with_offsets(line)
        phones = list(_DEALER_PHONE.finditer(normalized))
        if phones:
            if len(phones) != 1:
                return ""
            phone = phones[0]
            suffix_start = phone.end()
            while suffix_start < len(normalized) and normalized[suffix_start].isspace():
                suffix_start += 1
            suffix_address_start = _address_start(normalized[suffix_start:])
            if suffix_address_start is not None:
                # Contact prefix + phone + address: keep only the address suffix.
                start = suffix_start + suffix_address_start
                line = line[_original_index(offsets, start):]
            else:
                address_start = _address_start(normalized[:phone.start()])
                if address_start is None:
                    # Standalone contact line: another clean address line may follow.
                    continue
                start = _original_index(offsets, address_start)
                phone_start = _original_index(offsets, phone.start())
                candidate = line[start:phone_start]
                # A spaced dash separates the address from a trailing contact name;
                # a dash followed by digits (e.g. "94 -96") remains an address range.
                candidate_folded, candidate_offsets = _fold_with_offsets(candidate)
                contact = re.search(r"\s+[-–]\s+(?=[^\d\s])", candidate_folded)
                if not contact:
                    return ""
                contact_start = _original_index(candidate_offsets, contact.start())
                line = candidate[:contact_start]
            if _address_start(_fold_with_offsets(line)[0]) is None:
                continue
            line = line.strip(" -–,:;")
        else:
            address_start = _address_start(normalized)
            if address_start is None:
                # Contact names without a phone are still not address evidence.
                continue
            original_start = _original_index(offsets, address_start)
            if re.match(r"^o\s+so\s+\d+", normalized):
                original_start = 0  # Keep a legitimate "Ô số ..." reference.
            line = line[original_start:]
            line_folded, line_offsets = _fold_with_offsets(line)
            contact = re.search(r"\s+[-–]\s+(?=[^\d\s])", line_folded)
            if contact:
                line = line[:_original_index(line_offsets, contact.start())]
        if line:
            lines.append(line)
    return ", ".join(lines)

class Facts:
    def __init__(self, facts=None, verified_at=None, dealer_facts=None):
        # Deep copy: thay đổi facts trong một engine (VD test làm hết hạn fact)
        # không được rò sang SEED_FACTS dùng chung của engine khác.
        base_facts = list(facts if facts is not None else SEED_FACTS)
        self.dealer_directory_configured = dealer_facts is not None
        if dealer_facts is not None:
            base_facts = [f for f in base_facts if f.get("type") != "dealer"] + list(dealer_facts)
        self.facts = copy.deepcopy(base_facts)
        # verified_at semantics (R07): None KHÔNG có nghĩa "bỏ TTL vô thời hạn".
        # None = verified tại thời điểm Facts instance này được tạo (giờ thật
        # lúc build); TTL tính từ đó. Muốn stale thì truyền verified_at quá khứ.
        # Fact riêng có verified_at thì thắng verified_at chung.
        self.verified_at = verified_at or datetime.now(timezone.utc)
        self.version = 0  # tăng mỗi lần facts thay đổi (dispatch recheck)
    def touch(self):
        self.version += 1
    def is_fresh(self, fact, now):
        """Một fact có fresh tại `now` không: trong valid_from/to VÀ trong TTL
        tính từ verified_at hiệu lực (riêng của fact, nếu có, rồi chung)."""
        if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
        if fact.get("source") == "local_dealer_directory":
            return False
        va = fact.get("verified_at") or self.verified_at
        if va is not None and va.tzinfo is None: va = va.replace(tzinfo=timezone.utc)
        in_ttl = (now - va).total_seconds() <= fact.get("ttl_hours", 8760) * 3600
        return _in_range(fact, now) and in_ttl
    def query(self, ftype, vehicle=None, variant=None, location=None, extra=None, now=None):
        """Trả (fresh, expired_or_stale, conflict). Conflict = 2 fresh khác value.
        Fresh = trong khoảng valid_from/to VÀ trong TTL (xem is_fresh). Kết quả
        sắp xếp theo id nên độc lập với thứ tự bản ghi trong seed: không dừng ở
        fact expired đầu tiên, luôn xét hết. Lưu ý: fact có vehicle/variant/
        location = None là fact chung (VD phí), khớp mọi giá trị; fact có giá
        trị khác với giá trị request yêu cầu thì KHÔNG khớp (không bỏ dimension
        để khớp được dữ liệu)."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
        fresh, stale = [], []
        for f in self.facts:
            if f["type"] != ftype: continue
            # Directory addresses have no verification timestamp or TTL. They
            # are queried only through directory_dealers(), never as fresh facts.
            if ftype == "dealer" and f.get("source") == "local_dealer_directory": continue
            if vehicle and f.get("vehicle") not in (None, vehicle): continue
            if variant and f.get("variant") not in (None, variant): continue
            if location and f.get("location") not in (None, location): continue
            if extra and any(f.get("extra", {}).get(k) != v for k, v in extra.items()): continue
            (fresh if self.is_fresh(f, now) else stale).append(f)
        fresh.sort(key=lambda f: f["id"])
        vals = {(f["value"], f["unit"], json.dumps(f.get("extra") or {}, sort_keys=True)
                 if ftype in ("spec", "dealer") else None)
                for f in fresh}
        return fresh, stale, (len(vals) > 1)

    def directory_dealers(self, location):
        """Return explicit address matches without claiming freshness or distance."""
        if not self.dealer_directory_configured or not location:
            return []
        return [f for f in self.facts
                if f.get("type") == "dealer"
                and f.get("source") == "local_dealer_directory"
                and dealer_matches_location(f, location)]

def calc_otr(list_price: int, reg_rate_pct: float, plate: int, road: int):
    """Công thức v1: OTR = list + list*reg% + plate + road. Output trỏ về inputs."""
    reg = int(round(list_price * reg_rate_pct / 100))
    total = list_price + reg + plate + road
    return dict(formula="otr_v1", inputs=dict(list_price=list_price, reg_rate_pct=reg_rate_pct,
                plate_fee=plate, road_fee=road, reg_fee=reg), total=total)

# Spec facts (SAMPLE) — thêm sau seed chính để spec/compare có dữ liệu mẫu.
SEED_FACTS += [
    dict(id="f_spec_creta", type="spec", vehicle="Creta", variant=None,
         location=None, value=1, unit="spec",
         extra={"engine": "1.5L", "seats": 5, "power_hp": 115}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
    dict(id="f_spec_tucson", type="spec", vehicle="Tucson", variant=None,
         location=None, value=1, unit="spec",
         extra={"engine": "2.0L", "seats": 5, "power_hp": 156}, source="SAMPLE",
         valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=8760, version=1),
]
