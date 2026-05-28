// ragapp/static/ragapp/javascript/guide_cards_modal.js
(function () {
    "use strict";
    if (window.__IG_GUIDE_INITED__) return;
    window.__IG_GUIDE_INITED__ = true;

    function qs(sel, root) { return (root || document).querySelector(sel); }
    function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

    const modal = qs("#igGuideModal");

    // ✅ 모달이 없으면: 어디로도 이동하지 않음 (원인만 콘솔에 남김)
    if (!modal) {
        document.addEventListener("click", function (e) {
            const t = e.target && e.target.closest ? e.target.closest("[data-open-ig-guide]") : null;
            if (!t) return;
            e.preventDefault();
            try { console.warn("[ig-guide] #igGuideModal not found. include guide_cards_modal.html once."); } catch (_) { }
        }, { passive: false });
        return;
    }

    const viewport = qs("[data-ig-viewport]", modal);
    const btnPrev = qs("[data-ig-prev]", modal);
    const btnNext = qs("[data-ig-next]", modal);
    const dotsWrap = qs("[data-ig-dots]", modal);
    const cards = qsa("[data-ig-card]", modal);

    let index = 0;
    let lastActive = null;
    let prevBodyOverflow = "";

    function lockScroll(lock) {
        document.documentElement.classList.toggle("ig-noscroll", lock);
        try {
            if (lock) { prevBodyOverflow = document.body.style.overflow || ""; document.body.style.overflow = "hidden"; }
            else { document.body.style.overflow = prevBodyOverflow || ""; }
        } catch (_) { }
    }

    function ensureDots() {
        if (!dotsWrap || dotsWrap.childElementCount > 0) return;
        cards.forEach((_, i) => {
            const b = document.createElement("button");
            b.type = "button";
            b.setAttribute("aria-label", `페이지 ${i + 1}`);
            b.addEventListener("click", () => scrollToIndex(i));
            dotsWrap.appendChild(b);
        });
    }

    function syncDots() {
        if (!dotsWrap) return;
        const dots = qsa("button", dotsWrap);
        dots.forEach((d, i) => d.setAttribute("aria-current", i === index ? "true" : "false"));
    }

    function setBtnStates() {
        const max = Math.max(0, cards.length - 1);
        if (btnPrev) btnPrev.disabled = (index <= 0);
        if (btnNext) btnNext.disabled = (index >= max);
    }

    function syncAll() { ensureDots(); syncDots(); setBtnStates(); }

    function scrollToIndex(i, smooth = true) {
        if (!viewport) return;
        const max = cards.length - 1;
        index = Math.max(0, Math.min(max, i));
        viewport.scrollTo({ left: index * viewport.clientWidth, behavior: smooth ? "smooth" : "auto" });
        syncAll();
    }

    function openModal() {
        lastActive = document.activeElement;
        modal.setAttribute("aria-hidden", "false");
        lockScroll(true);
        scrollToIndex(0, false);
        setTimeout(() => { try { viewport && viewport.focus(); } catch (_) { } }, 0);
        syncAll();
    }

    function closeModal() {
        modal.setAttribute("aria-hidden", "true");
        lockScroll(false);
        try { lastActive && lastActive.focus && lastActive.focus(); } catch (_) { }
        lastActive = null;
    }

    // close handlers
    qsa("[data-ig-close]", modal).forEach((el) => {
        el.addEventListener("click", (e) => { e.preventDefault(); closeModal(); });
    });

    // esc + arrows
    document.addEventListener("keydown", (e) => {
        if (modal.getAttribute("aria-hidden") !== "false") return;
        if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
        if (e.key === "ArrowLeft") { e.preventDefault(); scrollToIndex(index - 1); }
        if (e.key === "ArrowRight") { e.preventDefault(); scrollToIndex(index + 1); }
    });

    if (btnPrev) btnPrev.addEventListener("click", () => scrollToIndex(index - 1));
    if (btnNext) btnNext.addEventListener("click", () => scrollToIndex(index + 1));

    if (viewport) {
        viewport.addEventListener("scroll", () => {
            const w = viewport.clientWidth || 1;
            const nextIndex = Math.round(viewport.scrollLeft / w);
            if (nextIndex !== index) { index = nextIndex; syncAll(); }
        }, { passive: true });
    }

    // ✅ data-open-ig-guide: 무조건 모달 오픈
    document.addEventListener("click", (e) => {
        const t = e.target && e.target.closest ? e.target.closest("[data-open-ig-guide]") : null;
        if (!t) return;
        e.preventDefault();
        openModal();
    }, { passive: false });

    // autoshow
    if (document.body && document.body.hasAttribute("data-ig-autoshow")) openModal();

    syncAll();
})();
