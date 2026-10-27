let serviceKey = "";
let health = null;
let postsAfter = null;
let selectedPostId = null;
let commentsAfter = null;
let selectedCommentId = null;
let writeBusy = false;
const pendingWrites = new Map();

const $ = (selector) => document.querySelector(selector);
const node = (tag, className, text) => {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
};

function notice(message, error = false) {
  const target = $("#notice");
  target.textContent = message || "";
  target.classList.toggle("error", error);
}

function updateWriteControls() {
  const enabled = health?.outbound_enabled === true;
  document.querySelectorAll("[data-write]").forEach((button) => {
    button.disabled = !enabled || writeBusy || (button.id === "reply-submit" && !selectedCommentId);
  });
  $("#service-status").textContent = health
    ? `Page: ${health.page_configured ? "đã cấu hình" : "chưa cấu hình"} · OpenRouter: ${health.openrouter_configured ? "đã cấu hình" : "chưa cấu hình"} · outbound: ${enabled ? "ĐANG BẬT" : "đang tắt"} · inbox chờ: ${health.pending_comments} · lỗi: ${health.failed_comments}`
    : "Không đọc được trạng thái dịch vụ.";
}

async function request(path, options = {}, authorized = true) {
  const headers = new Headers(options.headers || {});
  if (authorized) headers.set("Authorization", `Bearer ${serviceKey}`);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const raw = await response.text();
  let body = {};
  try { body = raw ? JSON.parse(raw) : {}; } catch { body = { error: "invalid_server_response" }; }
  if (!response.ok) {
    const error = new Error(typeof body.error === "string" ? body.error : `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    if (response.status === 401 && authorized) clearKey();
    throw error;
  }
  return body;
}

function clearKey() {
  serviceKey = "";
  $("#api-key").value = "";
  $("#dashboard").hidden = true;
  $("#auth-panel").hidden = false;
  health = null;
  updateWriteControls();
}

function showEmpty(list, message) {
  list.replaceChildren(node("p", "muted", message));
}

function renderPosts(items, append = false) {
  const list = $("#posts-list");
  if (!append) list.replaceChildren();
  if (!items.length && !append) return showEmpty(list, "Chưa có bài viết.");
  for (const post of items) {
    const button = node("button", "record");
    button.type = "button";
    button.dataset.recordId = post.id;
    button.setAttribute("aria-pressed", String(post.id === selectedPostId));
    button.append(node("strong", "", post.message || "(Bài viết không có nội dung)"));
    button.append(node("small", "", [post.created_time, post.id].filter(Boolean).join(" · ")));
    button.addEventListener("click", () => selectPost(post));
    list.append(button);
  }
}

function renderComments(items, append = false) {
  const list = $("#comments-list");
  if (!append) list.replaceChildren();
  if (!items.length && !append) return showEmpty(list, "Bài viết này chưa có comment.");
  for (const comment of items) {
    const button = node("button", "record");
    button.type = "button";
    button.dataset.recordId = comment.id;
    button.setAttribute("aria-pressed", String(comment.id === selectedCommentId));
    button.append(node("strong", "", comment.from?.name || comment.from?.id || "Khách"));
    button.append(node("span", "record-message", comment.message || "(Comment không có nội dung)"));
    button.append(node("small", "", [comment.created_time, comment.id].filter(Boolean).join(" · ")));
    button.addEventListener("click", () => selectComment(comment));
    list.append(button);
  }
}

function renderInbox(items) {
  const list = $("#inbox-list");
  list.replaceChildren();
  if (!items.length) return showEmpty(list, "Inbox chưa có sự kiện.");
  for (const entry of items) {
    const card = node("article", "record");
    card.append(node("strong", "", `${entry.status || "UNKNOWN"} · ${entry.comment_id || ""}`));
    card.append(node("p", "", entry.event?.text || "(Không có nội dung comment)"));
    card.append(node("small", "", `Lượt xử lý: ${entry.attempts ?? 0} · ${entry.updated_at || entry.created_at || ""}`));
    if (entry.result) {
      if (Array.isArray(entry.result.routing)) card.append(node("p", "muted", `Luồng: ${entry.result.routing.join(", ")}`));
      if (entry.result.response_text) card.append(node("p", "", `Phản hồi: ${entry.result.response_text}`));
    }
    if (entry.last_error) card.append(node("p", "muted", `Lỗi: ${entry.last_error}`));
    list.append(card);
  }
}

function renderReviews(items) {
  const list = $("#reviews-list");
  list.replaceChildren();
  if (!items.length) return showEmpty(list, "Không có ca chờ duyệt.");
  for (const review of items) {
    const card = node("article", "record");
    card.append(node("strong", "", review.id || "Ca cần duyệt"));
    card.append(node("small", "", review.created_at || ""));
    card.append(node("pre", "", JSON.stringify(review.payload ?? review, null, 2)));
    list.append(card);
  }
}

async function loadHealth() {
  health = await request("/health", {}, false);
  updateWriteControls();
}

async function loadPosts(append = false) {
  const query = new URLSearchParams({ limit: "25" });
  if (append && postsAfter) query.set("after", postsAfter);
  const result = await request(`/api/posts?${query}`);
  renderPosts(Array.isArray(result.data) ? result.data : [], append);
  postsAfter = result.paging?.has_next ? result.paging?.cursors?.after || null : null;
  $("#more-posts").hidden = !postsAfter;
}

async function selectPost(post) {
  selectedPostId = post.id;
  selectedCommentId = null;
  commentsAfter = null;
  $("#comments-title").textContent = post.message || `Bài ${post.id}`;
  $("#reply-form").hidden = true;
  showEmpty($("#comments-list"), "Đang tải comment…");
  $("#more-comments").hidden = true;
  updateWriteControls();
  try {
    const query = new URLSearchParams({ limit: "25" });
    const result = await request(`/api/posts/${encodeURIComponent(post.id)}/comments?${query}`);
    if (selectedPostId !== post.id) return;
    renderComments(Array.isArray(result.data) ? result.data : []);
    commentsAfter = result.paging?.has_next ? result.paging?.cursors?.after || null : null;
    $("#more-comments").hidden = !commentsAfter;
    document.querySelectorAll("#posts-list .record").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.recordId === post.id));
    });
    notice("");
  } catch (error) {
    if (selectedPostId === post.id) {
      showEmpty($("#comments-list"), "Không tải được comment.");
      notice(`Không tải được comment: ${error.message}`, true);
    }
  }
}

function selectComment(comment) {
  selectedCommentId = comment.id;
  $("#reply-target").textContent = `Trả lời ${comment.from?.name || comment.from?.id || "khách"} · ${comment.id}`;
  $("#reply-form").hidden = false;
  document.querySelectorAll("#comments-list .record").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.recordId === comment.id));
  });
  updateWriteControls();
  $("#reply-message").focus();
}

async function loadInbox() {
  const result = await request("/api/inbox?limit=50");
  renderInbox(Array.isArray(result.data) ? result.data : []);
}

async function loadReviews() {
  const result = await request("/api/reviews?limit=50");
  renderReviews(Array.isArray(result.data) ? result.data : []);
}

async function refreshAll() {
  notice("Đang tải dữ liệu…");
  try {
    await loadHealth();
    const results = await Promise.allSettled([loadPosts(), loadInbox(), loadReviews()]);
    const failed = results.filter((result) => result.status === "rejected");
    if (failed.length) throw failed[0].reason;
    notice("Đã tải dữ liệu mới nhất.");
  } catch (error) {
    notice(`Không tải đủ dữ liệu: ${error.message}`, true);
  }
}

function writeKey(action, signature) {
  const previous = pendingWrites.get(action);
  if (previous) return previous.key;
  const key = `${action}-${crypto.randomUUID().replaceAll("-", "")}`;
  pendingWrites.set(action, { signature, key });
  return key;
}

async function submitWrite(action, path, payload, confirmText) {
  if (health?.outbound_enabled !== true) {
    notice("Outbound đang tắt; không gửi hành động này.", true);
    return;
  }
  const signature = `${path}\n${JSON.stringify(payload)}`;
  const previous = pendingWrites.get(action);
  if (previous && previous.signature !== signature) {
    notice("Lần gửi trước chưa có kết quả xác nhận. Hãy gửi lại đúng nội dung để dùng lại khóa an toàn.", true);
    return;
  }
  if (!window.confirm(confirmText)) return;
  const key = writeKey(action, signature);
  writeBusy = true;
  updateWriteControls();
  try {
    const result = await request(path, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(payload),
    });
    pendingWrites.delete(action);
    notice(`Đã xác nhận thành công. ID: ${result.id || "không có"}`);
  } catch (error) {
    const knownNoWrite = [400, 401, 404, 423].includes(error.status)
      || (error.status === 409 && error.body?.status === "FAILED")
      || (error.status === 502 && error.body?.outcome_unknown !== true);
    const pending = pendingWrites.get(action);
    const alreadyUncertain = pending?.uncertain === true;
    if (knownNoWrite && !alreadyUncertain) pendingWrites.delete(action);
    else if (!knownNoWrite && pending) pendingWrites.set(action, { ...pending, uncertain: true });
    const unresolved = alreadyUncertain || !knownNoWrite;
    const retry = unresolved ? " Hãy gửi lại cùng nội dung từ trang này để giữ nguyên khóa idempotency." : "";
    if (error.status === 423) loadHealth().catch(() => {});
    notice(`Không xác nhận được kết quả: ${error.message}.${retry}`, true);
  } finally {
    writeBusy = false;
    updateWriteControls();
  }
}

$("#api-key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  serviceKey = $("#api-key").value;
  $("#api-key").value = "";
  notice("Đang xác thực khóa…");
  try {
    await request("/api/inbox?limit=1");
    $("#auth-panel").hidden = true;
    $("#dashboard").hidden = false;
    await refreshAll();
  } catch (error) {
    notice(error.status === 401 ? "Service API key không hợp lệ." : `Không kết nối được: ${error.message}`, true);
    clearKey();
  }
});

$("#refresh-all").addEventListener("click", refreshAll);
$("#load-posts").addEventListener("click", () => loadPosts().catch((error) => notice(error.message, true)));
$("#more-posts").addEventListener("click", () => loadPosts(true).catch((error) => notice(error.message, true)));
$("#more-comments").addEventListener("click", async () => {
  if (!selectedPostId || !commentsAfter) return;
  const postId = selectedPostId;
  try {
    const query = new URLSearchParams({ limit: "25", after: commentsAfter });
    const result = await request(`/api/posts/${encodeURIComponent(postId)}/comments?${query}`);
    if (selectedPostId !== postId) return;
    renderComments(Array.isArray(result.data) ? result.data : [], true);
    commentsAfter = result.paging?.has_next ? result.paging?.cursors?.after || null : null;
    $("#more-comments").hidden = !commentsAfter;
  } catch (error) { notice(error.message, true); }
});
$("#load-inbox").addEventListener("click", () => loadInbox().catch((error) => notice(error.message, true)));
$("#load-reviews").addEventListener("click", () => loadReviews().catch((error) => notice(error.message, true)));

$("#reply-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedCommentId) return;
  const message = $("#reply-message").value;
  await submitWrite("reply", `/api/comments/${encodeURIComponent(selectedCommentId)}/reply`, { message },
    "Gửi trả lời công khai này lên Facebook Page?");
});

$("#publish-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#post-message").value;
  const link = $("#post-link").value || null;
  await submitWrite("publish", "/api/posts", { message, link }, "Đăng nội dung này công khai lên Facebook Page?");
});

loadHealth().catch(() => updateWriteControls());
