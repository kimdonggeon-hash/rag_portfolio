/* ragapp/static/ragapp/javascript/usage_widget.js
   ✅ 서버 DailyUsage(/api/usage/status/) 기반 사용량 위젯
   - 홈 카드(#usage-widget) + 상단 pill(#usageWidget) 동시 지원
   - window.QARAG_USAGE.bump 호환 유지(이제 bump = refresh)
*/
(function () {
    "use strict";

    var API_URL = "/api/usage/status/";
    var last = null;

    var inFlight = false;
    var pending = false;
    var lastFetchAt = 0;

    function n(v, d) {
        var x = Number(v);
        return Number.isFinite(x) ? x : d;
    }

    function fmtInf(v) {
        if (v === null || v === undefined) return "∞";
        var x = n(v, null);
        if (x === null) return "∞";
        if (x <= 0) return "∞";
        return String(x);
    }

    function fmtRemain(v) {
        if (v === null || v === undefined) return "∞";
        return String(v);
    }

    function setText(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = String(value);
    }

    // 상단 pill (feature.html)
    function updateHeaderPills(data) {
        var root = document.getElementById("usageWidget");
        if (!root) return;

        var rem = (data && data.remaining) ? data.remaining : {};

        ["image"].forEach(function (k) {
            var el = root.querySelector('[data-kind="' + k + '"]');
            if (!el) return;

            var b = el.querySelector("b");
            if (b) b.textContent = fmtRemain(rem[k]);

            el.classList.toggle("fx-usage-zero", rem[k] === 0);
        });
    }

    // 홈 카드(오늘 사용량 위젯)
    function updateHomeCard(data) {
        var root = document.getElementById("usage-widget");
        if (!root) return;

        var used = (data && data.used) ? data.used : {};
        var limits = (data && data.limits) ? data.limits : {};
        var rem = (data && data.remaining) ? data.remaining : {};
        var isAdmin = !!(data && data.unlimited);

        // used
        setText("uw-web-used", n(used.web, 0));
        setText("uw-rag-used", n(used.rag, 0));
        setText("uw-pdf-used", n(used.pdf, 0));

        // limits: 어드민이면 ∞로 보이게
        setText("uw-web-limit", isAdmin ? "∞" : fmtInf(limits.web));
        setText("uw-rag-limit", isAdmin ? "∞" : fmtInf(limits.rag));
        setText("uw-pdf-limit", isAdmin ? "∞" : fmtInf(limits.pdf));

        // ✅ remaining 기반 경고 표시(0=red, 1=amber, null=unlimited)
        function setRowState(kind, rowId) {
            var row = document.getElementById(rowId);
            if (!row) return;

            var r = rem[kind]; // null이면 무제한
            row.classList.toggle("uw-zero", r === 0);
            row.classList.toggle("uw-low", r === 1);
            row.classList.toggle("uw-unlim", r === null || isAdmin);

            // undefined(키 없음)면 상태 표시 제거
            if (r === undefined && !isAdmin) {
                row.classList.remove("uw-zero", "uw-low", "uw-unlim");
            }
        }

        setRowState("web", "uw-row-web");
        setRowState("rag", "uw-row-rag");
        setRowState("pdf", "uw-row-pdf");
    }

    function apply(data) {
        updateHeaderPills(data);
        updateHomeCard(data);
    }

    function refresh() {
        var hasHeader = !!document.getElementById("usageWidget");
        var hasCard = !!document.getElementById("usage-widget");
        if (!hasHeader && !hasCard) return;

        var now = Date.now();
        if (inFlight) { pending = true; return; }
        if (now - lastFetchAt < 800) {
            pending = true;
            setTimeout(refresh, 900 - (now - lastFetchAt));
            return;
        }

        inFlight = true;
        lastFetchAt = now;

        fetch(API_URL, { credentials: "same-origin" })
            .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            })
            .then(function (j) {
                last = j || null;
                apply(last);
            })
            .catch(function () {
                // 실패하면 템플릿 초기 표시 유지
            })
            .finally(function () {
                inFlight = false;
                if (pending) { pending = false; refresh(); }
            });
    }

    function boot() {
        refresh();

        window.QARAG_USAGE = window.QARAG_USAGE || {};
        window.QARAG_USAGE.refresh = refresh;
        window.QARAG_USAGE.bump = function () { refresh(); }; // 호환
        window.QARAG_USAGE.getState = function () {
            try { return last ? JSON.parse(JSON.stringify(last)) : null; }
            catch (_) { return last; }
        };
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
