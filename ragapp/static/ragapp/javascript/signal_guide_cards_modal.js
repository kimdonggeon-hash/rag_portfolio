/* ragapp/static/ragapp/javascript/signal_guide_cards_modal.js */
(function () {
    "use strict";

    if (window.__GUIDE_CARDS_INITED__) return;
    window.__GUIDE_CARDS_INITED__ = true;

    function qs(sel, root) { return (root || document).querySelector(sel); }
    function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

    var modal = qs("#guideModal");
    if (!modal) return;

    var panel = qs(".gd-panel", modal);
    var track = qs("#gdTrack", modal);

    // ✅ "표" 카드 제거: 🖼️(이미지 업로드) + 🔎(이미지 검색)만 남긴다
    var cards = qsa("[data-gd-card]", modal).filter(function (card) {
        var t = (qs(".gd-card-title", card) || {}).textContent || "";
        t = String(t).trim();
        return (t !== "표 데이터 올려두기" && t !== "표에서 검색하기");
    });

    var btnPrev = qs("#gdPrev", modal);
    var btnNext = qs("#gdNext", modal);
    var dotsHost = qs("#gdDots", modal);
    var openers = qsa("[data-guide-open]");
    var closers = qsa("[data-gd-close]", modal);

    var idx = 0;
    var lastActive = null;

    function setHidden(h) {
        modal.hidden = !!h;
        modal.setAttribute("aria-hidden", h ? "true" : "false");
        document.body.classList.toggle("gd-open", !h);
    }

    function renderDots() {
        if (!dotsHost) return;
        dotsHost.innerHTML = "";
        cards.forEach(function (_, i) {
            var d = document.createElement("span");
            d.className = "gd-dot" + (i === idx ? " is-on" : "");
            d.setAttribute("role", "button");
            d.setAttribute("tabindex", "0");
            d.setAttribute("aria-label", (i + 1) + "번째 가이드");
            d.addEventListener("click", function () { go(i); });
            d.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(i); }
            });
            dotsHost.appendChild(d);
        });
    }

    function updateNav() {
        if (btnPrev) btnPrev.disabled = (idx <= 0);
        if (btnNext) btnNext.textContent = (idx >= cards.length - 1) ? "닫기" : "다음";
    }

    function apply() {
        if (!track) return;

        // ✅ track 안의 카드들도 동일하게 2개만 남기기(표 카드 DOM 제거)
        //    - JS만 바꿔도 동작은 되지만, 슬라이드 폭(100% 단위)이 꼬일 수 있어서 제거가 안전함.
        var all = qsa("[data-gd-card]", modal);
        all.forEach(function (card) {
            if (cards.indexOf(card) === -1) {
                try { card.parentNode && card.parentNode.removeChild(card); } catch (_) { }
            }
        });

        track.style.transform = "translateX(" + (-idx * 100) + "%)";
        renderDots();
        updateNav();
    }

    function go(n) {
        idx = Math.max(0, Math.min(cards.length - 1, n));
        apply();
    }

    function open() {
        lastActive = document.activeElement;
        setHidden(false);
        go(0);
        setTimeout(function () {
            try { panel && panel.focus(); } catch (_) { }
        }, 0);
    }

    function close() {
        setHidden(true);
        if (lastActive && typeof lastActive.focus === "function") {
            try { lastActive.focus(); } catch (_) { }
        }
    }

    openers.forEach(function (b) {
        b.addEventListener("click", function (e) {
            e.preventDefault();
            open();
        });
    });

    closers.forEach(function (el) {
        el.addEventListener("click", function (e) {
            e.preventDefault();
            close();
        });
    });

    if (btnPrev) {
        btnPrev.addEventListener("click", function () { go(idx - 1); });
    }

    if (btnNext) {
        btnNext.addEventListener("click", function () {
            if (idx >= cards.length - 1) close();
            else go(idx + 1);
        });
    }

    document.addEventListener("keydown", function (e) {
        if (modal.hidden) return;
        if (e.key === "Escape") { e.preventDefault(); close(); }
        if (e.key === "ArrowLeft") { go(idx - 1); }
        if (e.key === "ArrowRight") { go(idx + 1); }
    });

    // 초기 상태
    setHidden(true);
})();
