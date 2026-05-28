/* ragapp/static/ragapp/javascript/base.js
   전역 공통 JS: 충돌 없는 유틸/초기화만
*/

(function () {
    "use strict";

    // 안전한 로그(원하면 window.dglog와 연동)
    function log(tag, data) {
        try {
            if (typeof window.dglog === "function") window.dglog(tag, data);
            else console.log(`[base] ${tag}`, data ?? "");
        } catch (_) { }
    }

    // DOM ready
    function ready(fn) {
        try {
            if (document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", fn, { once: true });
            } else {
                fn();
            }
        } catch (e) {
            log("ready_error", e && e.message ? e.message : e);
        }
    }

    // 전역 네임스페이스(필요할 때만 사용)
    window.DG = window.DG || {};
    window.DG.ready = ready;
    window.DG.log = log;

    ready(function () {
        // 전역 초기화 훅 (나중에 토글/토스트/공통 메뉴 등 여기서)
        log("boot", { path: location.pathname });
    });
})();
