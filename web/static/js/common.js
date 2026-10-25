/* 公共工具：API 请求、Token 管理、轻量 Markdown 渲染 */
const API = {
  token() { return localStorage.getItem("token") || ""; },
  setToken(t) { localStorage.setItem("token", t); },
  logout() { localStorage.removeItem("token"); location.href = "/login"; },
  async request(path, options = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" }, options.headers || {});
    if (this.token()) headers["Authorization"] = "Bearer " + this.token();
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (resp.status === 401) { this.logout(); throw new Error("未登录"); }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || "请求失败");
    return data;
  },
  get(path) { return this.request(path); },
  post(path, body) { return this.request(path, { method: "POST", body: JSON.stringify(body || {}) }); },
  patch(path, body) { return this.request(path, { method: "PATCH", body: JSON.stringify(body || {}) }); },
  del(path) { return this.request(path, { method: "DELETE" }); },
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 轻量 markdown：粗体 / 行内代码 / 无序列表 / 段落（先转义，防 XSS） */
function renderMarkdown(text) {
  const esc = escapeHtml(text);
  const lines = esc.split(/\n/);
  let html = "", inList = false;
  const inline = s => s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const line of lines) {
    const t = line.trim();
    const isBullet = /^([-*•])\s+/.test(t);
    if (isBullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inline(t.replace(/^([-*•])\s+/, "")) + "</li>";
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (t === "") continue;
      html += "<p>" + inline(t) + "</p>";
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}

function toast(msg, ms = 2200) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

const TOOL_LABELS = {
  query_order: "查询订单",
  query_product: "查询商品",
  search_knowledge: "检索知识库",
  recommend_products: "商品推荐",
  apply_after_sales: "售后政策校验",
  create_ticket: "登记工单",
};

const STATUS_CLASS = {
  "待付款": "badge-orange", "待发货": "badge-blue", "已发货": "badge-purple",
  "已签收": "badge-green", "售后中": "badge-orange", "已退款": "badge-gray",
  "已取消": "badge-gray", "新建": "badge-red", "处理中": "badge-orange",
  "已解决": "badge-green", "在售": "badge-green", "下架": "badge-gray",
  "高": "badge-red", "中": "badge-orange", "低": "badge-gray",
  "正常": "badge-green",
};

function badge(v) {
  return `<span class="badge ${STATUS_CLASS[v] || "badge-gray"}">${escapeHtml(v)}</span>`;
}
