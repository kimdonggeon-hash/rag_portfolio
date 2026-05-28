/* ragapp/static/ragapp/javascript/admin_dashboard_obs.js */
(function () {
  "use strict";
  if (window.__ADMIN_DASHBOARD_OBS__) return;
  window.__ADMIN_DASHBOARD_OBS__ = true;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ─────────────────────────────────────────────────────────────
  // Mount points (대시보드 스코프)
  // ─────────────────────────────────────────────────────────────
  function getRoot() {
    return document.getElementById("dgDashObsRoot") || document.body;
  }
  function getMount() {
    return document.getElementById("dgDashObsMount") || getRoot() || document.body;
  }

  // ─────────────────────────────────────────────────────────────
  // CSS (scoped to #dgDashObsRoot)
  // ─────────────────────────────────────────────────────────────
  function ensureCss() {
    if (document.getElementById("opsObsCss")) return;
    const st = document.createElement("style");
    st.id = "opsObsCss";
    st.textContent = `
/* ===== Admin OPS OBS Modal (scoped: #dgDashObsRoot) ===== */
#dgDashObsRoot .ops-modal-overlay{
  position:fixed; inset:0;
  display:flex; align-items:flex-end; justify-content:center;
  padding:18px;
  background: rgba(2,6,23,.55);
  z-index: 2147482500;
  opacity:0; pointer-events:none;
  transition: opacity .18s ease;
}
#dgDashObsRoot .ops-modal-overlay.open{ opacity:1; pointer-events:auto; }

#dgDashObsRoot .ops-modal{
  width: min(720px, 96vw);
  border-radius: 18px;
  border: 1px solid rgba(148,163,184,.22);
  background: rgba(15,23,42,.96);
  color: rgba(226,232,240,.92);
  box-shadow: 0 22px 70px rgba(2,6,23,.62);

  max-height: calc(100vh - 32px);
  overflow: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;

  transform: translateY(10px);
  transition: transform .18s ease;
  font: 13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial;
}
#dgDashObsRoot .ops-modal-overlay.open .ops-modal{ transform: translateY(0); }

#dgDashObsRoot .ops-head{
  display:flex; align-items:center; justify-content:space-between;
  gap:12px;
  padding:14px 14px 12px;
  border-bottom: 1px solid rgba(148,163,184,.16);
}
#dgDashObsRoot .ops-title{ font-weight: 900; font-size: 15px; letter-spacing:.2px; }
#dgDashObsRoot .ops-sub{ font-size: 12px; color: rgba(226,232,240,.70); margin-top:2px; }

#dgDashObsRoot .ops-x{
  all: unset;
  cursor:pointer;
  width:38px; height:38px;
  display:inline-flex; align-items:center; justify-content:center;
  border-radius: 12px;
  border: 1px solid rgba(148,163,184,.18);
  background: rgba(255,255,255,.05);
  color: rgba(226,232,240,.92);
  font-weight: 900;
}
#dgDashObsRoot .ops-x:hover{ background: rgba(255,255,255,.08); }

#dgDashObsRoot .ops-body{ padding: 14px; display:grid; gap: 12px; }

#dgDashObsRoot .ops-section{
  border: 1px solid rgba(148,163,184,.14);
  background: rgba(2,6,23,.26);
  border-radius: 16px;
  padding: 12px;
}
#dgDashObsRoot .ops-section-title{
  font-weight: 900;
  margin-bottom: 10px;
  color: rgba(226,232,240,.88);
}

#dgDashObsRoot .ops-row{
  display:flex; align-items:center; justify-content:space-between;
  gap: 14px;
  padding: 10px 0;
  border-bottom: 1px dashed rgba(148,163,184,.14);
}
#dgDashObsRoot .ops-row:last-child{ border-bottom:none; }

#dgDashObsRoot .ops-row-name{ font-weight: 900; }
#dgDashObsRoot .ops-row-desc{ margin-top:3px; font-size:12px; color: rgba(226,232,240,.68); }

#dgDashObsRoot .ops-switch{ position:relative; width: 52px; height: 30px; flex: 0 0 auto; }
#dgDashObsRoot .ops-switch input{ position:absolute; inset:0; opacity:0; cursor:pointer; }
#dgDashObsRoot .ops-slider{
  position:absolute; inset:0;
  border-radius: 999px;
  border: 1px solid rgba(148,163,184,.22);
  background: rgba(255,255,255,.06);
  transition: background .18s ease, border-color .18s ease;
}
#dgDashObsRoot .ops-slider::after{
  content:"";
  position:absolute; top: 3px; left: 3px;
  width: 22px; height: 22px;
  border-radius: 999px;
  background: rgba(226,232,240,.92);
  box-shadow: 0 10px 26px rgba(2,6,23,.45);
  transition: transform .18s ease;
}
#dgDashObsRoot .ops-switch input:checked + .ops-slider{
  background: rgba(99,102,241,.22);
  border-color: rgba(99,102,241,.35);
}
#dgDashObsRoot .ops-switch input:checked + .ops-slider::after{ transform: translateX(22px); }

#dgDashObsRoot .ops-metrics{
  display:grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
#dgDashObsRoot .ops-metric{
  border-radius: 14px;
  border: 1px solid rgba(148,163,184,.14);
  background: rgba(255,255,255,.03);
  padding: 10px;
}
#dgDashObsRoot .ops-metric-k{ color: rgba(226,232,240,.70); font-size:12px; }
#dgDashObsRoot .ops-metric-v{ font-size: 18px; font-weight: 900; margin-top:4px; }

#dgDashObsRoot .ops-note{ margin-top:10px; font-size:12px; color: rgba(226,232,240,.70); }

#dgDashObsRoot .ops-actions{ display:flex; gap:10px; margin-top: 10px; justify-content:flex-end; flex-wrap: wrap; }
#dgDashObsRoot .ops-btn{
  all: unset;
  cursor:pointer;
  padding: 8px 12px;
  border-radius: 12px;
  border: 1px solid rgba(148,163,184,.18);
  background: rgba(255,255,255,.05);
  color: rgba(226,232,240,.92);
  font-weight: 900;
}
#dgDashObsRoot .ops-btn:hover{ background: rgba(255,255,255,.08); }
#dgDashObsRoot .ops-btn.danger{
  border-color: rgba(239,68,68,.26);
  background: rgba(239,68,68,.12);
}
#dgDashObsRoot .ops-btn:disabled{ opacity:.55; cursor:not-allowed; }

#dgDashObsRoot .ops-foot{
  display:flex; gap: 8px; flex-wrap:wrap;
  padding-top: 2px;
}
#dgDashObsRoot .ops-pill{
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid rgba(148,163,184,.18);
  background: rgba(255,255,255,.04);
  font-weight: 900;
  font-size: 12px;
}
#dgDashObsRoot .ops-pill[data-on="1"]{
  border-color: rgba(34,197,94,.24);
  background: rgba(34,197,94,.12);
}

#dgDashObsRoot .ops-toast{
  position: fixed;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%) translateY(10px);
  background: rgba(15,23,42,.95);
  border: 1px solid rgba(148,163,184,.22);
  color: rgba(226,232,240,.92);
  padding: 10px 12px;
  border-radius: 14px;
  box-shadow: 0 18px 60px rgba(2,6,23,.55);
  z-index: 2147482600;
  opacity: 0;
  transition: opacity .18s ease, transform .18s ease;
  font: 13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial;
}
#dgDashObsRoot .ops-toast.show{
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}
@media (max-width: 520px){
  #dgDashObsRoot .ops-modal{ width: min(720px, 98vw); }
  #dgDashObsRoot .ops-metrics{ grid-template-columns: 1fr; }
  #dgDashObsRoot .ops-actions{ justify-content:stretch; }
  #dgDashObsRoot .ops-btn{ text-align:center; }
}
    `;
    document.head.appendChild(st);
  }

  // ─────────────────────────────────────────────────────────────
  // Cookie / API
  // ─────────────────────────────────────────────────────────────
  function getCookie(name) {
    const v = document.cookie ? document.cookie.split("; ") : [];
    for (const s of v) {
      const [k, ...rest] = s.split("=");
      if (k === name) return decodeURIComponent(rest.join(""));
    }
    return null;
  }
  const csrftoken = getCookie("csrftoken");

  async function apiGet(url) {
    const res = await fetch(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (data && (data.message || data.error)) || ("HTTP " + res.status);
      throw new Error(msg);
    }
    return data;
  }

  async function apiPost(url, bodyObj) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrftoken || "",
      },
      body: JSON.stringify(bodyObj || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (data && (data.message || data.error)) || ("HTTP " + res.status);
      throw new Error(msg);
    }
    return data;
  }

  // ─────────────────────────────────────────────────────────────
  // Toast (루트 스코프 내부에만 표시)
  // ─────────────────────────────────────────────────────────────
  function toast(msg) {
    const root = getRoot();
    const el = document.createElement("div");
    el.className = "ops-toast";
    el.textContent = msg;
    root.appendChild(el);

    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => {
        try { el.remove(); } catch (_) { }
      }, 220);
    }, 1600);
  }

  function fmtOnOff(v) {
    return (v === 1 || v === true) ? "ON" : "OFF";
  }

  // ─────────────────────────────────────────────────────────────
  // Modal (단일 인스턴스 + mount 내부에만 존재)
  // ─────────────────────────────────────────────────────────────
  function buildModal() {
    ensureCss();
    const mount = getMount();

    // ✅ 중복 제거: mount 안의 기존 OPS 모달만 제거
    $$('.ops-modal-overlay[data-ops-modal="1"]', mount).forEach((x) => {
      try { x.remove(); } catch (_) { }
    });

    const overlay = document.createElement("div");
    overlay.className = "ops-modal-overlay";
    overlay.dataset.opsModal = "1";

    const titleId = "opsModalTitle_" + Date.now() + "_" + Math.floor(Math.random() * 10000);

    overlay.innerHTML = `
      <div class="ops-modal" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
        <div class="ops-head">
          <div>
            <div class="ops-title" id="${titleId}">🛠 OPS 컨트롤</div>
            <div class="ops-sub">점검/쓰기잠금/스파이크 + 접속자 수</div>
          </div>
          <button type="button" class="ops-x" aria-label="닫기">✕</button>
        </div>

        <div class="ops-body">
          <div class="ops-section">
            <div class="ops-section-title">토글</div>

            <div class="ops-row">
              <div class="ops-row-left">
                <div class="ops-row-name">점검 모드</div>
                <div class="ops-row-desc">일반 사용자에게 503 안내. 관리자는 그대로 접근.</div>
              </div>
              <label class="ops-switch">
                <input type="checkbox" data-target="maintenance">
                <span class="ops-slider"></span>
              </label>
            </div>

            <div class="ops-row">
              <div class="ops-row-left">
                <div class="ops-row-name">쓰기 잠금</div>
                <div class="ops-row-desc">POST/업로드/저장 같은 “쓰기” 요청만 잠시 차단.</div>
              </div>
              <label class="ops-switch">
                <input type="checkbox" data-target="writelock">
                <span class="ops-slider"></span>
              </label>
            </div>

            <div class="ops-row">
              <div class="ops-row-left">
                <div class="ops-row-name">스파이크 가드</div>
                <div class="ops-row-desc">요청 폭주 시 429로 완충(서버 보호).</div>
              </div>
              <label class="ops-switch">
                <input type="checkbox" data-target="spike_guard">
                <span class="ops-slider"></span>
              </label>
            </div>
          </div>

          <div class="ops-section">
            <div class="ops-section-title">접속자</div>
            <div class="ops-metrics">
              <div class="ops-metric">
                <div class="ops-metric-k">일반</div>
                <div class="ops-metric-v" data-m="users">-</div>
              </div>
              <div class="ops-metric">
                <div class="ops-metric-k">관리자</div>
                <div class="ops-metric-v" data-m="admins">-</div>
              </div>
              <div class="ops-metric">
                <div class="ops-metric-k">합계</div>
                <div class="ops-metric-v" data-m="total">-</div>
              </div>
            </div>
            <div class="ops-note" data-m="window">최근 -초 기준</div>

            <div class="ops-actions">
              <button type="button" class="ops-btn danger" data-act="reset-presence">초기화</button>
              <button type="button" class="ops-btn" data-act="refresh">새로고침</button>
            </div>
          </div>

          <div class="ops-foot">
            <span class="ops-pill" data-pill="maintenance">점검: -</span>
            <span class="ops-pill" data-pill="writelock">쓰기잠금: -</span>
            <span class="ops-pill" data-pill="spike_guard">스파이크: -</span>
          </div>
        </div>
      </div>
    `;

    const modal = $(".ops-modal", overlay);
    const closeBtn = $(".ops-x", overlay);

    const prevFocus = document.activeElement;
    let closed = false;
    let onKey = null;

    function cleanup() {
      if (onKey) document.removeEventListener("keydown", onKey);
      onKey = null;

      if (typeof overlay.__opsCleanup === "function") {
        try { overlay.__opsCleanup(); } catch (_) { }
      }
      overlay.__opsCleanup = null;

      try {
        if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus();
      } catch (_) { }
    }

    function close() {
      if (closed) return;
      closed = true;

      overlay.classList.remove("open");
      cleanup();
      setTimeout(() => {
        try { overlay.remove(); } catch (_) { }
      }, 180);
    }

    overlay.addEventListener("pointerdown", (e) => {
      if (e.target === overlay) close();
      e.stopPropagation();
    });

    modal.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
    });

    closeBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      close();
    });

    onKey = function (e) {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);

    mount.appendChild(overlay);

    overlay.getBoundingClientRect();
    requestAnimationFrame(() => overlay.classList.add("open"));

    try { closeBtn.focus(); } catch (_) { }

    return { overlay, modal, close };
  }

  // ─────────────────────────────────────────────────────────────
  // Logic (폴링/정리 확실)
  // ─────────────────────────────────────────────────────────────
  function bindLogic(modalRoot, overlay) {
    const END_STATUS = "/ragadmin/ops/api/status/";
    const END_SET = "/ragadmin/ops/api/set/";
    const END_RESET = "/ragadmin/ops/api/reset-presence/";

    let polling = null;
    let busy = false;
    let destroyed = false;

    const switches = $$('input[type="checkbox"][data-target]', modalRoot);

    function setBusy(v) {
      busy = !!v;
      switches.forEach((x) => { x.disabled = busy; });
      $$("button.ops-btn", modalRoot).forEach((b) => { b.disabled = busy; });
    }

    async function refresh() {
      if (busy || destroyed) return;
      setBusy(true);
      try {
        const data = await apiGet(END_STATUS);
        const ops = (data && data.ops) || {};
        const online = (data && data.online) || {};

        switches.forEach((el) => {
          const t = el.getAttribute("data-target");
          if (t === "maintenance") el.checked = (ops.maintenance === 1);
          if (t === "writelock") el.checked = (ops.writelock === 1);
          if (t === "spike_guard") el.checked = (ops.spike_guard === 1);
        });

        const setPill = (key, val) => {
          const p = modalRoot.querySelector(`[data-pill="${key}"]`);
          if (!p) return;
          p.textContent =
            (key === "maintenance" ? "점검: " :
              key === "writelock" ? "쓰기잠금: " : "스파이크: ") + fmtOnOff(val);
          p.dataset.on = (val === 1) ? "1" : "0";
        };
        setPill("maintenance", ops.maintenance);
        setPill("writelock", ops.writelock);
        setPill("spike_guard", ops.spike_guard);

        const setM = (k, v) => {
          const el = modalRoot.querySelector(`[data-m="${k}"]`);
          if (el) el.textContent = String(v ?? "-");
        };
        setM("users", online.users ?? 0);
        setM("admins", online.admins ?? 0);
        setM("total", online.total ?? 0);

        const note = modalRoot.querySelector('[data-m="window"]');
        if (note) note.textContent = `최근 ${online.window_sec ?? "-"}초 기준`;
      } catch (e) {
        toast("불러오기 실패: " + (e && e.message ? e.message : "error"));
      } finally {
        setBusy(false);
      }
    }

    async function setToggle(target, enabled) {
      if (busy || destroyed) return;
      setBusy(true);
      try {
        await apiPost(END_SET, { target, enabled });
        toast("적용됨");
        await refresh();
      } catch (e) {
        toast("적용 실패: " + (e && e.message ? e.message : "error"));
        await refresh();
      } finally {
        setBusy(false);
      }
    }

    modalRoot.addEventListener("change", (e) => {
      const el = e.target;
      if (!el || el.tagName !== "INPUT") return;
      const target = el.getAttribute("data-target");
      if (!target) return;
      setToggle(target, !!el.checked);
    });

    modalRoot.addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;

      const act = btn.getAttribute("data-act");
      if (act === "refresh") {
        await refresh();
        return;
      }
      if (act === "reset-presence") {
        if (busy || destroyed) return;
        setBusy(true);
        try {
          await apiPost(END_RESET, {});
          toast("초기화 완료");
          await refresh();
        } catch (err) {
          toast("초기화 실패: " + (err && err.message ? err.message : "error"));
        } finally {
          setBusy(false);
        }
      }
    });

    refresh();
    polling = setInterval(refresh, 5000);

    overlay.__opsCleanup = function () {
      destroyed = true;
      try { clearInterval(polling); } catch (_) { }
      polling = null;
    };
  }

  function init() {
    const btn = document.getElementById("adminOpsBtn");
    if (!btn) return;

    try {
      if (btn.tagName === "BUTTON") btn.setAttribute("type", "button");
    } catch (_) { }

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const { overlay, modal } = buildModal();
      bindLogic(modal, overlay);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
