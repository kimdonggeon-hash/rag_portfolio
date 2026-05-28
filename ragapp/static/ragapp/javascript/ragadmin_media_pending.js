/* ragadmin_media_pending.js */
(() => {
    "use strict";

    const $ = (s, r = document) => r.querySelector(s);
    const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

    const grid = $("#dgGrid");
    if (!grid) return;

    const toastEl = $("#dgToast");
    const countEl = $("#dgCount");
    const autoReloadEl = $("#dgAutoReload");
    const filterEl = $("#dgFilter");
    const collapseAllEl = $("#dgCollapseAll");
    const reloadBtn = $("#dgReloadBtn");

    // Preview elements
    const stageEl = $("#dgPreviewStage");
    const imgEl = $("#dgPreviewImg");
    const emptyEl = $("#dgPreviewEmpty");
    const zoomEl = $("#dgZoom");
    const zoomLabelEl = $("#dgZoomLabel");

    const pNewTab = $("#dgPNewTab");
    const pOrigTab = $("#dgPOrigTab");

    const penaltiesUrl = grid.dataset.penaltiesUrl || "";
    const approveUrl = grid.dataset.approveUrl || "";
    const rejectUrl = grid.dataset.rejectUrl || "";
    const liftUrl = grid.dataset.liftUrl || "";

    let selectedCard = null;

    function cards() {
        return $$(".dg-card", grid);
    }

    function ensureOpen(card) {
        if (!card) return;
        if (card !== selectedCard || card.open !== true) selectCard(card, true);
    }

    function showToast(msg, ms = 1600) {
        if (!toastEl) return;
        toastEl.textContent = msg;
        toastEl.hidden = false;
        window.clearTimeout(showToast._t);
        showToast._t = window.setTimeout(() => (toastEl.hidden = true), ms);
    }

    function setCount(n) {
        if (countEl) countEl.textContent = String(Math.max(0, n));
    }

    function decCount() {
        if (!countEl) return;
        const n = Number(countEl.textContent || "0");
        setCount(n - 1);
    }

    function getCookie(name) {
        const v = `; ${document.cookie}`;
        const parts = v.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(";").shift();
        return "";
    }

    async function postJson(url, payload) {
        const csrf = getCookie("csrftoken");
        const res = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload),
            credentials: "same-origin",
        });

        let data = null;
        const ct = (res.headers.get("content-type") || "").toLowerCase();
        if (ct.includes("application/json")) {
            data = await res.json().catch(() => null);
        } else {
            const text = await res.text().catch(() => "");
            data = { ok: res.ok, text };
        }
        return { res, data };
    }

    function bestPreviewUrl(card) {
        return (card.dataset.previewUrl || "").trim() || (card.dataset.signedUrl || "").trim() || "";
    }

    function setZoom(percent) {
        const p = Math.min(600, Math.max(20, Math.round(percent)));
        if (zoomEl) zoomEl.value = String(p);
        if (zoomLabelEl) zoomLabelEl.textContent = `${p}%`;
        if (imgEl) imgEl.style.transform = `scale(${p / 100})`;
    }

    function fitZoom() {
        if (!selectedCard) return;
        const url = bestPreviewUrl(selectedCard);
        if (!url) return;

        const sw = stageEl?.clientWidth || 1;
        const sh = stageEl?.clientHeight || 1;

        const nw = imgEl?.naturalWidth || 1;
        const nh = imgEl?.naturalHeight || 1;

        const scale = Math.min(sw / nw, sh / nh);
        setZoom(Math.round(scale * 100));

        if (stageEl) {
            stageEl.scrollLeft = 0;
            stageEl.scrollTop = 0;
        }
    }

    // ✅ CSS가 .dg-card[open] > .dg-body { display:block } 를 전담하므로
    //    과거 JS가 남긴 hidden/display 흔적만 제거해준다.
    function cleanupBody(card) {
        const b = $(".dg-body", card);
        if (!b) return;
        b.removeAttribute("hidden");
        b.style.removeProperty("display");
    }

    function selectCard(card, open = true) {
        if (!card) return;

        // 선택 표시
        cards().forEach((c) => c.classList.remove("is-selected"));
        card.classList.add("is-selected");

        // 다른 카드 닫기 (open만 제어)
        cards().forEach((c) => {
            if (c === card) return;
            c.open = false;
            c.classList.remove("is-open");
            cleanupBody(c);
        });

        // 선택 카드 열기/닫기 (open만 제어)
        card.open = !!open;
        card.classList.toggle("is-open", !!open);
        cleanupBody(card);

        selectedCard = card;

        // 스크롤 보정
        try {
            card.scrollIntoView({ block: "nearest", inline: "nearest" });
        } catch { /* no-op */ }

        // preview
        const url = bestPreviewUrl(card);
        if (!url) {
            if (imgEl) imgEl.removeAttribute("src");
            if (emptyEl) emptyEl.hidden = false;
        } else {
            if (emptyEl) emptyEl.hidden = true;
            if (imgEl) imgEl.src = url;
        }

        // preview links
        if (pNewTab) {
            if ((card.dataset.previewUrl || "").trim()) {
                pNewTab.hidden = false;
                pNewTab.href = card.dataset.previewUrl;
            } else {
                pNewTab.hidden = true;
            }
        }
        if (pOrigTab) {
            if ((card.dataset.signedUrl || "").trim()) {
                pOrigTab.hidden = false;
                pOrigTab.href = card.dataset.signedUrl;
            } else {
                pOrigTab.hidden = true;
            }
        }

        // 기본 줌
        setZoom(Number(zoomEl?.value || "100"));
    }

    function nextCard(dir = 1) {
        const list = cards().filter((c) => !c.hasAttribute("hidden"));
        if (!list.length) return null;

        const idx = selectedCard ? list.indexOf(selectedCard) : -1;
        const nextIdx = idx < 0 ? 0 : Math.min(list.length - 1, Math.max(0, idx + dir));
        return list[nextIdx] || null;
    }

    function cardStatusEl(card) {
        return $(".dg-status", card) || null;
    }

    function readApprovePayload(card) {
        const caption = ($('input[name="caption"]', card)?.value || "").trim();
        const tags = ($('input[name="tags"]', card)?.value || "").trim();
        return {
            pending_id: card.dataset.pendingId,
            caption,
            tags,
        };
    }

    function readRejectPayload(card) {
        const reason = ($('textarea[name="reason"]', card)?.value || "").trim();
        const mode = ($(".dg-reject-modes input[type=radio]:checked", card)?.value || "reject_only").trim();

        const payload = {
            pending_id: card.dataset.pendingId,
            reason,
            reject_mode: mode,
        };

        if (mode === "restrict_all") {
            payload.restrict_days = ($('select[name="restrict_days"]', card)?.value || "7").trim();
            payload.delete_blob = $('input[name="delete_blob"]', card)?.checked ? 1 : 0;
            payload.actor_key = (card.dataset.actorKey || "").trim();
        }
        return payload;
    }

    async function doApprove(card) {
        if (!approveUrl) return showToast("approve_url 없음");
        const st = cardStatusEl(card);
        if (st) st.textContent = "승인 처리 중…";

        const payload = readApprovePayload(card);
        const { res, data } = await postJson(approveUrl, payload);

        if (!res.ok || (data && data.ok === false)) {
            if (st) st.textContent = "실패";
            showToast("승인 실패");
            return;
        }

        if (st) st.textContent = "승인 완료";
        showToast("승인 완료");
        card.remove();
        decCount();

        const nxt = nextCard(+1) || nextCard(-1);
        if (nxt) selectCard(nxt, true);

        if (autoReloadEl?.checked) location.reload();
    }

    async function doReject(card) {
        if (!rejectUrl) return showToast("reject_url 없음");
        const st = cardStatusEl(card);
        if (st) st.textContent = "거절 처리 중…";

        const payload = readRejectPayload(card);

        if (payload.reject_mode === "restrict_all") {
            const days = payload.restrict_days;
            const del = payload.delete_blob ? " + 파일삭제" : "";
            const ok = window.confirm(`정말 제한(${days})${del}까지 적용할까요?`);
            if (!ok) {
                if (st) st.textContent = "취소됨";
                return;
            }
        }

        const { res, data } = await postJson(rejectUrl, payload);

        if (!res.ok || (data && data.ok === false)) {
            if (st) st.textContent = "실패";
            showToast("거절 실패");
            return;
        }

        if (st) st.textContent = "거절 완료";
        showToast("거절 완료");
        card.remove();
        decCount();

        const nxt = nextCard(+1) || nextCard(-1);
        if (nxt) selectCard(nxt, true);

        if (autoReloadEl?.checked) location.reload();
    }

    async function doLift(card) {
        if (!liftUrl) return showToast("lift_url 없음");
        const actor = (card.dataset.actorKey || "").trim();
        if (!actor) return showToast("actor_key 없음");

        const st = cardStatusEl(card);
        if (st) st.textContent = "선처 처리 중…";

        const { res, data } = await postJson(liftUrl, { actor_key: actor });

        if (!res.ok || (data && data.ok === false)) {
            if (st) st.textContent = "실패";
            showToast("선처 실패");
            return;
        }

        if (st) st.textContent = "선처 완료";
        showToast("선처 완료");
    }

    async function copyText(text) {
        const t = String(text || "").trim();
        if (!t) return;
        try {
            await navigator.clipboard.writeText(t);
            showToast("복사됨");
        } catch {
            showToast("복사 실패");
        }
    }

    // ===== Reason helpers =====
    function appendReason(card) {
        const preset = ($('select[name="reason_preset"]', card)?.value || "").trim();
        const custom = ($('input[name="reason_custom"]', card)?.value || "").trim();
        const add = custom || preset;
        if (!add) return;

        const ta = $('textarea[name="reason"]', card);
        if (!ta) return;
        ta.value = ta.value ? `${ta.value}\n- ${add}` : `- ${add}`;

        const customInp = $('input[name="reason_custom"]', card);
        if (customInp) customInp.value = "";
    }

    function clearReason(card) {
        const ta = $('textarea[name="reason"]', card);
        if (ta) ta.value = "";
        const customInp = $('input[name="reason_custom"]', card);
        if (customInp) customInp.value = "";
        const presetSel = $('select[name="reason_preset"]', card);
        if (presetSel) presetSel.value = "";
    }

    // ===== UI wiring =====
    grid.addEventListener("click", (e) => {
        const card = e.target.closest(".dg-card");
        const actionBtn = e.target.closest("[data-action]");

        // data-copy
        const copyEl = e.target.closest("[data-copy]");
        if (copyEl) {
            e.preventDefault();
            if (card) ensureOpen(card);
            return void copyText(copyEl.getAttribute("data-copy") || "");
        }

        // summary 클릭 처리: 기본 <details> 토글은 항상 막고, 우리가 직접 제어
        const summary = e.target.closest("summary, .dg-summary");
        if (card && summary) {
            e.preventDefault();

            const interactiveInSummary = e.target.closest("button, a, input, textarea, select, label");

            if (!interactiveInSummary) {
                const willOpen = !(card.open === true);
                selectCard(card, willOpen);
                return;
            }

            ensureOpen(card);

            // summary 안 일반 링크는 수동 새탭 (원하면 유지)
            const a = e.target.closest("a[href]");
            if (a && !a.closest("[data-action]") && !a.closest("[data-copy]")) {
                window.open(a.href, a.target || "_blank", "noreferrer");
                return;
            }
            // 아래 action 처리로 계속 진행
        }

        // summary 밖 빈공간 클릭 → 열기
        if (card && !actionBtn) {
            const interactive = e.target.closest("button, a, input, textarea, select, label");
            if (!interactive) {
                selectCard(card, true);
                return;
            }
        }

        if (!actionBtn) return;

        const action = actionBtn.dataset.action || "";

        // 버튼 누르면 무조건 카드 열기
        if (card) ensureOpen(card);

        // preview 헤더 copy
        if (action === "copy-id") return void copyText(selectedCard?.dataset.pendingId || "");
        if (action === "copy-key") return void copyText(selectedCard?.dataset.storageKey || "");

        // 카드 내부 액션
        if (card) {
            if (action === "approve") return void doApprove(card);
            if (action === "reject") return void doReject(card);
            if (action === "lift") return void doLift(card);

            if (action === "reason-append") return void appendReason(card);
            if (action === "reason-clear") return void clearReason(card);

            if (action === "actor-open") {
                const actor = (card.dataset.actorKey || "").trim();
                if (!actor || !penaltiesUrl) return;
                const url = `${penaltiesUrl}?actor_key=${encodeURIComponent(actor)}`;
                window.open(url, "_blank", "noreferrer");
                return;
            }

            if (action === "copy-id") return void copyText(card.dataset.pendingId || "");
            if (action === "copy-key") return void copyText(card.dataset.storageKey || "");
        }
    });

    document.addEventListener("click", (e) => {
        if (e.target.closest("#dgGrid")) return;

        const actionBtn = e.target.closest("[data-action]");
        if (!actionBtn) return;

        const action = actionBtn.dataset.action || "";

        if (action === "copy-id") return void copyText(selectedCard?.dataset.pendingId || "");
        if (action === "copy-key") return void copyText(selectedCard?.dataset.storageKey || "");

        if (action === "zoom-in") return void setZoom(Number(zoomEl?.value || "100") + 10);
        if (action === "zoom-out") return void setZoom(Number(zoomEl?.value || "100") - 10);
        if (action === "zoom-100") return void setZoom(100);
        if (action === "zoom-reset") {
            setZoom(100);
            if (stageEl) { stageEl.scrollLeft = 0; stageEl.scrollTop = 0; }
            return;
        }
        if (action === "zoom-fit") return void fitZoom();
        if (action === "bg-toggle") {
            if (!stageEl) return;
            stageEl.dataset.bg = stageEl.dataset.bg === "checker" ? "solid" : "checker";
        }
    });

    // zoom slider
    zoomEl?.addEventListener("input", () => setZoom(Number(zoomEl.value || "100")));

    // wheel = zoom
    stageEl?.addEventListener("wheel", (e) => {
        e.preventDefault();
        const cur = Number(zoomEl?.value || "100");
        const delta = e.deltaY > 0 ? -8 : +8;
        setZoom(cur + delta);
    }, { passive: false });

    // drag to pan
    let pan = { on: false, x: 0, y: 0, sl: 0, st: 0 };
    stageEl?.addEventListener("pointerdown", (e) => {
        pan.on = true;
        pan.x = e.clientX;
        pan.y = e.clientY;
        pan.sl = stageEl.scrollLeft;
        pan.st = stageEl.scrollTop;
        stageEl.setPointerCapture(e.pointerId);
    });
    stageEl?.addEventListener("pointermove", (e) => {
        if (!pan.on) return;
        const dx = e.clientX - pan.x;
        const dy = e.clientY - pan.y;
        stageEl.scrollLeft = pan.sl - dx;
        stageEl.scrollTop = pan.st - dy;
    });
    stageEl?.addEventListener("pointerup", () => { pan.on = false; });
    stageEl?.addEventListener("pointercancel", () => { pan.on = false; });

    // double click = reset
    stageEl?.addEventListener("dblclick", () => {
        setZoom(100);
        if (stageEl) { stageEl.scrollLeft = 0; stageEl.scrollTop = 0; }
    });

    // filter
    filterEl?.addEventListener("input", () => {
        const q = (filterEl.value || "").trim().toLowerCase();
        let visible = 0;

        cards().forEach((c) => {
            const hay = [
                c.dataset.pendingId,
                c.dataset.actorKey,
                c.dataset.origName,
                c.dataset.storageKey,
            ].join(" ").toLowerCase();

            const ok = !q || hay.includes(q);
            if (!ok) c.setAttribute("hidden", "");
            else { c.removeAttribute("hidden"); visible++; }
        });

        if (selectedCard && selectedCard.hasAttribute("hidden")) {
            const nxt = nextCard(+1) || nextCard(-1);
            if (nxt) selectCard(nxt, true);
            else selectedCard = null;
        }

        showToast(`표시: ${visible}건`, 900);
    });

    // collapse all
    collapseAllEl?.addEventListener("click", () => {
        cards().forEach((c) => {
            c.open = false;
            c.classList.remove("is-open");
            cleanupBody(c);
        });
        if (selectedCard) {
            selectedCard.open = false;
            selectedCard.classList.remove("is-open");
        }
        showToast("모두 접기");
    });

    reloadBtn?.addEventListener("click", () => location.reload());

    // keyboard
    document.addEventListener("keydown", (e) => {
        if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;

        if (e.key === "j" || e.key === "J") {
            const nxt = nextCard(+1);
            if (nxt) selectCard(nxt, true);
        } else if (e.key === "k" || e.key === "K") {
            const prv = nextCard(-1);
            if (prv) selectCard(prv, true);
        } else if (e.key === "Escape") {
            if (selectedCard) selectCard(selectedCard, false);
        } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            if (selectedCard) doApprove(selectedCard);
        }
    });

    // 최초 1개 자동 선택
    const first = cards().find((c) => !c.hasAttribute("hidden"));
    if (first) selectCard(first, true);

    // 이미지 로드 후 초기 줌 안정화
    imgEl?.addEventListener("load", () => {
        if (!imgEl.dataset._inited) {
            imgEl.dataset._inited = "1";
            setZoom(Number(zoomEl?.value || "100"));
        }
    });

})();
