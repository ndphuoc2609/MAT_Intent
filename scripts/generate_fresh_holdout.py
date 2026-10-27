"""Fresh holdout cho Luna (sinh SAU vòng sửa dev, chưa từng dùng để sửa).
24 ca, phrasing/xe/tình huống mới so với contract set. Kỳ vọng viết tay theo spec,
không chạy engine để sinh kỳ vọng. Chạy 1 lần, không sửa theo kết quả này.
"""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PC = lambda v: dict(topic=f"{v} 2026", vehicles=[v], persona_default="tư vấn viên")
PCM = dict(topic="Nhiều xe Hyundai", vehicles=["Creta", "Tucson"], persona_default="tư vấn viên")
CASES = [
    ("fh_price_otr", "price", PC("Creta"), "Bản tiêu chuẩn lăn bánh ở HCM hết bao nhiêu tiền?", ["price"], "AUTO"),
    ("fh_price_list", "price", PC("Tucson"), "Tucson bản cao giá niêm yết là bao nhiêu ạ?", ["price"], "AUTO"),
    ("fh_price_no_variant", "price", PC("Creta"), "Creta giá bao nhiêu vậy ad?", ["price"], "ASK"),
    ("fh_promo", "promo", PC("Creta"), "Tháng này Creta có ưu đãi gì hot không ad?", ["promo"], "AUTO"),
    ("fh_loan_docs", "loan", PC("Creta"), "Mua trả góp cần giấy tờ gì, trả trước bao nhiêu thì được?", ["loan"], "AUTO"),
    ("fh_loan_unit_missing", "loan", PC("Creta"), "Trả trước 150 có mua được Creta không?", ["loan"], "ASK"),
    ("fh_stock_color", "stock", PC("Creta"), "Creta bản cao còn màu đen không ad?", ["stock"], "AUTO"),
    ("fh_stock_no_vehicle", "stock", PCM, "Xe này còn màu trắng không?", ["stock"], "REVIEW"),
    ("fh_spec", "spec", PC("Tucson"), "Tucson có bao nhiêu chỗ ngồi, động cơ mấy chấm?", ["spec"], "AUTO"),
    ("fh_dealer", "dealer", PC("Creta"), "Cho mình địa chỉ đại lý ở Đà Nẵng với", ["dealer"], "HANDOFF"),
    ("fh_dealer_hn", "dealer", PC("Creta"), "Đại lý Hyundai ở Hà Nội nằm ở đường nào?", ["dealer"], "AUTO"),
    ("fh_testdrive", "testdrive", PC("Creta"), "Mình muốn lái thử Creta cuối tuần này ở Hà Nội được không?", ["testdrive"], "ASK"),
    ("fh_buy", "buy", PC("Creta"), "Mình chốt mua Creta bản cao, cần làm gì tiếp?", ["buy"], "HANDOFF"),
    ("fh_complaint", "complaint", PC("Creta"), "Xe mình mới lấy mà đã lỗi cảm biến, đại lý không xử lý", ["complaint"], "HANDOFF"),
    ("fh_service", "service", PC("Creta"), "Bảo dưỡng Creta định kỳ bao lâu một lần?", ["service"], "HANDOFF"),
    ("fh_registration", "registration", PC("Creta"), "Đăng ký biển Hà Nội cho Creta tốn bao nhiêu?", ["registration"], "AUTO"),
    ("fh_sarcasm", "sarcasm", PC("Creta"), "Giá rẻ quá, chắc bán cho vui thôi chứ ai mua nổi 😂", ["social"], "REVIEW"),
    ("fh_social", "social", PC("Creta"), "Xe đẹp quá ad ơi, nhìn mê luôn", ["social"], "AUTO"),
    ("fh_spam", "scope", PC("Creta"), "Bên mình cho vay lãi suất thấp, ai cần ib nhé", [], "REVIEW"),
    ("fh_unknown", "scope", PC("Creta"), "Hôm nay ăn gì ngon nhỉ mọi người?", [], "REVIEW"),
    ("fh_multi", "multi", PC("Creta"), "Creta bản cao lăn bánh HN bao nhiêu và có khuyến mãi gì?", ["price", "promo"], "AUTO"),
    ("fh_neg_variant", "negation", PC("Creta"), "Không lấy bản cao đâu, bản tiêu chuẩn còn trắng không?", ["stock"], "AUTO"),
    ("fh_no_call", "contact", PC("Creta"), "Đừng gọi điện nhé, nhắn giá Creta bản cao ở đây giúp mình", ["price"], "AUTO"),
    ("fh_greeting", "social", PC("Creta"), "Chào buổi sáng ad", ["social"], "AUTO"),
]
os.makedirs(os.path.join(ROOT, "datasets", "holdout_fresh"), exist_ok=True)
with open(os.path.join(ROOT, "datasets", "holdout_fresh", "api_cases.jsonl"), "w") as f:
    for cid, fam, pc, comment, topics, rout in CASES:
        f.write(json.dumps(dict(id=cid, family=fam, split="holdout_fresh", postcard=pc,
                                comment=comment, parent="", expected_topics=topics,
                                expected_routing=rout, label_source="synthetic_contract"),
                         ensure_ascii=False) + "\n")
print(f"fresh holdout: {len(CASES)} cases")
