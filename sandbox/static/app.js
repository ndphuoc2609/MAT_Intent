'use strict';
const $ = (id) => document.getElementById(id);
let conversation = null;
let busy = false;
let modelReady = false;
let progressTimer = null;
let availablePersonas = [];
const jevPolls = new Set();
const intentNames = {price: 'Giá', promo: 'Khuyến mãi', loan: 'Trả góp', stock: 'Tồn kho',
  spec: 'Thông số', dealer: 'Đại lý', testdrive: 'Lái thử', buy: 'Mua xe',
  service: 'Dịch vụ', complaint: 'Khiếu nại', registration: 'Đăng ký', social: 'Xã giao'};

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function responseNode(text) {
  const node = textNode('p', '', 'response-text');
  const url = 'https://hyundai.thanhcong.vn/thong-tin-dai-ly';
  const parts = text.split(url);
  parts.forEach((part, index) => {
    if (index) {
      const link = textNode('a', 'Tra cứu đại lý Hyundai chính thức');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      node.append(link);
    }
    node.append(document.createTextNode(part));
  });
  return node;
}
function setBusy(value, action = '') {
  busy = value;
  clearInterval(progressTimer);
  $('progress').textContent = '';
  if (value && action) {
    const started = Date.now();
    const update = () => { $('progress').textContent = `${action} · ${Math.floor((Date.now() - started) / 1000)} giây`; };
    update();
    progressTimer = setInterval(update, 1000);
  }
  $('start').disabled = value;
  $('conversation-select').disabled = value;
  $('send').disabled = value || !conversation || !modelReady;
  $('turn-text').disabled = value || !conversation;
  $('actor-select').disabled = value || !conversation;
  $('send').textContent = value && action === 'Đang xử lý comment' ? 'Đang xử lý…' : 'Gửi comment';
  $('start').textContent = value && action === 'Đang tạo bài viết' ? 'Đang tạo bài…' : 'Tạo bài viết';
}
function showPost(post, state) {
  $('post-preview').textContent = post.text;
  const duration = post.generation?.generation_latency_ms;
  const notes = [];
  if (typeof duration === 'number') notes.push(`Tạo bài: ${(duration / 1000).toFixed(1)} giây`);
  if (post.persona_origin === 'database_snapshot')
    notes.push(post.persona_fixture_only
      ? 'Snapshot hồ sơ DB được dùng làm fixture sandbox'
      : 'Nguồn persona: snapshot hồ sơ DB');
  if (state?.channel) notes.push(state.channel === 'public' ? 'Kênh: bình luận công khai' : 'Kênh: tin nhắn riêng');
  if (state?.generation_mode === 'generated') notes.push('Persona tự phản hồi · gửi mô phỏng');
  $('post-timing').textContent = notes.join(' · ');
}
function showPersonaRelease(release, generatedFacts, conversationId, post) {
  const box = $('persona-release-box');
  box.hidden = !release;
  $('persona-facts').replaceChildren();
  $('generated-facts').replaceChildren();
  if (!release) return;
  const source = release.persona?.source_kind || post?.persona_source_kind || 'unknown';
  const origin = post?.persona_origin || release.release?.style?.data_origin;
  const releaseId = release.release?.id ? ` · release ${release.release.id}` : '';
  $('persona-release-note').textContent = release.fixture_only
    ? origin === 'database_snapshot'
      ? `Fixture sandbox từ snapshot hồ sơ DB${releaseId}. Fact fixture không phải xác nhận của người thật.`
      : `Kịch bản hư cấu dùng trong sandbox${releaseId}.`
    : release.release?.approved
      ? `Persona nguồn ${source} · release đã duyệt${releaseId}.`
      : `Persona nguồn ${source} · trạng thái release chưa xác nhận${releaseId}.`;
  for (const fact of release.facts || []) {
    $('persona-facts').append(textNode('li', `${fact.key}: ${String(fact.value)} · ${fact.source_kind}`));
  }
  for (const fact of generatedFacts || []) {
    const row = document.createElement('li');
    row.append(textNode('span', `${fact.fact_key}: ${fact.value_text} · generated · ${fact.status}`));
    if (fact.status === 'ACTIVE' || fact.status === 'PENDING') {
      const button = textNode('button', 'Thu hồi fact');
      button.type = 'button';
      button.className = 'secondary-button';
      button.addEventListener('click', async () => {
        const reason = window.prompt('Lý do thu hồi fact:', 'Người vận hành thu hồi');
        if (!reason?.trim()) return;
        try {
          render(await api(`/api/conversations/${encodeURIComponent(conversationId)}/facts/${encodeURIComponent(fact.gen_fact_id)}/revoke`, {reason}));
        } catch (error) { $('error').textContent = error.message; }
      });
      row.append(document.createTextNode(' '), button);
    }
    $('generated-facts').append(row);
  }
}
function showJev(node, result) {
  node.replaceChildren();
  node.append(textNode('p', 'JEV · ĐÁNH GIÁ SONG SONG', 'turn-label'));
  if (result.status === 'pending') {
    node.append(textNode('p', 'Jev đang phân tích…', 'muted'));
    return;
  }
  if (result.status !== 'complete') {
    if (result.error_code === 'MODEL_BLOCKED') {
      node.append(textNode('p', 'OpenRouter đang chặn Jev trong guardrail của workspace. Hãy cho phép model typesafe/jev-1.14 cho API key đang dùng.', 'muted'));
      const link = textNode('a', 'Mở cài đặt guardrail OpenRouter');
      link.href = 'https://openrouter.ai/workspaces/default/guardrails';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      node.append(link);
      return;
    }
    const message = result.status === 'skipped' ? 'Jev đang bận; lượt này chưa được phân tích.' :
      result.status === 'missing' ? 'Không tìm thấy kết quả Jev của lượt này.' :
      `Jev chưa trả được kết quả${result.http_status ? ` (HTTP ${result.http_status})` : ''}; kiểm tra kết nối và cấu hình model.`;
    node.append(textNode('p', message, 'muted'));
    return;
  }
  const entries = Object.entries(result.predictions || {}).filter(([, value]) =>
    typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1)
    .sort((a, b) => b[1] - a[1]);
  node.append(textNode('p', entries.map(([key, value]) => `${intentNames[key] || key}: ${value.toFixed(3)}`).join(' · '), 'response-text'));
  const time = typeof result.latency_ms === 'number' ? `${(result.latency_ms / 1000).toFixed(2)} giây` : 'chưa rõ thời gian';
  const cost = result.cost_unknown || typeof result.cost_usd_est !== 'number' ? 'chi phí chưa xác định' : `$${result.cost_usd_est.toFixed(6)}`;
  const attempts = typeof result.attempts === 'number' ? ` · ${result.attempts} lần gọi` : '';
  node.append(textNode('p', `${result.model_effective || result.model_requested || 'Jev'} · ${time} · ${cost}${attempts}`, 'muted'));
  node.append(textNode('p', 'Điểm thô của Jev, chưa hiệu chuẩn; không dùng làm kết luận hoặc quyết định xử lý.', 'muted'));
}
function renderIntentDiagnostics(node, turn, jevResult = null) {
  node.replaceChildren();
  const labels = turn.semantic?.intent_labels || (turn.semantic?.requests || []).map(request => request.topic);
  const topics = [...new Set(labels.filter(label => typeof label === 'string'))];
  if (!topics.length) return;
  node.append(textNode('p', 'Intent · score Jev và confidence Luna', 'turn-label'));
  const confidence = new Map((turn.diagnostics?.intent_confidence || []).map(item => [item.topic, item]));
  const predictions = jevResult?.status === 'complete' ? jevResult.predictions || {} : {};
  for (const topic of topics) {
    const entry = confidence.get(topic);
    const score = typeof predictions[topic] === 'number' && Number.isFinite(predictions[topic]) &&
      predictions[topic] >= 0 && predictions[topic] <= 1
      ? `${predictions[topic].toFixed(3)} · Jev shadow`
      : jevResult?.status === 'pending' ? 'Đang chờ Jev' : 'Chưa có dữ liệu Jev';
    const conf = typeof entry?.confidence === 'number' && Number.isFinite(entry.confidence) &&
      entry.confidence >= 0 && entry.confidence <= 1
      ? `${entry.confidence.toFixed(3)} · Luna tự đánh giá, chưa hiệu chuẩn`
      : 'Chưa có confidence Luna';
    const evidence = typeof entry?.evidence_excerpt === 'string' ? entry.evidence_excerpt : 'Chưa có evidence';
    node.append(textNode('p', `${intentNames[topic] || topic} · Intent score: ${score} · Confidence: ${conf} · Evidence: ${evidence}`, 'muted'));
  }
}
async function pollJev(conversationId, eventId) {
  const key = `${conversationId}:${eventId}`;
  if (jevPolls.has(key)) return;
  jevPolls.add(key);
  try {
    for (let attempt = 0; attempt < 60; attempt++) {
      if (!conversation || conversation.id !== conversationId) return;
      const node = Array.from(document.querySelectorAll('[data-jev-event]')).find(el => el.dataset.jevEvent === eventId);
      if (!node) return;
      const result = await api(`/api/conversations/${encodeURIComponent(conversationId)}/jev/${encodeURIComponent(eventId)}`);
      if (!conversation || conversation.id !== conversationId) return;
      const currentNode = Array.from(document.querySelectorAll('[data-jev-event]')).find(el => el.dataset.jevEvent === eventId);
      if (!currentNode) return;
      showJev(currentNode, result);
      const turn = conversation.history.find(item => item.jev_shadow_event_id === eventId);
      const diagnostics = currentNode.parentElement?.querySelector('[data-intent-diagnostics]');
      if (turn && diagnostics) renderIntentDiagnostics(diagnostics, turn, result);
      if (result.status !== 'pending') return;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    const node = Array.from(document.querySelectorAll('[data-jev-event]')).find(el => el.dataset.jevEvent === eventId);
    if (node && conversation?.id === conversationId) showJev(node, {status: 'failed', error: 'Jev chưa trả kết quả; hãy tải lại hội thoại.'});
  } catch (_) {
    const node = Array.from(document.querySelectorAll('[data-jev-event]')).find(el => el.dataset.jevEvent === eventId);
    if (node && conversation?.id === conversationId) showJev(node, {status: 'failed', error: 'Không tải được kết quả Jev.'});
  } finally { jevPolls.delete(key); }
}
function render(state) {
  conversation = state;
  if (state.actor_id && Array.from($('actor-select').options).some(option => option.value === state.actor_id))
    $('actor-select').value = state.actor_id;
  showPost(state.post, state);
  showPersonaRelease(state.persona_release, state.generated_facts, state.id, state.post);
  const directory = state.dealer_directory;
  $('scope').textContent = directory?.status === 'available'
    ? `Danh sách đại lý: ${directory.count} địa chỉ · Chưa xác minh cập nhật trực tuyến`
    : 'Chưa đọc được danh sách đại lý trên máy.';
  $('history').replaceChildren();
  if (!state.history.length) $('history').append(textNode('p', 'Gõ bình luận bất kỳ để bắt đầu. Luna sẽ đọc bài viết và ngữ cảnh riêng này.', 'empty'));
  for (const turn of state.history) {
    const item = textNode('article', '', 'turn');
    item.append(textNode('p', turn.actor_id === 'customer-2' ? 'NGƯỜI 2' : 'NGƯỜI 1', 'turn-label'), textNode('p', turn.text, 'customer-text'));
    const personaSource = state.post.persona_origin === 'database_snapshot'
      ? ' · SNAPSHOT DB'
      : state.post.persona_fixture_only ? ' · FIXTURE' : '';
    item.append(textNode('p', state.post.persona_name
      ? `${state.post.persona_name} · PERSONA${personaSource}`
      : 'TƯ VẤN', 'turn-label'));
    const modelFailure = turn.routing.includes('REVIEW_MODEL_ERROR');
    const reply = turn.response_text || (turn.candidate_text
      ? `Nội dung chưa gửi: ${turn.candidate_text}`
      : modelFailure
      ? turn.model_error_code === 'MODEL_REFUSAL'
        ? 'Mình không thể xử lý yêu cầu này.'
        : 'Mình chưa xử lý xong câu này. Bạn gửi lại giúp mình nhé.'
      : turn.error || 'Không có phản hồi tự động; xem điều hướng.');
    item.append(responseNode(reply));
    const plan = turn.response_plan || {};
    const factIds = (plan.fact_ids || []).join(', ') || 'không có';
    const productIds = (plan.facts_used || []).join(', ') || 'không có';
    const effects = (turn.effects || []).map(effect => `${effect.purpose}: ${effect.status}${effect.text ? ` — ${effect.text}` : ''}`).join(', ') || 'không có';
    item.append(textNode('p', `${turn.sends.length ? 'Đã gửi mô phỏng' : 'Không gửi'} · ${turn.routing.join(' · ') || 'chưa có điều hướng'}`, 'turn-meta'));
    item.append(textNode('p', `Fact persona: ${factIds} · Fact sản phẩm: ${productIds}${state.sample_facts ? ' (DỮ LIỆU MẪU)' : ''} · Tác vụ engine: ${effects}`, 'muted'));
    const supplied = (turn.persona_facts_supplied || []).map(fact => `${fact.key}: ${String(fact.value)}`).join(' · ');
    if (supplied) item.append(textNode('p', `Fact đã cấp cho Luna: ${supplied}`, 'muted'));
    for (const proposal of turn.proposal_results || []) {
      const value = proposal.value ? ` · ${proposal.value}` : '';
      const reason = proposal.reason ? ` · Lý do: ${proposal.reason}` : '';
      item.append(textNode('p', `Fact generated ${proposal.key || proposal.proposal_id}: ${proposal.status}${value}${reason}`, 'muted'));
    }
    for (const map of turn.dealer_maps || []) {
      try {
        const url = new URL(map.url);
        if (url.protocol !== 'https:' || url.hostname !== 'www.google.com' || url.pathname !== '/maps/search/') continue;
        const link = textNode('a', `Xem ${map.name} trên bản đồ`, 'muted');
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        const row = document.createElement('p');
        row.append(link);
        item.append(row);
      } catch (_) { /* Ignore malformed links. */ }
    }
    if (turn.routing.some(route => route.startsWith('REVIEW') || route.startsWith('HANDOFF') || route.startsWith('ERROR')))
      item.append(textNode('p', 'Cần kiểm tra thêm', 'turn-meta'));
    const cost = turn.cost_unknown ? 'chi phí chưa xác định' : `$${Number(turn.cost_usd_est || 0).toFixed(6)}`;
    const duration = typeof turn.pipeline_latency_ms === 'number' ? ` · ${(turn.pipeline_latency_ms / 1000).toFixed(1)} giây` : '';
    const lunaLabels = turn.semantic?.intent_labels || (turn.semantic?.requests || []).map(request => request.topic);
    if (lunaLabels?.length) item.append(textNode('p', `Luna nhận diện: ${[...new Set(lunaLabels)].map(label => intentNames[label] || label).join(', ')}`, 'muted'));
    const diagnosticsNode = document.createElement('div');
    diagnosticsNode.dataset.intentDiagnostics = 'true';
    renderIntentDiagnostics(diagnosticsNode, turn, turn.jev_shadow_event_id ? {status: 'pending'} : null);
    if (diagnosticsNode.childNodes.length) item.append(diagnosticsNode);
    if (turn.jev_shadow_event_id) {
      const jev = document.createElement('div');
      jev.dataset.jevEvent = turn.jev_shadow_event_id;
      showJev(jev, {status: 'pending'});
      item.append(jev);
      queueMicrotask(() => pollJev(state.id, turn.jev_shadow_event_id));
    }
    const details = document.createElement('details');
    details.append(textNode('summary', 'Xem diễn giải lượt này'));
    details.append(textNode('p', `${turn.routing.join(' · ') || 'Chưa có điều hướng'} · ${turn.sends.length ? 'Đã gửi mô phỏng' : 'Không gửi'}`, 'turn-meta'));
    details.append(textNode('p', `${turn.attempts} lần gọi · ${cost}${duration}`, 'muted'));
    for (const call of turn.model_calls || []) {
      const phaseName = {analysis: 'Phân tích Luna', analysis_retry: 'Phân tích Luna · thử lại',
        fallback_semantic: 'Phân tích dự phòng', reply: 'Luna viết lời đáp', reply_retry: 'Luna viết lời đáp · thử lại'};
      const label = phaseName[call.phase] || (call.phase === 'repair' ? 'Luna sửa kết quả' : 'Luna lần đầu');
      const time = typeof call.latency_ms === 'number' ? `${(call.latency_ms / 1000).toFixed(2)} giây` : 'chưa rõ thời gian';
      const outcome = call.validation === 'failed' ? 'không qua kiểm tra' : call.validation === 'passed' ? 'qua kiểm tra' : 'chưa kiểm tra';
      const tokens = typeof call.usage?.prompt_tokens === 'number' && typeof call.usage?.completion_tokens === 'number'
        ? ` · ${call.usage.prompt_tokens} token vào / ${call.usage.completion_tokens} token ra` : '';
      const reasoning = typeof call.reasoning_tokens === 'number' ? ` (suy luận: ${call.reasoning_tokens})` : '';
      details.append(textNode('p', `${label}: ${time} · ${outcome}${tokens}${reasoning}${call.validation_errors?.length ? ` · ${call.validation_errors.join('; ')}` : ''}`, 'muted'));
    }
    details.append(textNode('pre', JSON.stringify({semantic: turn.semantic, diagnostics: turn.diagnostics,
      peer_signals: turn.peer_signals, needs: turn.persona_needs, gaps: turn.persona_gaps,
      supplied_facts: turn.persona_facts_supplied, proposals: turn.proposal_results,
      phase_errors: turn.phase_errors, error: turn.error, errors: turn.errors,
      model_calls: turn.model_calls}, null, 2)));
    item.append(details);
    $('history').append(item);
  }
  const last = state.history.at(-1);
  $('route').textContent = last ? last.routing.join(' · ') : 'Chưa có lượt';
  $('semantic').textContent = last ? JSON.stringify(last.semantic, null, 2) : 'Chưa có diễn giải';
  $('requests').textContent = JSON.stringify(state.active_requests, null, 2);
  $('pending').textContent = JSON.stringify(state.pending, null, 2);
  $('fake-sends').textContent = JSON.stringify(state.fake_sends, null, 2);
  $('outbox').textContent = JSON.stringify(state.outbox, null, 2);
  $('lead-profile').textContent = JSON.stringify(state.lead_profile || {}, null, 2);
  $('history').scrollTop = $('history').scrollHeight;
  setBusy(false);
}
$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true, 'Đang tạo bài viết');
  $('error').textContent = '';
  const body = {actor_id: 'customer-1', persona_id: $('persona-select').value, channel: $('channel-select').value};
  try {
    const state = await api('/api/conversations', body);
    const option = textNode('option', `${state.post.persona_name} · ${state.post.title} · ${state.id.slice(0, 6)}`);
    option.value = state.id;
    if (!$('conversation-select').options[0].value) $('conversation-select').replaceChildren();
    $('conversation-select').append(option);
    $('conversation-select').value = state.id;
    render(state);
    $('turn-text').focus();
  } catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
$('conversation-select').addEventListener('change', async () => {
  if (!$('conversation-select').value) return;
  setBusy(true, 'Đang tải hội thoại');
  $('error').textContent = '';
  try { render(await api(`/api/conversations/${encodeURIComponent($('conversation-select').value)}`)); }
  catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
$('actor-select').addEventListener('change', async () => {
  if (busy || !conversation) return;
  const conversationId = conversation.id;
  const actorId = $('actor-select').value;
  setBusy(true, 'Đang tải lượt của người này');
  $('error').textContent = '';
  try {
    render(await api(`/api/conversations/${encodeURIComponent(conversationId)}?actor_id=${encodeURIComponent(actorId)}`));
  } catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
$('turn-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !conversation || !modelReady || !$('turn-text').value.trim()) return;
  setBusy(true, 'Đang xử lý comment');
  $('error').textContent = '';
  try {
    render(await api(`/api/conversations/${encodeURIComponent(conversation.id)}/turn`, {text: $('turn-text').value, actor_id: $('actor-select').value}));
    $('turn-text').value = '';
    $('turn-text').focus();
  } catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
(async () => {
  try {
    const [health, setup, sessions] = await Promise.all([api('/health'), api('/api/personas'), api('/api/conversations')]);
    for (const session of sessions.conversations || []) {
      const option = textNode('option', session.title);
      option.value = session.id;
      $('conversation-select').append(option);
    }
    modelReady = Boolean(health.configured && health.budget_ok);
    $('model-status').textContent = !health.configured
      ? 'Chưa cấu hình OPENROUTER_API_KEY để tạo bài và phân tích comment.'
      : !health.budget_ok
        ? `Model đang bị chặn bởi budget: ${health.budget_reason}. Làm mới trang sau khi đối soát.`
        : !health.budget_enforced
          ? `Model ${health.model} sẵn sàng · Giới hạn chi phí đang tắt; mỗi comment có thể phát sinh phí không giới hạn. Jev shadow ${health.jev_shadow_enabled ? 'đang bật' : 'đang tắt'}.`
          : `Model ${health.model} sẵn sàng · Mỗi comment dùng OpenRouter và được tính ngân sách. Jev shadow ${health.jev_shadow_enabled ? 'đang bật' : 'đang tắt'}.`;
    $('persona-select').replaceChildren();
    availablePersonas = setup.personas;
    for (const persona of availablePersonas) {
      const suffix = persona.source_kind === 'fictional_fixture'
        ? ' · FIXTURE' : persona.status === 'PUBLISHED' ? ' · HỒ SƠ ĐÃ DUYỆT' : '';
      const option = textNode('option', `${persona.name}${suffix}`);
      option.value = persona.id;
      $('persona-select').append(option);
    }
  } catch (error) { $('model-status').textContent = `Không kết nối được máy chủ: ${error.message}`; }
})();
