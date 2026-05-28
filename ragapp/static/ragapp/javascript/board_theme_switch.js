(function () {
    "use strict";

    // 중복 실행 방지
    if (window.__boardThemeSwitchInitDone) return;
    window.__boardThemeSwitchInitDone = true;

    const KEYS = ["theme", "dg_theme", "board_theme"]; // 혹시 기존 베이스 스크립트 키랑 충돌/불일치 대비
    const root = document.documentElement;

    function preferred() {
        try {
            return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        } catch (_) {
            return "light";
        }
    }

    function readSaved() {
        try {
            for (const k of KEYS) {
                const v = localStorage.getItem(k);
                if (v === "dark" || v === "light") return v;
            }
        } catch (_) { }
        return null;
    }

    function writeSaved(theme) {
        try {
            for (const k of KEYS) localStorage.setItem(k, theme);
        } catch (_) { }
    }

    function apply(theme) {
        // ✅ html + body 둘 다 박아서 CSS가 어디를 보든 먹게
        root.setAttribute("data-theme", theme);
        if (document.body) document.body.setAttribute("data-theme", theme);

        // (선택) class 기반도 같이(혹시 기존 CSS가 class를 볼 수도 있어서)
        root.classList.toggle("theme-dark", theme === "dark");
        root.classList.toggle("theme-light", theme === "light");

        // 버튼 UI 갱신
        const btn = document.getElementById("boardThemeSwitch");
        if (btn) {
            const ico = btn.querySelector(".bts-ico");
            if (ico) ico.textContent = theme === "dark" ? "🌙" : "☀️";
            btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
            btn.setAttribute("title", theme === "dark" ? "라이트 모드" : "다크 모드");
            btn.setAttribute("aria-label", theme === "dark" ? "라이트 모드로 전환" : "다크 모드로 전환");
        }

        writeSaved(theme);

        // 디버그용(원하면 지워도 됨)
        // console.log("[boardTheme] applied:", theme);
    }

    function toggle() {
        const cur = root.getAttribute("data-theme") || preferred();
        apply(cur === "dark" ? "light" : "dark");
    }

    function ensureButtonTopLevel() {
        const btn = document.getElementById("boardThemeSwitch");
        if (!btn) return null;

        // ✅ 핵심: 버튼이 어떤 래퍼/transform 아래에 있든, body 끝으로 “이동”
        if (document.body && btn.parentElement !== document.body) {
            document.body.appendChild(btn);
        }

        // ✅ 클릭/레이어 강제
        btn.style.position = "fixed";
        btn.style.top = "14px";
        btn.style.right = "14px";
        btn.style.zIndex = "2147483647";
        btn.style.pointerEvents = "auto";

        return btn;
    }

    function init() {
        // 1) 초기 테마
        apply(readSaved() || preferred());

        // 2) 버튼을 최상단으로 올려서 클릭 죽는 문제 제거
        ensureButtonTopLevel();

        // 3) 클릭 이벤트 (캡처 단계로 더 강하게)
        document.addEventListener(
            "click",
            function (e) {
                const t = e.target && e.target.closest ? e.target.closest("#boardThemeSwitch") : null;
                if (!t) return;
                e.preventDefault();
                toggle();
            },
            true
        );

        // 4) 혹시 다른 스크립트가 data-theme를 되돌리는 경우 방어
        const obs = new MutationObserver(() => {
            const v = readSaved();
            if (!v) return;
            const cur = root.getAttribute("data-theme");
            if (cur !== v) apply(v);
        });
        obs.observe(root, { attributes: true, attributeFilter: ["data-theme", "class"] });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
