/* ragapp/static/ragapp/javascript/media_search_controls.js */
(function () {
    "use strict";

    function $(id) {
        return document.getElementById(id);
    }

    function clamp(n, lo, hi) {
        if (Number.isFinite(lo) && n < lo) return lo;
        if (Number.isFinite(hi) && n > hi) return hi;
        return n;
    }

    function readNum(el, fallback) {
        var v = parseFloat((el && el.value) || "");
        if (!Number.isFinite(v)) return fallback;
        return v;
    }

    function setFixedPopoverPosition(btn, pop) {
        if (!btn || !pop) return;

        var r = btn.getBoundingClientRect();
        var gap = 8;

        var top = Math.round(r.bottom + gap);
        var right = Math.round(window.innerWidth - r.right);

        pop.style.top = top + "px";
        pop.style.right = right + "px";
    }

    function init() {
        var root = $("mediaSearchRoot");
        if (!root) return;

        var form = $("mediaSearchForm");
        var kEl = $("k");

        // ✅ 스태프 전용 DOM (일반 유저/시크릿에서는 null)
        var gearBtn = $("msGearBtn");
        var pop = $("msAdvPop");

        var kRange = $("kRange");
        var kValue = $("kValue");

        // stepper
        function nudge(targetId, delta) {
            var el = $(targetId);
            if (!el) return;

            var min = Number.isFinite(parseFloat(el.min)) ? parseFloat(el.min) : -Infinity;
            var max = Number.isFinite(parseFloat(el.max)) ? parseFloat(el.max) : Infinity;

            // page 스텝퍼는 템플릿에서 제거됐지만, 혹시 남아있어도 안전
            var cur = readNum(el, targetId === "page" ? 1 : 120);
            var next = clamp(cur + delta, min, max);

            el.value = String(Math.round(next));
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
        }

        var stepButtons = root.querySelectorAll(".ms-step[data-step-target][data-step]");
        stepButtons.forEach(function (btn) {
            btn.addEventListener("click", function () {
                var t = btn.getAttribute("data-step-target");
                var d = parseFloat(btn.getAttribute("data-step") || "0") || 0;
                nudge(t, d);

                // sync range view if k changed (range는 스태프만 있을 수 있음)
                if (t === "k") {
                    if (kRange && kEl) kRange.value = kEl.value;
                    if (kValue && kEl) kValue.textContent = kEl.value;
                }
            });
        });

        // k range <-> k input sync (range는 스태프만 있을 수 있음)
        function syncKToRange() {
            if (!kEl) return;
            var v = clamp(readNum(kEl, 120), 1, 600);
            kEl.value = String(Math.round(v));
            if (kRange) kRange.value = kEl.value;
            if (kValue) kValue.textContent = kEl.value;
        }

        function syncRangeToK() {
            if (!kRange || !kEl) return;
            kEl.value = String(Math.round(readNum(kRange, 120)));
            if (kValue) kValue.textContent = kEl.value;
        }

        if (kEl) {
            kEl.addEventListener("input", syncKToRange);
            kEl.addEventListener("change", syncKToRange);
        }
        if (kRange) {
            kRange.addEventListener("input", syncRangeToK);
            kRange.addEventListener("change", syncRangeToK);
        }
        syncKToRange();

        // gear popover toggle (스태프 전용 DOM 있을 때만)
        function openPop() {
            if (!gearBtn || !pop) return;
            pop.hidden = false;
            gearBtn.setAttribute("aria-expanded", "true");
            setFixedPopoverPosition(gearBtn, pop);
        }

        function closePop() {
            if (!gearBtn || !pop) return;
            pop.hidden = true;
            gearBtn.setAttribute("aria-expanded", "false");
        }

        function togglePop() {
            if (!gearBtn || !pop) return;
            if (pop.hidden) openPop();
            else closePop();
        }

        if (gearBtn && pop) {
            gearBtn.addEventListener("click", function (e) {
                e.preventDefault();
                togglePop();
            });

            window.addEventListener("resize", function () {
                if (!pop.hidden) setFixedPopoverPosition(gearBtn, pop);
            });

            window.addEventListener(
                "scroll",
                function () {
                    if (!pop.hidden) setFixedPopoverPosition(gearBtn, pop);
                },
                true
            );

            document.addEventListener("mousedown", function (e) {
                if (pop.hidden) return;
                var t = e.target;
                if (t === gearBtn || gearBtn.contains(t)) return;
                if (t === pop || pop.contains(t)) return;
                closePop();
            });

            document.addEventListener("keydown", function (e) {
                if (e.key === "Escape" && !pop.hidden) {
                    closePop();
                    gearBtn.focus();
                }
            });
        }

        // optional: submit 전에 팝오버 닫기(시각 깔끔)
        if (form) {
            form.addEventListener(
                "submit",
                function () {
                    if (pop && !pop.hidden) closePop();
                },
                true
            );
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();