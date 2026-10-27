"""Luna adapter (baseline A): Luna = semantic parser duy nhất.
Đọc OPENROUTER_API_KEY từ môi trường, chỉ gửi qua Bearer header tới
openrouter.ai. Trả về semantic v1 hoặc
ERROR dict — không bao giờ tự bịa extraction khi API lỗi.
Prompt schema v1 đầy đủ nằm trong SYSTEM; xem config/schema_versions/semantic_v1.json.
"""
from __future__ import annotations
import json, os, random, time, urllib.error, urllib.request
from ..semantics import parse_json_strict

MODEL = "openai/gpt-6-luna"
PRICE = {"prompt": 0.0000001, "completion": 0.0000005}

SYSTEM = """Bạn là semantic parser cho comment Facebook tiếng Việt của Page ô tô Hyundai.
CHỈ trả JSON đúng schema v1, không giải thích thêm.
{
 "schema_version": "1",
 "sentiment": "POS|NEG|NEU",
 "sarcasm": "present|absent|uncertain",
 "scope": "in_scope|out_of_scope|unknown",
 "requests": [{"topic": "price|promo|loan|stock|spec|dealer|testdrive|buy|service|complaint|registration|social",
   "desired_outcome": "mô tả ngắn outcome khách muốn, VD price_otr, stock_availability, testdrive_propose",
   "polarity": "positive|negative|conditional|unknown"}],
 "operations": [{"op": "START_REQUEST|UPDATE_SLOT|ANSWER_PENDING|CONFIRM_PENDING|REJECT_PENDING|CANCEL_REQUEST|SET_CONTACT_PREFERENCE|REQUEST_HUMAN|REPORT_COMPLAINT|SOCIAL_ACK|UNRESOLVED",
   "request_ref": "topic:<topic> hoặc id request nếu context cho",
   "slot": {"kind": "vehicle|variant|color|location|price_kind|down_payment|datetime|dealer|service_topic|compare_vehicle", "value": "giá trị thô hoặc số, down_payment là {amount, unit|null}", "explicit": true},
   "evidence_excerpt": "trích nguyên văn từ comment (hoặc parent/root nếu suy luận từ đó)",
   "pending_id": "chỉ khi trả lời/xác nhận pending trong context",
   "scope": "call|public_reply|inbox|all (chỉ cho SET_CONTACT_PREFERENCE)",
   "reason": "chỉ cho UNRESOLVED/REQUEST_HUMAN",
   "human_source": "chỉ cho REQUEST_HUMAN: customer_explicit|model_proposed|policy"}],
 "uncertainties": [{"alternatives": ["..."], "different_action": true|false}]
}
QUY TẮC BẮT BUỘC:
- Mỗi nhu cầu riêng = một request riêng; đa nhu cầu thì nhiều request/operation, không gộp slot chung.
- Phủ định/điều kiện: "không lấy bản cao nữa, lấy bản tiêu chuẩn" = UPDATE_SLOT variant sang Tiêu chuẩn, giữ request mua; "đừng hủy lịch, chỉ đổi sang chiều" = UPDATE_SLOT datetime, KHÔNG CANCEL_REQUEST; "không phải Tucson, Creta" = vehicle Creta, không Tucson.
- "oke/ừ/đc/👍" chỉ là xác nhận khi context có pending yes/no cụ thể; không pending thì SOCIAL_ACK.
- Khen + "chưa có nhu cầu" = social, không buy. Quảng cáo vay của người khác = scope out_of_scope, không loan. "Cho số tư vấn bảo hành" = service, không buy. "Đừng gọi, gửi giá tại đây" = SET_CONTACT_PREFERENCE scope call + request price.
- Khiếu nại, tranh chấp cọc, yêu cầu gặp người = REPORT_COMPLAINT hoặc REQUEST_HUMAN.
- PostCard chỉ là bối cảnh: post nhiều xe + "xe này" chưa rõ thì UNRESOLVED, không tự chọn xe.
- Sarcasm: khen ngược/mỉa mai = present; không chắc = uncertain; không dùng số confidence.
- different_action=true CHỈ khi hai cách hiểu đều hợp lý theo context và dẫn tới trạng thái/hiệu ứng xử lý khác nhau thật sự (VD: hủy khác đổi giờ, gửi khác không gửi). Nếu cùng dẫn tới một trạng thái/hiệu ứng, chọn cách hiểu hợp ngữ cảnh nhất và đặt different_action=false; không flag chỉ vì cách diễn đạt có thể phân tích khác nhau.
- Evidence phải là trích nguyên văn có trong comment/parent/root đã cho; không bịa excerpt.
- down_payment: "200" không kèm đơn vị thì unit=null, KHÔNG tự gán triệu.
- price_kind: "lăn bánh"=otr, "niêm yết"=list, "giá" chung chung thì để price_kind trống và UNRESOLVED? Không: vẫn START_REQUEST price, không đặt slot price_kind.
- sentiment độc lập với intent; spam là scope, không phải topic.
- LUÔN trả ít nhất 1 operation, kể cả out_of_scope/spam/unknown: dùng UNRESOLVED với reason và evidence_excerpt trích nguyên văn.
- evidence_excerpt phải là chuỗi con CHÍNH XÁC của comment/parent/root: copy nguyên văn, không thêm/bớt/sửa từ, không paraphrase.
- Bảo hành/bảo dưỡng = service, không thêm spec trừ khi hỏi thông số kỹ thuật xe (động cơ, chỗ ngồi, công suất).
- Nói chuyện phiếm không liên quan xe, không phải chào/cảm ơn/khen/tag/emoji -> UNRESOLVED, không SOCIAL_ACK.
- Đổi variant/màu trong câu hỏi tồn kho/giá thì topic là stock/price, không thêm buy trừ khi khách nói muốn mua/đặt cọc.
- SET_CONTACT_PREFERENCE: tối đa 1 op mỗi comment, chỉ khi khách nói KHÔNG/ĐỪNG làm gì đó; scope là kênh BỊ CHẶN. "Gửi giá tại đây", "nhắn ở đây", "inbox được" là CHO PHÉP, không phải chặn, không sinh pref op cho chúng.
- down_payment: unit lấy đúng đơn vị trong câu: "200 triệu" -> {"amount": 200, "unit": "triệu"}; chỉ để unit=null khi câu thật sự không có đơn vị.
- So sánh A với B: slot vehicle=A, compare_vehicle=B, tách riêng từng xe; không gộp cả cụm "A với B" vào một slot.
- "Cho số tư vấn X" kèm câu hỏi về X: trả lời bằng fact X nếu có (service/spec...), chỉ REQUEST_HUMAN khi không có fact hoặc khách nói rõ muốn gặp người.
- Service: khi nhu cầu dịch vụ có chủ đề con xác định được (bảo hành, bảo dưỡng, sửa chữa) thì LUÔN đặt slot service_topic tương ứng (warranty/maintenance/general) trên request service, kể cả khi cùng comment còn xin số tư vấn hoặc yêu cầu người.
- REQUEST_HUMAN: human_source=customer_explicit CHỈ khi khách nói rõ muốn gặp người ("gặp nhân viên", "nhân viên liên hệ lại", "chuyển cho người xử lý", "muốn nói chuyện với người thật"). "Cho số tư vấn X" KHÔNG phải yêu cầu gặp người: đó là xin thông tin liên hệ -> human_source=model_proposed (model đề xuất vì chưa có fact số), kèm slot service_topic nếu có câu hỏi về X.
- "Lấy bản X" trong câu hỏi tồn kho/giá (VD "còn màu trắng không?", "bản tiêu chuẩn giá bao nhiêu?") = CHỌN VARIANT cho chính request tồn kho/giá đó, KHÔNG tạo request buy. Buy chỉ khi khách nói rõ mua/đặt cọc/chốt mua/trả tiền. Phủ định variant ("không lấy bản cao nữa, lấy bản tiêu chuẩn") = UPDATE_SLOT variant trên request đang hỏi, không phải request mua mới.
- "Còn màu X không?" / "còn màu trắng không?" = câu hỏi TỒN KHO: tạo request stock mới (mang vehicle/variant từ context nếu câu mới không nêu), KHÔNG UPDATE_SLOT color lên request price đang mở. Bất kỳ câu nào nhắc "màu X" đều phải có color slot trên request stock.
- "Hủy X" / "hủy lái thử" / "không cần X nữa" = CANCEL_REQUEST (dứt hẳn nhu cầu đó). REJECT_PENDING CHỈ dùng khi trả lời "không" cho một câu hỏi yes/no đang chờ (VD bot hỏi "Bạn xác nhận đề xuất này nhé?" và khách nói "không"). Sau CANCEL thì không đề xuất lại, không START lại nhu cầu đó.
- "Vẫn lấy/giữ X nhé" = giữ nguyên request đã có, KHÔNG tạo START_REQUEST mới.
- Cụm chỉ kênh liên hệ ("đừng gọi nữa", "gửi tại đây thôi", "nhắn tin thôi", "đừng nhắn tin") = CHỈ SET_CONTACT_PREFERENCE, không START request mới, không hỏi lại nhu cầu.
- Follow-up stock/testdrive không nêu xe/phiên bản: mang vehicle/variant từ request gần nhất của đúng người trong context sang request mới (explicit=false), không hỏi lại xe khi context đã rõ.
- request_ref LUÔN là "topic:<topic>" (VD topic:price, topic:testdrive). KHÔNG BAO GIỜ điền pending id (q_...) vào request_ref; pending id chỉ nằm ở trường pending_id của ANSWER/CONFIRM/REJECT_PENDING.
"""

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): raise RuntimeError("redirect refused")

def call_luna(post: str, comment: str, parent: str = "", context: str = "",
              model: str = MODEL, timeout: int = 60, max_tokens: int = 1200,
              repair_note: str = "", budget_guard=None):
    """Trả dict {ok, semantic, raw_text, usage, cost_usd_est, cost_unknown,
    model_effective, error, attempts}. attempts = số HTTP attempt THỰC đã
    thực hiện (kể cả retry). budget_guard: callable -> bool, được gọi TRƯỚC
    MỖI HTTP attempt (kể cả retry) — guard từ chối thì dừng ngay, không
    retry ẩn dưới guard (A8). cost_unknown=True khi response thiếu usage
    hoặc thiếu token counts: cost_usd_est=None, KHÔNG được tính là 0 như đã
    biết chắc (trước đây usage={} -> cost 0.0)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return dict(ok=False, error="Set OPENROUTER_API_KEY in the process environment", attempts=0)
    user = (f"Post: {post}\nParent/root: {parent}\nContext (requests/pending gần nhất của đúng người): {context}\n"
            f"Comment cần phân tích: {comment}")
    if repair_note: user += f"\nLưu ý sửa lại (chỉ sửa phần lỗi, giữ nguyên phần đúng): {repair_note}"
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": user}],
               "temperature": 0.0, "max_tokens": max_tokens,
               "response_format": {"type": "json_object"}}
    last_err = None
    attempts = 0
    unknown_failed_attempt = False
    last_failure_unbilled = False
    for attempt in range(3):  # 1 lần chính + 2 retry chỉ lỗi retryable
        # A8: guard per-attempt — kể cả retry cũng phải qua guard trước khi
        # HTTP attempt đó xảy ra. Adapter không tuân thủ guard là adapter
        # không đạt contract (runner không thể chặn retry nó không thấy).
        if budget_guard is not None and not budget_guard():
            known_cost = last_failure_unbilled and not unknown_failed_attempt
            return dict(ok=False, error="budget guard denied before HTTP attempt",
                        attempts=attempts, cost_usd_est=0.0 if known_cost else None,
                        cost_unknown=not known_cost)
        attempts = attempt + 1
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                         data=json.dumps(payload).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as res:
                out = json.load(res)
            last_failure_unbilled = False
            content = out["choices"][0]["message"]["content"]
            usage = out.get("usage", {})
            try:
                semantic = parse_json_strict(content)
                if not isinstance(semantic, dict):
                    raise ValueError("model response must be a JSON object")
            except (TypeError, ValueError) as e:
                last_err = f"invalid model JSON: {e}"[:500]
                break
            # A8: chỉ tính cost khi cả hai token counts là số nguyên không âm.
            cost, cost_unknown = None, True
            if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                               for k in ("prompt_tokens", "completion_tokens")):
                try:
                    cost = (usage["prompt_tokens"] * PRICE["prompt"]
                            + usage["completion_tokens"] * PRICE["completion"])
                    cost_unknown = False
                except OverflowError:
                    pass
            return dict(ok=True, semantic=semantic, raw_text=content, usage=usage,
                        cost_usd_est=cost, cost_unknown=cost_unknown,
                        model_effective=out.get("model", model), attempts=attempts,
                        retry_cost_usd_est=0.0 if attempts > 1 and not unknown_failed_attempt else None,
                        retry_cost_unknown=attempts > 1 and unknown_failed_attempt)
        except urllib.error.HTTPError as e:
            last_failure_unbilled = e.code in (400, 401, 403, 404, 413, 422)
            unknown_failed_attempt |= not last_failure_unbilled
            last_err = f"HTTP {e.code}: {e.read().decode(errors='replace').replace(api_key, '[REDACTED]')[:500]}"
            if e.code not in (429, 529, 500, 502, 503, 504): break
        except Exception as e:  # timeout / disconnect: retryable
            last_failure_unbilled = False
            unknown_failed_attempt = True
            last_err = str(e).replace(api_key, "[REDACTED]")[:500]
        time.sleep((2 ** attempt) * 0.5 + random.random() * 0.5)
    known_cost = last_failure_unbilled and not unknown_failed_attempt
    return dict(ok=False, error=last_err, attempts=attempts,
                cost_usd_est=0.0 if known_cost else None, cost_unknown=not known_cost)
