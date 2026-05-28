/* ragapp/static/ragapp/javascript/board.js
   게시판 전용 JS만.
   - data-confirm: 확인창
   - 신고 모달(#reportModal): 글/댓글 신고 공용
   - 토스트(#boardToast): ?reported=1/0 안내
*/

(function () {
    "use strict";

    const log = (tag, data) => {
        try {
            if (window.DG && typeof window.DG.log === "function") window.DG.log(`board:${tag}`, data);
            else console.log(`[board] ${tag}`, data ?? "");
        } catch (_) { }
    };

    const ready = (fn) => {
        try {
            if (window.DG && typeof window.DG.ready === "function") return window.DG.ready(fn);
            if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn, { once: true });
            else fn();
        } catch (e) {
            log("ready_error", e && e.message ? e.message : e);
        }
    };

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    function toast(text) {
        const el = $("#boardToast");
        if (!el) return;
        try {
            el.textContent = String(text || "");
            el.hidden = false;
            el.classList.add("is-show");
            window.clearTimeout(el._t);
            el._t = window.setTimeout(() => {
                el.classList.remove("is-show");
                window.setTimeout(() => { el.hidden = true; }, 180);
            }, 2200);
        } catch (_) { }
    }

    function parseReportedToast() {
        try {
            const p = new URLSearchParams(location.search);
            const v = p.get("reported");
            if (v === "1") toast("신고가 접수되었습니다. 감사합니다.");
            else if (v === "0") toast("신고가 처리되지 않았어요. 잠시 후 다시 시도해 주세요.");
            // ✅ 토스트 한 번만 뜨게 URL에서 reported 제거(새로고침 중복 방지)
            if (v === "1" || v === "0") {
                p.delete("reported");
                const qs = p.toString();
                const clean = location.pathname + (qs ? `?${qs}` : "") + location.hash;
                try { history.replaceState(null, "", clean); } catch (_) { }
            }
        } catch (_) { }
    }

    function bindConfirm() {
        $$("[data-confirm]").forEach((el) => {
            el.addEventListener("click", (e) => {
                const msg = el.getAttribute("data-confirm") || "";
                if (msg && !window.confirm(msg)) {
                    e.preventDefault();
                    e.stopPropagation();
                }
            });
        });
    }

    function bindReportModal() {
        const dlg = $("#reportModal");
        if (!dlg) return;

        const form = $("#reportModalForm", dlg);
        const titleEl = $("#reportModalTitle", dlg);
        const subEl = $("#reportModalSub", dlg);

        const close = () => {
            try { dlg.close(); } catch (_) { }
        };

        // 열기 버튼들: .js-report-open + data-report-*
        $$(".js-report-open").forEach((btn) => {
            btn.addEventListener("click", () => {
                const url = btn.getAttribute("data-report-url") || "";
                const kind = btn.getAttribute("data-report-kind") || "신고";
                const target = btn.getAttribute("data-report-target") || "";

                if (form && url) form.setAttribute("action", url);
                if (titleEl) titleEl.textContent = kind;
                if (subEl) subEl.textContent = target;

                // 입력 초기화(선택)
                const msg = $("textarea", dlg);
                if (msg) msg.value = "";

                try { dlg.showModal(); } catch (_) { }
                window.setTimeout(() => { if (msg) msg.focus(); }, 60);
            });
        });

        // 닫기 버튼
        $$(".js-modal-close", dlg).forEach((b) => b.addEventListener("click", close));

        // 바깥 클릭 닫기
        dlg.addEventListener("click", (e) => {
            if (e.target === dlg) close();
        });

        // submit 중복 방지
        if (form) {
            form.addEventListener("submit", () => {
                $$("button[type='submit']", form).forEach((b) => {
                    try {
                        b.disabled = true;
                        b.setAttribute("aria-disabled", "true");
                    } catch (_) { }
                });
            });
        }
    }

    function focusSearch() {
        // 게시판 목록에서만 검색창 자동 포커스(있으면)
        const inp = $(".board-search input[type='search']");
        if (!inp) return;
        // 모바일은 키보드 튀는거 싫으면 막아도 됨
        try { inp.focus({ preventScroll: true }); } catch (_) { try { inp.focus(); } catch (_) { } }
    }

    ready(() => {
        log("boot", { path: location.pathname });

        bindConfirm();
        parseReportedToast();
        bindReportModal();
        focusSearch();
    });
})();
