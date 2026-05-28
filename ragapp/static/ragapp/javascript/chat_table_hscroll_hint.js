// ragapp/static/ragapp/javascript/chat_table_hscroll_hint.js
// - #chatThread 내 <table>을 자동으로 .chat-table-wrap로 감싸고
// - 가로 overflow/스크롤 위치에 따라 힌트 클래스 토글
(function () {
    "use strict";
    if (window.__CHAT_TABLE_HSCROLL_INITED__) return;
    window.__CHAT_TABLE_HSCROLL_INITED__ = true;

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

    var thread = $("#chatThread");
    if (!thread) return;

    var scheduled = false;

    function wrapTables() {
        var tables = $all("#chatThread .chat-bubble table");
        tables.forEach(function (tbl) {
            // 이미 wrap된 경우 스킵
            var p = tbl.parentElement;
            if (p && p.classList && p.classList.contains("chat-table-wrap")) return;

            // 테이블을 감쌀 래퍼 생성
            var w = document.createElement("div");
            w.className = "chat-table-wrap";

            // 테이블 앞에 래퍼 삽입 후 테이블 이동
            tbl.parentNode.insertBefore(w, tbl);
            w.appendChild(tbl);
        });
    }

    function updateWrap(w) {
        try {
            var max = w.scrollWidth - w.clientWidth;
            var overflow = max > 6;

            w.classList.toggle("is-overflow", overflow);
            if (!overflow) {
                w.classList.remove("is-scrolled", "is-end");
                return;
            }

            var left = w.scrollLeft || 0;
            var atStart = left <= 2;
            var atEnd = left >= (max - 2);

            w.classList.toggle("is-scrolled", !atStart);
            w.classList.toggle("is-end", atEnd);

            // scroll 이벤트는 한 번만
            if (!w.dataset.hscrollBound) {
                w.addEventListener("scroll", function () { updateWrap(w); }, { passive: true });
                w.dataset.hscrollBound = "1";
            }
        } catch (_) { }
    }

    function refresh() {
        scheduled = false;
        wrapTables();
        $all("#chatThread .chat-table-wrap").forEach(updateWrap);
    }

    function schedule() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(refresh);
    }

    // 최초 1회
    schedule();

    // 채팅이 업데이트될 때 자동 감지(가벼운 observer + rAF debounce)
    var mo = new MutationObserver(function () { schedule(); });
    mo.observe(thread, { childList: true, subtree: true });

    // 리사이즈에도 재계산
    window.addEventListener("resize", schedule, { passive: true });
})();
