"""Luna adapter (baseline A): Luna = semantic parser duy nhất.
Đọc OPENROUTER_API_KEY từ môi trường, chỉ gửi qua Bearer header tới
openrouter.ai. Trả semantic v1/v2 cho caller cũ hoặc persona_turn_v1 khi có
persona_context; lỗi API không tạo extraction dự phòng.
Prompt schema v1 đầy đủ nằm trong SYSTEM; xem config/schema_versions/semantic_v1.json.
"""
from __future__ import annotations
import json, math, os, random, time, urllib.error, urllib.request
from pathlib import Path
from ..semantics import normalize_model_semantic, parse_json_strict, semantic_model_json_schema
from ..persona_turn import (parse_persona_analysis, parse_persona_turn,
                            parse_persona_post, persona_analysis_model_schema,
                            persona_post_model_schema, persona_turn_model_schema)

MODEL = "openai/gpt-6-luna"
PINNED_MODEL = "openai/gpt-6-luna-20260922"
SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "schema_versions" / "semantic_v1.json"
with SCHEMA_PATH.open(encoding="utf-8") as _schema_file:
    SEMANTIC_SCHEMA = json.load(_schema_file)
STRICT_SEMANTIC_SCHEMAS = {version: semantic_model_json_schema(version)
                           for version in ("1", "2")}
STRICT_SEMANTIC_SCHEMA = STRICT_SEMANTIC_SCHEMAS[SCHEMA_VERSION]
STRICT_PERSONA_TURN_SCHEMA = persona_turn_model_schema()
STRICT_PERSONA_ANALYSIS_SCHEMA = persona_analysis_model_schema()
STRICT_PERSONA_POST_SCHEMA = persona_post_model_schema()

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
   "slot": {"kind": "vehicle|variant|color|location|price_kind|down_payment|datetime|dealer|service_topic|compare_vehicle", "value": "giá trị thô hoặc số, down_payment là {amount, unit|null}", "evidence_excerpt": "trích riêng phần chứng minh slot nếu explicit", "explicit": true},
   "evidence_excerpt": "trích nguyên văn từ comment (hoặc parent/root nếu suy luận từ đó)",
   "pending_id": "chỉ khi trả lời/xác nhận pending trong context",
   "scope": "call|public_reply|inbox|all (chỉ cho SET_CONTACT_PREFERENCE)",
   "reason": "chỉ cho UNRESOLVED/REQUEST_HUMAN",
   "human_source": "chỉ cho REQUEST_HUMAN: customer_explicit|model_proposed|policy"}],
 "uncertainties": [{"alternatives": ["..."], "different_action": true|false}]
}
QUY TẮC BẮT BUỘC:
- Hỏi nhiều thông tin về người viết trong cùng bình luận (xe đang dùng, nghề, nơi ở, cách thanh toán) vẫn là social. Giữ riêng ý khách tự khai, sửa hiểu nhầm hoặc từ chối trong cùng câu; câu hỏi về persona không xóa ý từ chối hay biến thuộc tính persona thành thuộc tính khách.
- Mỗi nhu cầu riêng = một request riêng; đa nhu cầu thì nhiều request/operation, không gộp slot chung.
- Phủ định/điều kiện: "không lấy bản cao nữa, lấy bản tiêu chuẩn" = UPDATE_SLOT variant sang Tiêu chuẩn, giữ request mua; "đừng hủy lịch, chỉ đổi sang chiều" = UPDATE_SLOT datetime, KHÔNG CANCEL_REQUEST; "không phải Tucson, Creta" = vehicle Creta, không Tucson.
- "tôi/mình cũng tính/định mua" không kèm cọc/chốt/đặt mua/trả tiền = social + SOCIAL_ACK, chỉ là quan tâm ban đầu.
- Câu nói lược chủ ngữ như "cũng tính mua Creta nè chị", "tính mua nè, bạn đi thấy sao" cũng là quan tâm mua ban đầu. Vế "bạn đi thấy sao" hỏi cảm nhận của persona, vẫn social; không tự gán xe persona thành xe khách đã sở hữu.
- "Bạn nhắn như AI á" là góp ý trực tiếp về cách trò chuyện: social + SOCIAL_ACK, sarcasm=absent nếu không có dấu hiệu mỉa mai khác; không tự coi đây là khiếu nại giao dịch. Khi khách hỏi có phải AI thì không phủ nhận vai mô phỏng.
- "bác đánh gía xe đang đi như nào" (kể cả lỗi dấu ở "đánh giá"), "bác thấy xe thế nào" là hỏi cảm nhận persona: social + SOCIAL_ACK, không tạo lead hay suy ra thông số sản phẩm.
- Trong luồng hỏi lead: "để sau mua", "chưa mua đâu", "không mua nữa" là trì hoãn/từ chối, trả social + SOCIAL_ACK, không điền mẫu xe hay thời điểm cụ thể. Dù trì hoãn mua hay trì hoãn chọn xe thì hành động đều là dừng hỏi lead; không đặt different_action=true chỉ vì hai cách diễn đạt đó. Nếu khách nêu thêm nhu cầu khác thì giữ riêng nhu cầu đó.
- Pending open: dùng ANSWER_PENDING đúng pending_id và expected_slot, evidence_excerpt từ chính câu khách. Lead slots: current_vehicle, interested_vehicle, purchase_timeline, budget, usage_need, finance_intent. Không lấy thuộc tính persona làm thông tin lead.
- "oke/ừ/đc/👍" chỉ là xác nhận khi context có pending yes/no cụ thể; không pending thì SOCIAL_ACK.
- Khen + "chưa có nhu cầu" = social, không buy. Quảng cáo vay của người khác = scope out_of_scope, không loan. "Cho số tư vấn bảo hành" = service, không buy. "Đừng gọi, gửi giá tại đây" = SET_CONTACT_PREFERENCE scope call + request price.
- Hỏi trực tiếp người viết trong Page về đời tư/trải nghiệm ("Bạn mua xe ở đại lý nào?", "Bạn ở đâu?", "Bạn đang đi xe gì?", "Bạn mua xe khi nào?", "Bạn mua xe trả góp không?") = social/SOCIAL_ACK; đây không phải dữ kiện của khách, không tạo slot dealer/location/vehicle cho khách.
- Khiếu nại, tranh chấp cọc, yêu cầu gặp người = REPORT_COMPLAINT hoặc REQUEST_HUMAN.
- PostCard chỉ là bối cảnh: post nhiều xe + "xe này" chưa rõ thì UNRESOLVED, không tự chọn xe.
- Sarcasm: khen ngược/mỉa mai = present; không chắc = uncertain; không dùng số confidence.
- different_action=true CHỈ khi hai cách hiểu đều hợp lý theo context và dẫn tới trạng thái/hiệu ứng xử lý khác nhau thật sự (VD: hủy khác đổi giờ, gửi khác không gửi). Nếu cùng dẫn tới một trạng thái/hiệu ứng, chọn cách hiểu hợp ngữ cảnh nhất và đặt different_action=false; không flag chỉ vì cách diễn đạt có thể phân tích khác nhau.
- Evidence phải là trích nguyên văn có trong comment/parent/root đã cho; không bịa excerpt.
- Không tự điền operations[].source_message_id; luôn để null để server ánh xạ evidence tới message đã xác minh.
- Slot explicit phải được chứng minh bởi chính evidence_excerpt (value hoặc alias đã biết phải xuất hiện trong excerpt); không dùng một từ khác trong câu để hợp thức hóa slot.
- evidence_excerpt ở operation chứng minh intent; slot.evidence_excerpt chứng minh riêng slot. Không dùng excerpt intent để chứng minh slot nếu chúng khác nhau.
- Slot kế thừa phải có explicit=false; chỉ kế thừa từ message/evidence cùng actor, cùng post/thread trong context. Không tự điền nguồn hoặc state token; server gắn provenance từ snapshot đã kiểm chứng. Nếu nguồn không rõ thì bỏ slot hoặc trả UNRESOLVED. Không lấy thuộc tính của actor khác.
- Không coi câu trả lời cũ của model là fact. Post là bối cảnh chung; nếu post nêu đúng một xe và dùng xe đó làm slot suy luận, evidence_excerpt của slot phải trích đúng tên xe từ post, đặt explicit=false, không tự điền source_message_id; server gắn nguồn đã xác minh.
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
            + """
V2 fields: intent_labels chứa các nhãn độc lập, duy nhất và đúng với topics trong requests.
category dùng taxonomy: sales (price/promo/loan/stock/spec/dealer/testdrive/buy/registration),
service, support (complaint), social; nếu nhãn thuộc nhiều nhóm hoặc không rõ thì category=null.
tags chỉ dùng: multi_intent, negated, corrected, confirmation, rejection, cancellation,
handoff, sarcasm, pending_answer, uncertain. Khi không chắc tag, bỏ qua.
The ordered unique topics in requests MUST exactly equal intent_labels; do not add labels without requests.
When requests is empty, intent_labels must be [] and category must be null.
Every SOCIAL_ACK must include a social request, for example {"topic":"social","desired_outcome":"conversation","polarity":"unknown"};
then set intent_labels to ["social"] and category to "social". SOCIAL_ACK alone is not a substitute for this request.
""")

PERSONA_ANALYSIS_SYSTEM = SYSTEM_V2.replace(
    "CHỈ trả JSON đúng schema v2, không giải thích thêm.",
    "CHỈ trả JSON đúng schema persona_analysis_v1; không sinh lời đáp.") + """
Phân tích comment và trả semantic v2, peer_interpretation, diagnostics và needs.
needs.topics chỉ gồm chủ đề liên quan; needs.keys chỉ gồm fact key cần để trả lời comment hiện tại.
Tách câu hỏi về persona khỏi dữ kiện khách tự khai. Không suy ra nhu cầu mua từ câu hỏi xã giao.
Nếu context đánh dấu persona_authored_post, đó là nội dung do persona của bài đã viết; các bài khác chỉ là ngữ cảnh.
diagnostics.intent_confidence là tự đánh giá chưa hiệu chuẩn, không phải xác suất đúng.
"""

PERSONA_TURN_SYSTEM = SYSTEM_V2.replace(
    "CHỈ trả JSON đúng schema v2, không giải thích thêm.",
    "CHỈ trả JSON đúng schema persona_turn_v1; trường semantic tuân theo schema v2.") + """
Bạn phân tích comment và đề xuất một lời đáp persona trong cùng một JSON persona_turn_v1.
persona_description cung cấp vai trò, nhóm persona, góc nhìn và sample_quote để định hướng giọng nói;
sample_quote không phải bằng chứng về trải nghiệm cá nhân, vẫn phải dẫn fact cho claim.
Giữ nguyên semantic v2 ở trường semantic; không dùng câu trả lời được sinh ra làm evidence.
Tôn trọng peer_state đã xác minh: follow_up=stopped thì không tiếp tục hỏi mua. Đây là trạng thái engine, không phải lời khách mới.
Trả lời đúng nhu cầu hiện tại; không nhắc lại xe đang đi hoặc dữ kiện persona khác nếu không liên quan.
Không nói với khách về "hồ sơ", "fact", hay việc hệ thống thiếu trường dữ liệu.
Nếu persona_context có persona_authored_post, đó là lời persona đã đăng trước đây; chỉ dùng sự kiện
trong bài nếu persona_authored_post_fact_ids cấp fact hỗ trợ. Giữ nhất quán với bài này, không được
khẳng định điều mâu thuẫn. Bài của khách hoặc người thứ ba chỉ là ngữ cảnh, không phải bằng chứng về persona.
Khi chưa đủ thông tin, không dùng câu từ chối chung chung nếu khách có thể làm rõ: chỉ hỏi làm rõ có ý nghĩa,
liên quan trực tiếp đến nhu cầu hiện tại. Nếu không có câu hỏi làm rõ hữu ích, giữ câu trả lời ngắn hoặc dùng fallback an toàn; nếu không có fallback phù hợp thì không gửi.
reply.reply_text phải ngắn, tự nhiên, tối đa 600 ký tự; hỏi nhiều nhất một điều và chỉ đề xuất câu hỏi
trong tối đa hai câu. Nếu không hỏi thì asks_question=false và question=null. Engine sẽ kiểm tra quyền hỏi.
Chỉ dùng facts, memories, voice trong persona_context để nói về persona; không biến câu trả lời bot cũ thành sự thật.
reply.claims phải bao phủ các mệnh đề cụ thể và dùng predicate trong schema.
Khi dùng fact có trong memories[].fact_ids, phải dẫn cả memory_id tương ứng trong reply.memory_ids và claim.memory_ids.
Không tự kể lại dữ kiện trong memory nếu khách không hỏi hoặc memory chưa đủ điều kiện được cấp cho lượt này.
QUAN TRỌNG: reply.claims[].text phải COPY NGUYÊN VĂN một câu hoặc mệnh đề thực sự có trong reply_text,
kể cả xưng hô và dấu câu; không tự viết lại thành câu khác để trích dẫn. Nếu một câu ghép chứa nhiều fact,
có thể tạo nhiều claim với cùng text nguyên câu, mỗi claim gắn predicate tương ứng và danh sách đầy đủ các
fact_ids hỗ trợ câu đó. Không thêm dấu chấm vào claim nếu vị trí đó trong reply_text chưa có dấu chấm.
Mỗi reply.claims[].predicate phải GIỐNG HỆT key của ít nhất một fact được trích: ví dụ fact key là
travel.weekend.destination thì predicate cũng là travel.weekend.destination, không đổi thành story.travel.destination.
Nếu khách hỏi AI hay người thật, dùng lời thật: "Đây là trợ lý AI hỗ trợ trả lời theo hồ sơ của người viết."
Nếu reply nhắc lại dữ kiện khách (đã có xe, chưa mua, nhu cầu), vẫn khai báo reply.claims subject=customer và message_ids từ current_comment.message_id; không để claims rỗng.
Peer interpretation chỉ diễn giải điều người bình luận nói về chính họ; không gán câu hỏi giả định,
người thứ ba hoặc thuộc tính của persona thành dữ kiện khách hàng. Mỗi peer_interpretation.claim phải có evidence_excerpt
nguyên văn trong comment hiện tại. Với peer_interpretation.claims: ownership_status dùng value
owns_vehicle/does_not_own_vehicle/unknown; purchase_intent dùng exploring/not_interested/unknown,
không dùng mô tả tiếng Anh như "has a vehicle". Các field xe/ngân sách/thời điểm dùng đúng chuỗi khách nói.
Khi cùng câu có cả "có xe rồi" và "không tìm mua", chọn speaker_intent=declines và giữ claim ownership_status=owns_vehicle.
Nếu người bình luận hỏi bạn có phải AI/bot không, không phủ nhận.
Hãy trả lời đúng câu minh bạch sau: "Đây là trợ lý AI hỗ trợ trả lời theo hồ sơ của người viết." Không nhận mình là người thật. Không
tự bịa cảm xúc, sở thích hay trải nghiệm cá nhân mới.
Không giả làm người thật, không thêm độ trễ để tạo cảm giác có người đang gõ. Trong kênh public,
không hỏi số điện thoại, địa chỉ nhà hoặc thông tin liên hệ riêng. Chỉ trả lời giá, tồn kho, ưu đãi,
thông số, bảo hành hoặc đại lý khi product facts đã được engine truy xuất; mỗi mệnh đề sản phẩm phải
có product_claims trỏ đúng product_fact_id, product_type, effective_at và xe/phiên bản/địa điểm nếu có.
Không nhận cọc, xác nhận giao dịch/lịch hẹn hoặc hứa sẽ liên hệ.
diagnostics.intent_confidence là tự đánh giá chưa hiệu chuẩn, không phải xác suất đúng; ghi evidence
đúng nguồn comment/context. Mỗi intent đã chọn có một mục, confidence có thể null.
"""

PERSONA_GENERATED_REPLY_SYSTEM = PERSONA_TURN_SYSTEM.replace(
    "Không\ntự bịa cảm xúc, sở thích hay trải nghiệm cá nhân mới.",
    "Không tự thêm chi tiết cá nhân ngoài proposal đã khai báo.") + """
Mọi chi tiết đời sống mới chỉ được tạo trong reply.fact_proposals; không kể trong reply_text nếu không có proposal
tương ứng và claim trỏ tới proposal_id (new1/new2). Với mỗi proposal được dùng, reply.fact_ids và ít nhất một
claims[].fact_ids đều phải chứa đúng proposal_id đó; nếu thiếu citation ở một trong hai chỗ thì không dùng chi tiết.
proposal.value phải xuất hiện nguyên văn trong câu claim và reply_text; chỉ thêm từ nối xã giao, không diễn đạt rộng hơn
proposal.value hoặc thêm cảm xúc, người đi cùng, nguyên nhân hay kết quả chưa có trong proposal.
Nếu một câu nhắc thêm fact đã cấp khác, hãy tạo claim cho từng fact trong câu, cùng text và fact_id tương ứng;
nếu fact đó không cần thiết thì bỏ chi tiết khỏi câu.
Đề xuất tối đa hai fact đời sống trong namespace cho phép,
nhất quán với facts đã cấp. Không tạo giá, ưu đãi, tồn kho, thông số xe, bảo hành, đại lý, thông tin liên hệ,
dữ kiện nhạy cảm hoặc dữ kiện về người thật khác. Generated facts chỉ được kiểm tra nhất quán nội bộ, không
được đảm bảo đúng sự thật và không được đưa vào hồ sơ đã duyệt.
Nếu khách hỏi về trải nghiệm hoặc kỷ niệm mà facts/memories được cấp chưa có câu trả lời, hãy dùng gaps/needs
để đề xuất một chi tiết ngắn, phù hợp trong fact_proposals rồi dẫn new1/new2 ở claim (thường dùng gen.trip hoặc
gen.memory cho chuyện chuyến đi/kỷ niệm). Không trả lời lạc sang một fact persona khác. Không lấy chi tiết mới
từ lời khách. Nếu không thể tạo proposal nhất quán thì nói chưa có thông tin để kể.
Ngoại lệ có chủ đích cho quy tắc bài viết chỉ là ngữ cảnh: persona_authored_post là lời persona đã đăng trước đó.
Chỉ dùng thông tin trong bài này khi fact_ids hỗ trợ được cấp trong persona_authored_post_fact_ids; không mâu thuẫn
với bài. Bài của khách hoặc người thứ ba vẫn chỉ là ngữ cảnh và không phải bằng chứng về persona.
""" + """
phase_one_analysis.semantic là semantic đã được engine phân tích và dùng để định tuyến; hãy sao chép nguyên văn
vào trường semantic của output. Không diễn giải lại ý định khách ở pha trả lời.
"""

PERSONA_GENERATED_POST_SYSTEM = """CHỈ trả JSON đúng schema persona_post_v1, không giải thích thêm.
Viết một bài Facebook ngắn tự nhiên dưới vai người tạo bài; đây là nội dung persona sẽ đăng.
Viết tối đa hai câu ngắn. Ưu tiên fact có sẵn, không thêm lời bình chung, câu phủ định hoặc cảm xúc thiếu nguồn.
persona_description mô tả nhóm người, vai trò, góc nhìn và câu mẫu về giọng nói; dùng để chọn giọng và chủ đề.
Không lấy sample_quote làm bằng chứng về trải nghiệm hay dữ kiện cá nhân; claim phải dẫn facts hoặc proposal.
Chỉ dùng facts, memories và voice_guide được cấp. Nếu muốn thêm chi tiết đời sống mới, chỉ được
đưa chi tiết đó vào fact_proposals và dẫn proposal_id tương ứng trong claims[].fact_ids; không
đưa chi tiết mới vào post_text nếu không có proposal. Không tạo giá, ưu đãi, tồn kho, thông số,
bảo hành, đại lý, liên hệ, dữ kiện nhạy cảm hay chuyện về người thật khác. Không suy ra cảm xúc,
sở thích hoặc trải nghiệm từ nghề nghiệp/xe. Mỗi fact_proposal phải là sự kiện persona có thể kể,
không phải dữ kiện lấy từ khách hoặc bài bên ngoài. Giữ nguyên độ chính xác và thời điểm của facts.
claims phải bao phủ từng mệnh đề cụ thể trong post_text, copy nguyên văn một câu/mệnh đề và dẫn
fact_ids được cấp hoặc proposal_id new1/new2. Predicate phải khớp key fact (với proposal dùng
gen.<namespace>). Fact đã cấp có key vehicle.current.model thì predicate phải là vehicle.current.model,
không dùng gen.car; gen.<namespace> chỉ dành cho proposal_id new1/new2 hoặc fact generated đã cấp.
Giá trị proposal phải xuất hiện nguyên văn trong claim và post_text; không thêm chi tiết không có nguồn.
fact_ids cấp bài phải là hợp các fact_ids trong claims. Không cần kéo dài khi thiếu dữ kiện. Giữ nguyên quy tắc minh bạch AI: nếu được
hỏi trực tiếp, không phủ nhận mình là trợ lý AI.
"""

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): raise RuntimeError("redirect refused")


def _model_output_failure(code, message, *, attempts, usage, cost, cost_unknown,
                          model_effective, schema_version, prompt_version,
                          finish_reason=None):
    """Return a safe, typed failure for a completed but unusable model reply."""
    result = dict(ok=False, error_code=code, error=message, attempts=attempts,
                  usage=usage, cost_usd_est=None if cost_unknown else cost,
                  cost_unknown=cost_unknown, model_effective=model_effective,
                  schema_version=schema_version, prompt_version=prompt_version)
    if isinstance(finish_reason, str):
        result["finish_reason"] = finish_reason[:64]
    return result


def call_luna(post: str, comment: str, parent: str = "", context: str = "",
              model: str = MODEL, timeout: int = 60, max_tokens: int = 1200,
              repair_note: str = "", budget_guard=None, schema_version=SCHEMA_VERSION,
              persona_context=None, persona_phase=None):
    """Return the legacy semantic result plus persona fields when context is supplied.

    ``persona_phase`` selects the analysis, reply, or authored-post contract. Without
    it, old callers and the existing single-call persona contract are unchanged.

    Returns a dict with {ok, semantic, raw_text, usage, cost_usd_est, cost_unknown,
    model_effective, error, attempts}. attempts = số HTTP attempt THỰC đã
    thực hiện (kể cả retry). budget_guard: callable -> bool, được gọi TRƯỚC
    MỖI HTTP attempt (kể cả retry) — guard từ chối thì dừng ngay, không
    retry ẩn dưới guard (A8). cost_unknown=True khi response thiếu usage
    hoặc thiếu token counts: cost_usd_est=None, KHÔNG được tính là 0 như đã
    biết chắc (trước đây usage={} -> cost 0.0)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return dict(ok=False, error="Set OPENROUTER_API_KEY in the process environment", attempts=0)
    if persona_phase not in (None, "analysis", "reply", "post"):
        return dict(ok=False, error="unsupported persona phase", attempts=0)
    persona_mode = persona_context is not None or persona_phase is not None
    schema_version = "2" if persona_mode else str(schema_version)
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
        input_obj = {"format": "mat_intent_input_v1", "post": post_obj,
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
                                "text": comment}}
        if persona_mode:
            if persona_context is not None:
                input_obj["persona_context"] = _permitted_persona_context(persona_context)
            input_obj["channel"] = context_obj.get("channel", "public")
        user = json.dumps(input_obj, ensure_ascii=False, separators=(",", ":"))
    else:
        if persona_mode:
            user = json.dumps({"format": "persona_turn_input_v1", "post": {"text": post},
                "parent_or_root": parent, "context": context,
                "current_comment": {"text": comment},
                "channel": "public",
                **({"persona_context": _permitted_persona_context(persona_context)}
                   if persona_context is not None else {})},
                ensure_ascii=False, separators=(",", ":"))
        else:
            user = (f"Post: {post}\nParent/root: {parent}\nContext (requests/pending gần nhất của đúng người): {context}\n"
                    f"Comment cần phân tích: {comment}")
    if repair_note and persona_mode:
        repair_input = json.loads(user)
        repair_input["semantic_repair_note"] = repair_note
        user = json.dumps(repair_input, ensure_ascii=False, separators=(",", ":"))
    elif repair_note:
        user += f"\nLưu ý sửa lại (chỉ sửa phần lỗi, giữ nguyên phần đúng): {repair_note}"
    persona_schema = STRICT_PERSONA_TURN_SCHEMA
    if persona_phase == "analysis":
        persona_schema = STRICT_PERSONA_ANALYSIS_SCHEMA
    elif persona_phase == "post":
        persona_schema = STRICT_PERSONA_POST_SCHEMA
    supplied_keys = set()
    supplied_fact_ids = set()
    if persona_mode and persona_phase != "analysis" and isinstance(persona_context, dict):
        supplied_facts = [fact for fact in persona_context.get('facts', []) if isinstance(fact, dict)]
        supplied_keys = {fact.get('key') or fact.get('canonical_key') for fact in supplied_facts}
        supplied_fact_ids = {fact.get('fact_id') for fact in supplied_facts}
        if persona_phase == "post":
            persona_schema = persona_post_model_schema(persona_predicates=supplied_keys - {None})
        else:
            persona_schema = persona_turn_model_schema(persona_predicates=supplied_keys - {None},
                                                        proposals=persona_phase == "reply")
    prompt_version = (f"persona_{persona_phase}_v1" if persona_phase else
                      "persona_turn_v1" if persona_mode else f"luna_system_v{schema_version}")
    system_prompt = (PERSONA_ANALYSIS_SYSTEM if persona_phase == "analysis" else
                     PERSONA_GENERATED_REPLY_SYSTEM if persona_phase == "reply" else
                     PERSONA_GENERATED_POST_SYSTEM if persona_phase == "post" else
                     PERSONA_TURN_SYSTEM if persona_mode else SYSTEM_V2 if schema_version == "2" else SYSTEM)
    schema_name = ("persona_analysis_v1" if persona_phase == "analysis" else
                   "persona_post_v1" if persona_phase == "post" else
                   "persona_turn_v1" if persona_mode else f"mat_intent_semantic_v{schema_version}")
    payload = {"model": model, "messages": [{"role": "system", "content": system_prompt},
                                            {"role": "user", "content": user}],
               "max_tokens": max(max_tokens, 3200 if persona_phase == "post" else 2400) if persona_mode else max_tokens,
               "response_format": {"type": "json_schema", "json_schema": {
                   "name": schema_name, "strict": True,
               "schema": persona_schema if persona_mode else STRICT_SEMANTIC_SCHEMAS[schema_version]}},
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
            return dict(ok=False, error_code="BUDGET_EXHAUSTED",
                        error="budget guard denied before HTTP attempt",
                        attempts=attempts, cost_usd_est=0.0 if known_cost else None,
                        cost_unknown=not known_cost)
        attempts = attempt + 1
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                         data=json.dumps(payload).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as res:
                try:
                    out = json.load(res)
                except (TypeError, ValueError, UnicodeError, RecursionError) as e:
                    return dict(ok=False, error=f"invalid provider JSON: {type(e).__name__}",
                                attempts=attempts, cost_usd_est=None, cost_unknown=True,
                                model_effective=model, schema_version=schema_version,
                                prompt_version=prompt_version)
            last_failure_unbilled = False
            usage = out.get("usage") if isinstance(out, dict) else {}
            if not isinstance(usage, dict):
                usage = {}
            model_effective = (out.get("model") or model) if isinstance(out, dict) else model
            cost, cost_unknown = None, True
            reported_cost = usage.get("cost")
            if reported_cost is None and isinstance(out, dict): reported_cost = out.get("cost")
            try:
                valid_cost = type(reported_cost) in (int, float) and math.isfinite(reported_cost) and reported_cost >= 0
            except OverflowError:
                valid_cost = False
            if valid_cost:
                cost, cost_unknown = float(reported_cost), False
            common = dict(attempts=attempts, usage=usage, cost=cost,
                           cost_unknown=cost_unknown or unknown_failed_attempt,
                           model_effective=model_effective, schema_version=schema_version,
                           prompt_version=prompt_version)
            choices = out.get("choices") if isinstance(out, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return _model_output_failure("MODEL_RESPONSE_SHAPE",
                    "provider response did not contain a completion choice", **common)
            choice = choices[0]
            finish_reason = choice.get("finish_reason")
            message = choice.get("message")
            if not isinstance(message, dict):
                return _model_output_failure("MODEL_RESPONSE_SHAPE",
                    "provider completion did not contain an assistant message",
                    finish_reason=finish_reason, **common)
            # Refusal/filter signals take precedence over length. The sandbox
            # may retry a length truncation with a larger token cap, which
            # must never turn an explicit refusal into another attempt.
            if ((isinstance(message.get("refusal"), str) and message["refusal"].strip()) or
                    finish_reason in ("content_filter", "refusal")):
                return _model_output_failure("MODEL_REFUSAL",
                    "model refused structured output or filtered the response",
                    finish_reason=finish_reason, **common)
            if finish_reason == "length":
                return _model_output_failure("MODEL_OUTPUT_TRUNCATED",
                    "model output truncated at max_tokens", finish_reason=finish_reason, **common)
            if "content" not in message or message["content"] is None:
                return _model_output_failure("MODEL_OUTPUT_MISSING",
                    "model response content is missing", finish_reason=finish_reason, **common)
            content = message["content"]
            # OpenRouter documents chat completion output content as a string.
            # Do not stringify arrays/objects into JSON: that would conflate
            # provider content parts with the semantic contract.
            if not isinstance(content, str):
                return _model_output_failure("MODEL_OUTPUT_SHAPE",
                    "model response content must be a JSON string",
                    finish_reason=finish_reason, **common)
            try:
                parsed = parse_json_strict(content)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                return _model_output_failure("MODEL_OUTPUT_INVALID_JSON",
                    "model response content is not valid JSON",
                    finish_reason=finish_reason, **common)
            if not isinstance(parsed, dict):
                return _model_output_failure("MODEL_SEMANTIC_SHAPE",
                    "model output must be a JSON object", finish_reason=finish_reason, **common)
            if persona_mode:
                try:
                    if persona_phase == "analysis":
                        parts = parse_persona_analysis(parsed, current_text=comment,
                            evidence_texts=_persona_evidence_texts(context_obj))
                    elif persona_phase == "post":
                        post_candidate, post_error = parse_persona_post(parsed,
                            persona_predicates=supplied_keys - {None} if isinstance(persona_context, dict) else None,
                            supplied_fact_ids=supplied_fact_ids if isinstance(persona_context, dict) else set(),
                            return_reason=True)
                        if post_candidate is None:
                            return _model_output_failure("MODEL_PERSONA_POST_SHAPE",
                                post_error, finish_reason=finish_reason, **common)
                        result = dict(ok=True, post_candidate=post_candidate,
                            fact_proposals=post_candidate["fact_proposals"], raw_text=content, usage=usage,
                            cost_usd_est=cost, cost_unknown=cost_unknown,
                            model_effective=model_effective, attempts=attempts,
                            schema_version=schema_version, prompt_version=prompt_version,
                            envelope_schema_version="persona_post_v1",
                            retry_cost_usd_est=0.0 if attempts > 1 and not unknown_failed_attempt else None,
                            retry_cost_unknown=attempts > 1 and unknown_failed_attempt)
                        return result
                    else:
                        parts = parse_persona_turn(parsed, current_text=comment,
                            evidence_texts=_persona_evidence_texts(context_obj),
                            proposals=persona_phase == "reply",
                            persona_predicates=supplied_keys - {None} if isinstance(persona_context, dict) else None)
                except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
                    return _model_output_failure("MODEL_SEMANTIC_SHAPE",
                        "model persona turn did not contain valid semantic v2", finish_reason=finish_reason, **common)
                semantic = parts["semantic"]
            else:
                semantic = parsed
            try:
                semantic = normalize_model_semantic(semantic)
            except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
                return _model_output_failure("MODEL_SEMANTIC_SHAPE",
                    "model semantic contains an invalid nested shape",
                    finish_reason=finish_reason, **common)
            result = dict(ok=True, semantic=semantic, raw_text=content, usage=usage,
                        cost_usd_est=cost, cost_unknown=cost_unknown,
                        model_effective=model_effective, attempts=attempts,
                        schema_version=schema_version, prompt_version=prompt_version,
                        retry_cost_usd_est=0.0 if attempts > 1 and not unknown_failed_attempt else None,
                        retry_cost_unknown=attempts > 1 and unknown_failed_attempt)
            if persona_mode:
                if persona_phase == "analysis":
                    result.update({key: parts[key] for key in ("needs", "diagnostics", "peer_signals")})
                    result["envelope_schema_version"] = "persona_analysis_v1"
                else:
                    result.update({key: parts[key] for key in ("reply_candidate", "diagnostics", "peer_signals")})
                    if persona_phase == "reply":
                        result["fact_proposals"] = parts.get("fact_proposals", [])
                    result["envelope_schema_version"] = "persona_turn_v1"
            return result
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


def _permitted_persona_context(value):
    """Do not forward unapproved context categories alongside the persona snapshot."""
    if not isinstance(value, dict):
        return {"facts": [], "memories": [], "voice": {}, "voice_guide": {}}
    result = {key: value[key] for key in ("facts", "memories", "voice", "voice_guide", "peer_state",
        "fixture_only", "source_kind", "needs", "gaps", "phase_one_analysis", "persona_description",
        "persona_authored_post", "persona_authored_post_fact_ids") if key in value}
    for key in ("facts", "memories"):
        if isinstance(result.get(key), list):
            result[key] = [{name: item for name, item in row.items() if name != "source_ref"}
                           if isinstance(row, dict) else row for row in result[key]]
    return result


def _persona_evidence_texts(context):
    if not isinstance(context, dict):
        return []
    texts = []
    messages = context.get("messages") if isinstance(context.get("messages"), list) else []
    recent = context.get("recent") if isinstance(context.get("recent"), list) else []
    for message in messages + recent:
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            texts.append(message["text"])
    prior = context.get("prior_customer_turns") if isinstance(context.get("prior_customer_turns"), list) else []
    texts.extend(value for value in prior if isinstance(value, str))
    return texts


def call_luna_tool(context, transcript, schema, timeout, *, budget_guard, model=MODEL, max_tokens=1800):
    """One budget-guarded HTTP attempt for the structured conversation tool loop."""
    if not callable(budget_guard) or budget_guard() is not True:
        return {'ok': False, 'error_code': 'BUDGET_EXHAUSTED', 'attempts': 0,
                'cost_usd_est': 0.0, 'cost_unknown': False, 'usage': {}}
    api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not api_key:
        return {'ok': False, 'error_code': 'MODEL_NOT_CONFIGURED', 'attempts': 0,
                'cost_usd_est': 0.0, 'cost_unknown': False, 'usage': {}}
    if not isinstance(context, dict) or not isinstance(transcript, list) or not isinstance(schema, dict):
        return {'ok': False, 'error_code': 'MODEL_INPUT_INVALID', 'attempts': 0,
                'cost_usd_est': 0.0, 'cost_unknown': False, 'usage': {}}
    instruction = ("Bạn trả JSON conversation_tools_v1 đúng schema. Đọc bình luận và ngữ cảnh, "
        "Lượt đầu tiên bắt buộc gọi action plan, tách mọi ý trong bình luận theo chủ thể/evidence và chọn thứ tự xử lý. "
        "Sau đó mới gọi công cụ hoặc final. Trả lời câu hỏi trực tiếp trước khi gợi mở. "
        "gọi retrieve để lấy thông tin trước khi viết. Product facts chỉ được dùng sau retrieve FOUND; "
        "chỉ đề xuất ký ức sau khi truy xuất persona_memory NOT_FOUND; không tự tạo dữ kiện sản phẩm "
        "hay thông tin nhạy cảm. Dữ kiện khách cần evidence nguyên văn "
        "đúng người nói; lưu fact trước khi dùng. Tôn trọng opt-out, không ép mua hoặc hỏi lại dữ kiện "
        "đã có. Trả lời đúng câu hỏi trước; tối đa một câu hỏi có ý nghĩa. Ký ức mới phải được chấp nhận "
        "và trích memory_id trước khi kể. Khi được hỏi về AI, trả lời minh bạch. "
        "Final phải gồm semantic v2 và persona_turn_v1 với claims có nguồn; khi persona fact được phép tạo còn thiếu, "
        "chỉ khai báo qua reply.fact_proposals và cite new1/new2; tuyệt đối không tạo product fact. "
        "Dùng abstain khi dữ liệu UNKNOWN hoặc không thể trả lời an toàn.")
    schema_name = context.get('_schema_name', 'conversation_tools_v1')
    if context.get('_task') == 'story_comparison':
        instruction = ("Bạn là bộ so sánh sự kiện. Chỉ xét hai mô tả và dữ kiện cấu trúc được cung cấp. "
            "Trả verdict SAME nếu cùng một sự kiện; ADDENDUM nếu bổ sung tương thích cho cùng sự kiện; "
            "DIFFERENT khi đủ căn cứ là sự kiện riêng; CONFLICT khi mâu thuẫn; AMBIGUOUS khi thiếu căn cứ. "
            "Không dùng cùng địa điểm/xe làm bằng chứng đủ để kết luận cùng chuyện. Không suy đoán thời gian hoặc người. "
            "Chỉ trả đúng JSON schema.")
    else:
        instruction += (" 'Đang cân nhắc/muốn mua một chiếc' là buying_interest, không phải giao dịch: tiếp tục hỏi chuyện phù hợp; "
            "chỉ handoff khi khách yêu cầu nhân viên, đặt cọc, thanh toán hoặc xác nhận giao dịch rõ ràng. "
            "Không gán lời hỏi hộ cho người nói; không đánh dấu câu hỏi chờ đã trả lời nếu khách chuyển sang ý khác. "
            "Nếu khách muốn dừng, chọn stop và không gợi hỏi tiếp.")
    body = {'model': model, 'messages': [{'role': 'system', 'content': instruction},
        {'role': 'user', 'content': json.dumps({'context': context, 'exchanges': transcript},
                                               ensure_ascii=False, separators=(',', ':'))}],
        'max_tokens': max_tokens, 'response_format': {'type': 'json_schema',
            'json_schema': {'name': schema_name, 'strict': True, 'schema': schema}},
        'provider': {'require_parameters': True}}
    request = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            output = json.load(response)
    except Exception:
        return {'ok': False, 'error_code': 'MODEL_TRANSPORT_ERROR', 'attempts': 1,
                'cost_usd_est': None, 'cost_unknown': True, 'usage': {}}
    if not isinstance(output, dict):
        return {'ok': False, 'error_code': 'MODEL_RESPONSE_SHAPE', 'attempts': 1,
                'cost_usd_est': None, 'cost_unknown': True, 'usage': {}}
    usage = output.get('usage') if isinstance(output.get('usage'), dict) else {}
    cost = usage.get('cost', output.get('cost'))
    known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    result = {'ok': False, 'attempts': 1, 'cost_usd_est': float(cost) if known else None,
              'cost_unknown': not known, 'usage': usage, 'model_effective': output.get('model', model)}
    choices = output.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        result['error_code'] = 'MODEL_RESPONSE_SHAPE'; return result
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('refusal') or choice.get('finish_reason') in ('refusal', 'content_filter'):
        result['error_code'] = 'MODEL_REFUSAL'; return result
    if choice.get('finish_reason') == 'length':
        result['error_code'] = 'MODEL_OUTPUT_TRUNCATED'; return result
    try:
        content = message.get('content')
        if not isinstance(content, str): raise ValueError('non-string content')
        parsed = parse_json_strict(content)
        if not isinstance(parsed, dict): raise ValueError('non-object output')
    except (TypeError, ValueError, RecursionError):
        result['error_code'] = 'MODEL_OUTPUT_INVALID_JSON'; return result
    result.update(ok=True, message=parsed)
    return result
