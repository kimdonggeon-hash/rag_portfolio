/* ragapp/static/ragapp/javascript/board_reports.js */
(function () {
    "use strict";

    const dlg = document.getElementById("reportNoteDialog");
    const form = document.getElementById("reportNoteForm");
    const btnClose = document.querySelector(".js-rep-close");

    // ✅ 이 페이지가 아니면 조용히 종료 (콘솔 빨간 로그 방지)
    if (!dlg || !form) return;

    const ridEl = document.getElementById("repRid");
    const actEl = document.getElementById("repAction");
    const titleEl = document.getElementById("repDlgTitle");
    const subEl = document.getElementById("repDlgSub");
    const noteEl = document.getElementById("repNote");

    const metaBase = document.querySelector('meta[name="report-action-base"]');
    const actionBase = metaBase ? (metaBase.getAttribute("content") || "") : "";

    function buildActionUrl(rid) {
        if (!actionBase) return "";

        const sRid = String(rid || "").trim();
        if (!sRid) return "";

        // 1) 가장 확실한 패턴 치환: '/0/action/' or '/0/action'
        if (actionBase.indexOf("/0/action/") !== -1) {
            return actionBase.replace("/0/action/", "/" + sRid + "/action/");
        }
        if (actionBase.endsWith("/0/action")) {
            return actionBase.replace("/0/action", "/" + sRid + "/action");
        }

        // 2) fallback: 마지막 숫자 세그먼트만 교체 (혹시 URL 패턴이 달라져도 최대한 대응)
        // 예: .../reports/0/action/  -> .../reports/<rid>/action/
        return actionBase.replace(/\/\d+(?=\/action\/?$)/, "/" + sRid);
    }

    function closeDlg() {
        try { dlg.close(); } catch (_) { }
    }

    document.addEventListener("click", function (e) {
        const b = e.target && e.target.closest ? e.target.closest("[data-rid][data-act]") : null;
        if (!b) return;

        const rid = b.getAttribute("data-rid");
        const act = b.getAttribute("data-act"); // resolve / reject
        if (!rid) return;

        const t = b.getAttribute("data-title") || "처리";
        const s = b.getAttribute("data-sub") || "";

        const url = buildActionUrl(rid);
        if (url) form.setAttribute("action", url);

        if (ridEl) ridEl.value = String(rid);
        if (actEl) actEl.value = String(act || "resolve");

        if (titleEl) titleEl.textContent = t;
        if (subEl) subEl.textContent = s;

        // ✅ showModal 먼저, focus는 그 다음 tick에서
        try { dlg.showModal(); } catch (_) { }

        if (noteEl) {
            noteEl.value = "";
            window.setTimeout(() => {
                try { noteEl.focus(); } catch (_) { }
            }, 0);
        }
    });

    if (btnClose) btnClose.addEventListener("click", closeDlg);

    dlg.addEventListener("click", function (e) {
        if (e.target === dlg) closeDlg();
    });

    // ✅ 일괄 처리 확인: board.js의 data-confirm과 충돌 없이 자체 confirm만 수행
    document.querySelectorAll(".js-bulk").forEach(function (f) {
        f.addEventListener("submit", function (e) {
            const msg =
                f.getAttribute("data-confirm") ||
                "실행할까요?";
            if (!confirm(msg)) {
                e.preventDefault();
                e.stopPropagation();
            }
        });
    });
})();
