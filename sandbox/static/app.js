'use strict';
const $ = (id) => document.getElementById(id);
let posts = [];
let conversation = null;
let busy = false;

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
function setBusy(value) {
  busy = value;
  $('start').disabled = value;
  $('conversation-select').disabled = value;
  $('send').disabled = value || !conversation;
  $('turn-text').disabled = value || !conversation;
  $('send').textContent = value ? 'Đang phân tích…' : 'Gửi tới Luna';
}
function showPost(post) {
  $('post-preview').textContent = post.text;
}
function render(state) {
  conversation = state;
  showPost(state.post);
  $('scope').textContent = `Page: ${state.page_id} · Actor: ${state.actor_id} · Thread: ${state.thread_id}`;
  $('history').replaceChildren();
  if (!state.history.length) $('history').append(textNode('p', 'Gõ bình luận bất kỳ để bắt đầu. Luna sẽ đọc bài viết và ngữ cảnh riêng này.', 'empty'));
  for (const turn of state.history) {
    const item = textNode('article', '', 'turn');
    item.append(textNode('p', 'KHÁCH HÀNG', 'turn-label'), textNode('p', turn.text, 'customer-text'));
    item.append(textNode('p', 'PHẢN HỒI CỦA ENGINE · SAMPLE', 'turn-label'));
    item.append(textNode('p', turn.response_text || turn.error || 'Không có phản hồi tự động; xem điều hướng.', 'response-text'));
    item.append(textNode('p', `${turn.routing.join(' · ') || 'Chưa có điều hướng'} · ${turn.sends.length ? 'Đã gửi mô phỏng' : 'Không gửi'}`, 'turn-meta'));
    const cost = turn.cost_unknown ? 'chi phí chưa xác định' : `$${Number(turn.cost_usd_est || 0).toFixed(6)}`;
    item.append(textNode('p', `${turn.attempts} lần gọi · ${cost}`, 'muted'));
    const details = document.createElement('details');
    details.append(textNode('summary', 'Xem diễn giải lượt này'), textNode('pre', JSON.stringify({semantic: turn.semantic, errors: turn.errors}, null, 2)));
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
$('post-select').addEventListener('change', () => {
  const manual = $('post-select').value === 'manual';
  $('manual').hidden = !manual;
  $('page-id').required = manual;
  $('post-text').required = manual;
});
$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true);
  $('error').textContent = '';
  const body = {actor_id: $('actor-id').value};
  if ($('post-select').value === 'manual') {
    Object.assign(body, {page_id: $('page-id').value, post_text: $('post-text').value,
      vehicles: $('vehicles').value.split(',').map((v) => v.trim()).filter(Boolean)});
  } else body.post_id = $('post-select').value;
  try {
    const state = await api('/api/conversations', body);
    const option = textNode('option', `${state.post.title} · ${state.actor_id} · ${state.id.slice(0, 6)}`);
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
  setBusy(true);
  $('error').textContent = '';
  try { render(await api(`/api/conversations/${encodeURIComponent($('conversation-select').value)}`)); }
  catch (error) { $('error').textContent = error.message; }
  finally { setBusy(false); }
});
$('turn-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !conversation || !$('turn-text').value.trim()) return;
  setBusy(true);
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
    const [health, seed] = await Promise.all([api('/health'), api('/api/posts')]);
    $('model-status').textContent = health.configured ? 'Luna sẵn sàng · Mỗi lượt dùng OpenRouter và được tính ngân sách.' : 'Chưa đặt OPENROUTER_API_KEY trong môi trường máy chủ. Bạn vẫn có thể tạo hội thoại; lượt gửi sẽ dừng an toàn cho đến khi cấu hình khóa và khởi động lại.';
    posts = seed.posts;
    $('post-select').replaceChildren();
    for (const post of posts) {
      const option = textNode('option', post.title);
      option.value = post.id;
      $('post-select').append(option);
    }
    const manual = textNode('option', 'Nhập Page / bài viết thủ công');
    manual.value = 'manual';
    $('post-select').append(manual);
  } catch (error) { $('model-status').textContent = `Không kết nối được máy chủ: ${error.message}`; }
})();
