/* ragapp/static/ragapp/javascript/sidebar_v2.js
   Tools Drawer toggle (trigger: #toolsDockBtn)
   - window capture + 독점 처리로 타 스크립트 간섭 방지
   - FIX: Quick Tools 링크가 안 눌리는 문제(포인터다운에서 닫아버리는 케이스)
   - ✅ A11y FIX: 닫을 때 aria-hidden 전에 포커스를 드로어 밖으로 이동 + inert 적용
*/
(() => {
    "use strict";

    if (typeof window !== "undefined") {
        if (window.__SIDEBAR_V2_INITED__) return;
        window.__SIDEBAR_V2_INITED__ = true;
        console.log("[sidebar_v2] loaded 2026-01-16 (a11y focus + inert)");
    }

    const ANIM_MS = 220;

    // ✅ 열기 전 포커스 저장(닫을 때 복귀)
    let lastFocusEl = null;

    function drawerEl() {
        return document.getElementById("toolsDrawer");
    }

    function safeFocus(el) {
        if (!el || typeof el.focus !== "function") return;
        try {
            el.focus({ preventScroll: true });
        } catch (_) {
            try { el.focus(); } catch (_) { }
        }
    }

    function focusOutsideDrawer(btn) {
        const fallbackBtn = btn || document.getElementById("toolsDockBtn");
        safeFocus(fallbackBtn || lastFocusEl || document.body);
    }

    function focusInsideDrawer(d) {
        if (!d) return;
        const target =
            d.querySelector(".tools-close-btn") ||
            d.querySelector("[data-tools-close]") ||
            d.querySelector(".tools-panel");

        if (target && target.classList && target.classList.contains("tools-panel")) {
            if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        }
        safeFocus(target);
    }

    function isOpen() {
        const d = drawerEl();
        const aria = d ? d.getAttribute("aria-hidden") : "true";
        if (aria === "false") return true;
        return document.body.classList.contains("tools-open") || document.body.dataset.toolsOpen === "1";
    }

    function clearTimer() {
        if (window.__TOOLS_DRAWER_CLOSE_TIMER__) {
            clearTimeout(window.__TOOLS_DRAWER_CLOSE_TIMER__);
            window.__TOOLS_DRAWER_CLOSE_TIMER__ = null;
        }
    }

    function applyState(open, btn) {
        const d = drawerEl();
        if (!d) return;

        // ✅ btn이 안 넘어오는 케이스(Esc/백드롭/닫기버튼 등)까지 동기화
        const realBtn = btn || document.getElementById("toolsDockBtn");

        if (open) {
            d.hidden = false;
            d.removeAttribute("hidden");
            d.style.display = "";
            d.removeAttribute("inert");               // ✅ 열림: inert 제거
            d.setAttribute("aria-hidden", "false");
        } else {
            // ✅ 닫힘: inert + aria-hidden (포커스는 setOpen/forceClosed에서 먼저 밖으로)
            d.setAttribute("inert", "");
            d.setAttribute("aria-hidden", "true");
        }

        document.body.dataset.toolsOpen = open ? "1" : "0";
        document.body.classList.toggle("tools-open", open);

        if (realBtn) {
            realBtn.setAttribute("aria-expanded", open ? "true" : "false");
            realBtn.setAttribute("aria-label", open ? "도구 패널 닫기" : "도구 패널 열기");
        }
    }

    function forceClosed(btn) {
        const d = drawerEl();
        if (!d) return;

        const realBtn = btn || document.getElementById("toolsDockBtn");

        clearTimer();

        // ✅ aria-hidden/hidden 전에 포커스를 반드시 밖으로
        focusOutsideDrawer(realBtn);

        document.body.dataset.toolsOpen = "0";
        document.body.classList.remove("tools-open");

        d.setAttribute("inert", "");
        d.setAttribute("aria-hidden", "true");
        d.hidden = true;

        if (realBtn) {
            realBtn.setAttribute("aria-expanded", "false");
            realBtn.setAttribute("aria-label", "도구 패널 열기");
        }
    }

    function setOpen(open, btn) {
        const d = drawerEl();
        if (!d) return;

        clearTimer();

        if (open) {
            // ✅ 열기 전 포커스 저장(닫을 때 복귀)
            lastFocusEl = document.activeElement;

            d.hidden = false;
            d.removeAttribute("hidden");
            d.style.display = "";
            d.removeAttribute("inert");
            d.setAttribute("aria-hidden", "false");

            requestAnimationFrame(() => {
                applyState(true, btn);

                // 타 스크립트가 순간 덮어써도 1회 더 복구
                setTimeout(() => {
                    if (document.body.classList.contains("tools-open")) {
                        const dd = drawerEl();
                        if (dd) {
                            dd.hidden = false;
                            dd.removeAttribute("hidden");
                            dd.style.display = "";
                            dd.removeAttribute("inert");
                            dd.setAttribute("aria-hidden", "false");
                        }
                    }
                }, 0);

                try {
                    const panel = d.querySelector(".tools-panel");
                    if (panel && typeof panel.scrollTo === "function") panel.scrollTo(0, 0);
                } catch (_) { }

                // ✅ 포커스를 드로어 내부로(접근성)
                focusInsideDrawer(d);
            });
            return;
        }

        // ✅ 닫기: aria-hidden 전에 포커스를 반드시 밖으로 이동 (경고 해결 핵심)
        const realBtn = btn || document.getElementById("toolsDockBtn");
        focusOutsideDrawer(realBtn);

        // 닫힘 상태에서 포커스/클릭 차단
        d.setAttribute("inert", "");

        applyState(false, btn);

        window.__TOOLS_DRAWER_CLOSE_TIMER__ = setTimeout(() => {
            const dd = drawerEl();
            if (dd) dd.hidden = true;
            window.__TOOLS_DRAWER_CLOSE_TIMER__ = null;
        }, ANIM_MS);
    }

    let ignoreClickUntil = 0;

    function stopAll(e) {
        try {
            e.preventDefault();
            e.stopPropagation();
            if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
        } catch (_) { }
    }

    function handler(e) {
        const t = e.target;

        // ✅ pointerdown은 토글 버튼만 처리 (링크/퀵툴은 click에서만)
        const btn = t && t.closest ? t.closest("#toolsDockBtn") : null;

        if (e.type === "pointerdown") {
            if (btn) {
                const now = Date.now();
                ignoreClickUntil = now + 450;
                stopAll(e);
                setOpen(!isOpen(), btn);
            }
            return;
        }

        // ---- 이하 click 처리 ----

        // 1) 토글 버튼은 독점 처리
        if (btn) {
            const now = Date.now();
            if (now < ignoreClickUntil) {
                stopAll(e);
                return;
            }
            stopAll(e);
            setOpen(!isOpen(), btn);
            return;
        }

        const d = drawerEl();
        if (!d || !isOpen()) return;

        // 2) 닫기(백드롭/닫기버튼)
        const close = t && t.closest ? t.closest("[data-tools-close]") : null;
        if (close) {
            stopAll(e);
            setOpen(false);
            return;
        }

        // 3) 링크 이동(Quick Tools 포함): 네비게이션을 방해하지 않게 "닫기"만 살짝 미룸
        const link = t && t.closest ? t.closest("#toolsDrawer a.side-link[href]") : null;
        if (link) {
            const href = link.getAttribute("href") || "";
            if (href && href !== "#") {
                setTimeout(() => setOpen(false), 0);
            }
            return; // preventDefault 하지 않음(이동 보장)
        }

        // 4) 특정 버튼 클릭 시 닫기
        const igGuide = t && t.closest ? t.closest("#toolsDrawer [data-open-ig-guide]") : null;
        if (igGuide) {
            setTimeout(() => setOpen(false), 0);
            return;
        }

        const qaragBtn = t && t.closest ? t.closest("#toolsDrawer #qaragLaunchBtn") : null;
        if (qaragBtn) {
            setTimeout(() => setOpen(false), 0);
            return;
        }
    }

    function init() {
        forceClosed();

        // window 캡처로 최우선 수신
        window.addEventListener("pointerdown", handler, true);
        window.addEventListener("click", handler, true);

        window.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && isOpen()) setOpen(false);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
