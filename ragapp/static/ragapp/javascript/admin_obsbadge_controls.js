/* ragapp/static/ragapp/javascript/admin_obsbadge_controls.js */
(function () {
    "use strict";
    if (window.__ADMIN_OBSBADGE_CONTROLS__) return;
    window.__ADMIN_OBSBADGE_CONTROLS__ = true;

    const $ = (sel, root = document) => root.querySelector(sel);

    // -----------------------------
    // Cookie / CSRF
    // -----------------------------
    function getCookie(name) {
        const v = document.cookie ? document.cookie.split("; ") : [];
        for (const s of v) {
            const [k, ...rest] = s.split("=");
            if (k === name) return decodeURIComponent(rest.join(""));
        }
        return "";
    }
    const csrftoken = getCookie("csrftoken");

    // -----------------------------
    // Utilities
    // -----------------------------
    function forceButtonType(id) {
        const b = document.getElementById(id);
        try {
            if (b && b.tagName === "BUTTON") b.setAttribute("type", "button");
        } catch (_) { }
    }

    function toastOrAlert(msg) {
        // 대시보드에 토스트가 있으면 그걸 사용, 없으면 alert
        try {
            if (typeof window.toast === "function") {
                window.toast(msg);
                return;
            }
        } catch (_) { }
        try { alert(msg); } catch (_) { }
    }

    // -----------------------------
    // API
    // -----------------------------
    async function post(url, payload) {
        const res = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRFToken": csrftoken,
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload || {}),
        });

        if (!res.ok) {
            let msg = "HTTP " + res.status;
            try {
                const data = await res.json().catch(() => ({}));
                msg = (data && (data.message || data.error)) || msg;
            } catch (_) { }
            throw new Error(msg);
        }
        return res;
    }

    async function getJson(url) {
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

    // -----------------------------
    // ObsBadge root / panel bridge
    // -----------------------------
    function getRoot() {
        // 미들웨어가 주입한 루트
        return document.getElementById("dgObsRoot");
    }

    function isEnabledFromRoot(r) {
        return !!(r && r.getAttribute("data-enabled") === "true");
    }

    function refreshChip() {
        const chip = $("#obsStateChip");
        const text = $("#obsStateText");
        if (!chip || !text) return;

        const r = getRoot();
        const enabled = isEnabledFromRoot(r);

        text.textContent = enabled ? "OBS ON" : "OBS OFF";
        chip.classList.toggle("obs-on", enabled);
        chip.classList.toggle("obs-off", !enabled);
    }

    function ensurePanelOpen() {
        const r = getRoot();
        if (!r) return null;
        const panel = r.querySelector("#dgObsPanel");
        if (panel) panel.hidden = false;
        return r;
    }

    function clickInside(selector) {
        const r = ensurePanelOpen();
        if (!r) return false;
        const el = r.querySelector(selector);
        if (el) {
            try { el.click(); } catch (_) { }
            return true;
        }
        return false;
    }

    async function toggleEnabled() {
        const r = getRoot();
        const enabled = isEnabledFromRoot(r);
        await post("/admin/obsbadge/toggle/", { enabled: !enabled });
        location.reload();
    }

    // -----------------------------
    // Init
    // -----------------------------
    function init() {
        // 버튼이 submit으로 동작하는 사고 방지
        forceButtonType("adminObsBtn");
        forceButtonType("adminObsHealthBtn");
        forceButtonType("adminObsClearBtn");
        forceButtonType("adminObsCfgOpenBtn");

        refreshChip();

        const btnToggle = $("#adminObsBtn");
        const btnHealth = $("#adminObsHealthBtn");
        const btnClear = $("#adminObsClearBtn");
        const btnCfg = $("#adminObsCfgOpenBtn");

        // dgObsRoot가 없으면(= 주입이 안 된 페이지/경로) “내부 버튼 클릭” 기능은 비활성화
        // 토글/초기화는 서버 endpoint만 치면 되므로 동작 가능
        const injected = !!getRoot();
        if (!injected) {
            if (btnHealth) btnHealth.disabled = true;
            if (btnCfg) btnCfg.disabled = true;
            // clear는 history endpoint가 살아있으면 가능하니 disable은 하지 않음
        }

        if (btnToggle) {
            btnToggle.addEventListener("click", async (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                    await toggleEnabled();
                } catch (err) {
                    toastOrAlert("OBS 토글 실패: " + (err && err.message ? err.message : "error"));
                }
            });
        }

        if (btnHealth) {
            btnHealth.addEventListener("click", async (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();

                    const r = getRoot();
                    const enabled = isEnabledFromRoot(r);

                    // OFF면 먼저 ON으로 켜고 리로드(패널/버튼 생성)
                    if (!enabled) {
                        await post("/admin/obsbadge/toggle/", { enabled: true });
                        location.reload();
                        return;
                    }

                    // ON이면 패널 내부 health 버튼을 누르는 게 1순위
                    const ok = clickInside("#dgObsHealth");
                    if (ok) return;

                    // fallback: 직접 health endpoint 호출해서 보여주기
                    const data = await getJson("/admin/obsbadge/health/");
                    toastOrAlert(JSON.stringify(data, null, 2));
                } catch (err) {
                    toastOrAlert("상태 보기 실패: " + (err && err.message ? err.message : "error"));
                }
            });
        }

        if (btnClear) {
            btnClear.addEventListener("click", async (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                    await post("/admin/obsbadge/history/clear/", {});
                    location.reload();
                } catch (err) {
                    toastOrAlert("기록 초기화 실패: " + (err && err.message ? err.message : "error"));
                }
            });
        }

        if (btnCfg) {
            btnCfg.addEventListener("click", (e) => {
                try {
                    e.preventDefault();
                    e.stopPropagation();
                    const ok = clickInside("#dgObsSettings");
                    if (!ok) toastOrAlert("OBS 설정 UI를 찾지 못했습니다. (주입 상태 확인)");
                } catch (_) { }
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
