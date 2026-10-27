"""Gói gán nhãn cho người (không có chữ ký duyệt): lấy dev API cases + contract
traces, để trống expected để người điền. Chạy:
python3 scripts/make_labeling_pack.py -> reports/human_labeling_pack.jsonl
"""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cases = [json.loads(l) for l in open(os.path.join(ROOT, "datasets", "development", "api_cases.jsonl"))]
out = []
for c in cases[:60]:
    out.append(dict(case_id=c["id"], family=c["family"], postcard=c["postcard"],
                    comment=c["comment"], parent=c.get("parent", ""),
                    label_topics=[], label_routing="", label_notes="",
                    labeled_by="", labeled_at="", status="UNLABELED"))
with open(os.path.join(ROOT, "reports", "human_labeling_pack.jsonl"), "w") as f:
    for o in out: f.write(json.dumps(o, ensure_ascii=False) + "\n")
print(f"labeling pack: {len(out)} cases, all UNLABELED (no human signature claimed)")
