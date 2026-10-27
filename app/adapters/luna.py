"""Luna adapter (baseline A): Luna = semantic parser duy nhất.
Đọc OPENROUTER_API_KEY từ môi trường, chỉ gửi qua Bearer header tới
openrouter.ai. Trả về semantic v1 hoặc
ERROR dict — không bao giờ tự bịa extraction khi API lỗi.
Prompt schema v1 đầy đủ nằm trong SYSTEM; xem config/schema_versions/semantic_v1.json.
"""
from __future__ import annotations
import json, math, os, random, time, urllib.error, urllib.request
from pathlib import Path
from ..semantics import normalize_model_semantic, parse_json_strict, semantic_model_json_schema

MODEL = "openai/gpt-6-luna"
PINNED_MODEL = "openai/gpt-6-luna-20260922"
SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "schema_versions" / "semantic_v1.json"
with SCHEMA_PATH.open(encoding="utf-8") as _schema_file:
    SEMANTIC_SCHEMA = json.load(_schema_file)
STRICT_SEMANTIC_SCHEMAS = {version: semantic_model_json_schema(version)
                           for version in ("1", "2")}
STRICT_SEMANTIC_SCHEMA = STRICT_SEMANTIC_SCHEMAS[SCHEMA_VERSION]

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
   "slot": {"kind": "vehicle|variant|color|location|price_kind|down_payment|datetime|dealer|service_topic|compare_vehicle", "value": "giá trị thô hoặc số, down_payment là {amount, unit|null}", "evidence_excerpt": "trích riêng phần chứng minh slot nếu explicit", "explicit": true, "source_message_id": "...", "source_actor_id": "...", "source_state_token": "..."},
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
- Slot explicit phải được chứng minh bởi chính evidence_excerpt (value hoặc alias đã biết phải xuất hiện trong excerpt); không dùng một từ khác trong câu để hợp thức hóa slot.
- evidence_excerpt ở operation chứng minh intent; slot.evidence_excerpt chứng minh riêng slot. Không dùng excerpt intent để chứng minh slot nếu chúng khác nhau.
- Slot kế thừa phải có explicit=false, source_message_id, source_actor_id và source_state_token đúng từ context; chỉ kế thừa state của cùng actor trong cùng post/thread. Không lấy thuộc tính của actor khác.
- Không coi câu trả lời cũ của model là fact. Post là bối cảnh chung; nếu suy xe từ post đơn xe, đánh dấu explicit=false và trỏ source_message_id tới post.
- Persona/tone chỉ áp dụng sau khi policy quyết định; không đổi sentiment, intent, polarity hoặc slot extraction.
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
- Strict output yêu cầu mọi field của schema: field tùy chọn không áp dụng phải trả null; trong slot.extra, kind không dùng phải là null. Không tự tạo fact để điền field.
"""

SYSTEM_V2 = (SYSTEM.replace("schema v1", "schema v2", 1)
            .replace('"schema_version": "1"', '"schema_version": "2"', 1)
            .replace("Slot kế thừa phải có explicit=false, source_message_id, source_actor_id và source_state_token đúng từ context; chỉ kế thừa state của cùng actor trong cùng post/thread. Không lấy thuộc tính của actor khác.",
                     "Slot kế thừa chỉ ghi explicit=false và evidence; server gắn source_message_id, actor và state token từ snapshot đã kiểm chứng.")
            + """
V2 fields: intent_labels chứa các nhãn độc lập, duy nhất và đúng với topics trong requests.
category dùng taxonomy: sales (price/promo/loan/stock/spec/dealer/testdrive/buy/registration),
service, support (complaint), social; nếu nhãn thuộc nhiều nhóm hoặc không rõ thì category=null.
tags chỉ dùng: multi_intent, negated, corrected, confirmation, rejection, cancellation,
handoff, sarcasm, pending_answer, uncertain. Khi không chắc tag, bỏ qua. Không điền nguồn
hoặc state token của slot kế thừa; server chịu trách nhiệm gắn provenance.
""")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): raise RuntimeError("redirect refused")

def call_luna(post: str, comment: str, parent: str = "", context: str = "",
              model: str = MODEL, timeout: int = 60, max_tokens: int = 1200,
              repair_note: str = "", budget_guard=None, schema_version=SCHEMA_VERSION):
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
    schema_version = str(schema_version)
    if schema_version not in STRICT_SEMANTIC_SCHEMAS:
        return dict(ok=False, error="unsupported semantic schema version", attempts=0)
    try:
        context_obj = parse_json_strict(context) if context else None
    except (TypeError, ValueError):
        context_obj = None
    if isinstance(context_obj, dict) and context_obj.get("format") == "mat_intent_context_v1":
        post_obj = dict(context_obj.get("postcard") or {})
        post_obj.setdefault("text", post)
        messages = [m for m in context_obj.get("messages", [])
                    if isinstance(m, dict) and m.get("role") not in ("post", "current")]
        current = next((m for m in context_obj.get("messages", [])
                        if isinstance(m, dict) and m.get("role") == "current"), {})
        user = json.dumps({"format": "mat_intent_input_v1", "post": post_obj,
            "root_and_parent_messages": messages,
            "recent_actor_messages": context_obj.get("recent", []),
            "active_requests": context_obj.get("active_requests", []),
            "pending": context_obj.get("pending", []),
            "contact_blocks": context_obj.get("contact_blocks", []),
            "prior_customer_turns": context_obj.get("prior_customer_turns", []),
            "recent_bot_replies": context_obj.get("recent_bot_replies", []),
            "current_comment": {"message_id": current.get("message_id"),
                                "actor_id": current.get("actor_id"),
                                "actor_role": current.get("role", "current"),
                                "text": comment}}, ensure_ascii=False, separators=(",", ":"))
    else:
        user = (f"Post: {post}\nParent/root: {parent}\nContext (requests/pending gần nhất của đúng người): {context}\n"
                f"Comment cần phân tích: {comment}")
    if repair_note: user += f"\nLưu ý sửa lại (chỉ sửa phần lỗi, giữ nguyên phần đúng): {repair_note}"
    prompt_version = f"luna_system_v{schema_version}"
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM_V2 if schema_version == "2" else SYSTEM},
                                            {"role": "user", "content": user}],
               "temperature": 0.0, "max_tokens": max_tokens,
               "response_format": {"type": "json_schema", "json_schema": {
                   "name": f"mat_intent_semantic_v{schema_version}", "strict": True,
               "schema": STRICT_SEMANTIC_SCHEMAS[schema_version]}},
               "provider": {"require_parameters": True}}
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
                semantic = normalize_model_semantic(semantic)
            except (TypeError, ValueError) as e:
                last_err = f"invalid model JSON: {e}"[:500]
                break
            # A8: chỉ tính cost khi cả hai token counts là số nguyên không âm.
            model_effective = out.get("model", model)
            cost, cost_unknown = None, True
            reported_cost = (usage.get("cost") if isinstance(usage, dict) else None)
            if reported_cost is None: reported_cost = out.get("cost")
            if type(reported_cost) in (int, float) and math.isfinite(reported_cost) and reported_cost >= 0:
                cost, cost_unknown = float(reported_cost), False
            return dict(ok=True, semantic=semantic, raw_text=content, usage=usage,
                        cost_usd_est=cost, cost_unknown=cost_unknown,
                        model_effective=model_effective, attempts=attempts,
                        schema_version=schema_version, prompt_version=prompt_version,
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
