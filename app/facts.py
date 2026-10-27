"""Facts DB (SAMPLE, giả lập hoàn toàn) + resolver + calculator. Code chọn fact,
không thực thi SQL do model sinh. Phân biệt valid (trong khoảng) và fresh (TTL)."""
from __future__ import annotations
import copy
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

class Facts:
    def __init__(self, facts=None, verified_at=None):
        # Deep copy: thay đổi facts trong một engine (VD test làm hết hạn fact)
        # không được rò sang SEED_FACTS dùng chung của engine khác.
        self.facts = copy.deepcopy(list(facts if facts is not None else SEED_FACTS))
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
