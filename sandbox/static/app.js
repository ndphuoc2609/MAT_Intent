'use strict';
const $ = (id) => document.getElementById(id);
let conversation = null;
let busy = false;
let modelReady = false;
let progressTimer = null;
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
  $('send').textContent = value && action === 'Đang xử lý comment' ? 'Đang xử lý…' : 'Gửi comment';
  $('start').textContent = value && action === 'Đang tạo bài viết' ? 'Đang tạo bài…' : 'Tạo bài viết';
}
function showPost(post) {
  $('post-preview').textContent = post.text;
  const duration = post.generation?.generation_latency_ms;
  $('post-timing').textContent = typeof duration === 'number' ? `Tạo bài: ${(duration / 1000).toFixed(1)} giây` : '';
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
  node.append(textNode('p', entries.map(([key, value]) => `${intentNames[key] || key}: ${(value * 100).toFixed(1)}%`).join(' · '), 'response-text'));
  const time = typeof result.latency_ms === 'number' ? `${(result.latency_ms / 1000).toFixed(2)} giây` : 'chưa rõ thời gian';
  const cost = result.cost_unknown || typeof result.cost_usd_est !== 'number' ? 'chi phí chưa xác định' : `$${result.cost_usd_est.toFixed(6)}`;
  const attempts = typeof result.attempts === 'number' ? ` · ${result.attempts} lần gọi` : '';
  node.append(textNode('p', `${result.model_effective || result.model_requested || 'Jev'} · ${time} · ${cost}${attempts}`, 'muted'));
  node.append(textNode('p', 'Xác suất thô để so sánh, chưa phải kết luận hoặc quyết định xử lý.', 'muted'));
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
  showPost(state.post);
  const directory = state.dealer_directory;
  $('scope').textContent = directory?.status === 'available'
    ? `Danh sách đại lý: ${directory.count} địa chỉ · Chưa xác minh cập nhật trực tuyến`
    : 'Chưa đọc được danh sách đại lý trên máy.';
  $('history').replaceChildren();
  if (!state.history.length) $('history').append(textNode('p', 'Gõ bình luận bất kỳ để bắt đầu. Luna sẽ đọc bài viết và ngữ cảnh riêng này.', 'empty'));
  for (const turn of state.history) {
    const item = textNode('article', '', 'turn');
    item.append(textNode('p', 'BẠN', 'turn-label'), textNode('p', turn.text, 'customer-text'));
    item.append(textNode('p', state.post.persona_name ? `${state.post.persona_name} · PERSONA` : 'TƯ VẤN', 'turn-label'));
    const modelFailure = turn.routing.includes('REVIEW_MODEL_ERROR');
    const reply = turn.response_text || (modelFailure
      ? turn.model_error_code === 'MODEL_REFUSAL'
        ? 'Mình không thể xử lý yêu cầu này.'
        : 'Mình chưa xử lý xong câu này. Bạn gửi lại giúp mình nhé.'
      : turn.error || 'Không có phản hồi tự động; xem điều hướng.');
    item.append(responseNode(reply));
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
      const label = call.phase === 'repair' ? 'Luna sửa kết quả' : 'Luna lần đầu';
      const time = typeof call.latency_ms === 'number' ? `${(call.latency_ms / 1000).toFixed(2)} giây` : 'chưa rõ thời gian';
      const outcome = call.validation === 'failed' ? 'không qua kiểm tra' : call.validation === 'passed' ? 'qua kiểm tra' : 'chưa kiểm tra';
      const tokens = typeof call.usage?.prompt_tokens === 'number' && typeof call.usage?.completion_tokens === 'number'
        ? ` · ${call.usage.prompt_tokens} token vào / ${call.usage.completion_tokens} token ra` : '';
      const reasoning = typeof call.reasoning_tokens === 'number' ? ` (suy luận: ${call.reasoning_tokens})` : '';
      details.append(textNode('p', `${label}: ${time} · ${outcome}${tokens}${reasoning}${call.validation_errors?.length ? ` · ${call.validation_errors.join('; ')}` : ''}`, 'muted'));
    }
    details.append(textNode('pre', JSON.stringify({semantic: turn.semantic, error: turn.error, errors: turn.errors, model_calls: turn.model_calls}, null, 2)));
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
  $('history').scrollTop = $('history').scrollHeight;
  setBusy(false);
}
$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true, 'Đang tạo bài viết');
  $('error').textContent = '';
  const body = {actor_id: 'customer-1', persona_id: $('persona-select').value,
    vehicle: $('vehicle-select').value};
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
$('turn-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !conversation || !modelReady || !$('turn-text').value.trim()) return;
  setBusy(true, 'Đang xử lý comment');
  $('error').textContent = '';
  try {
    render(await api(`/api/conversations/${encodeURIComponent(conversation.id)}/turn`, {text: $('turn-text').value}));
    $('turn-text').value = '';
    $('turn-text').focus();
  } catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
(async () => {
  try {
    const [health, setup] = await Promise.all([api('/health'), api('/api/personas')]);
    modelReady = Boolean(health.configured && health.budget_ok);
    $('model-status').textContent = !health.configured
      ? 'Chưa cấu hình OPENROUTER_API_KEY để tạo bài và phân tích comment.'
      : !health.budget_ok
        ? `Model đang bị chặn bởi budget: ${health.budget_reason}. Làm mới trang sau khi đối soát.`
        : !health.budget_enforced
          ? `Model ${health.model} sẵn sàng · Giới hạn chi phí đang tắt; mỗi comment có thể phát sinh phí không giới hạn.`
          : `Model ${health.model} sẵn sàng · Mỗi comment dùng OpenRouter và được tính ngân sách.`;
    $('persona-select').replaceChildren();
    for (const persona of setup.personas) {
      const option = textNode('option', persona.name);
      option.value = persona.id;
      option.textContent = persona.name;
      $('persona-select').append(option);
    }
    $('vehicle-select').replaceChildren();
    for (const vehicle of setup.vehicles) {
      const option = textNode('option', `Hyundai ${vehicle}`);
      option.value = vehicle;
      $('vehicle-select').append(option);
    }
  } catch (error) { $('model-status').textContent = `Không kết nối được máy chủ: ${error.message}`; }
})();
