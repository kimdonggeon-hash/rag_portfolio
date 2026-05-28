(function () {
    "use strict";

    function $(sel, root) { return (root || document).querySelector(sel); }
    function esc(s) {
        return String(s || "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }
    function getCookie(name) {
        const parts = document.cookie.split(";").map(s => s.trim());
        for (const it of parts) {
            if (it.startsWith(name + "=")) return decodeURIComponent(it.slice(name.length + 1));
        }
        return "";
    }

    const listUrl = $("#dgListUrl")?.value || "";
    const liftUrl = $("#dgLiftUrl")?.value || "";
    const actorEl = $("#dgActor");
    const reasonEl = $("#dgReason");
    const rowsEl = $("#dgRows");
    const countEl = $("#dgCount");

    function toast(msg, kind) {
        const el = $("#dgToast");
        if (!el) return;
        el.hidden = false;
        el.textContent = msg;
        el.classList.remove("ok", "err");
        el.classList.add(kind || "ok");
        clearTimeout(el._t);
        el._t = setTimeout(() => { el.hidden = true; }, 2200);
    }

    function setRowsHtml(html) {
        if (!rowsEl) return;
        rowsEl.innerHTML = html;
    }

    function setCount(n) {
        if (countEl) countEl.textContent = String(n || 0);
    }

    function buildUrl(base, params) {
        const u = new URL(base, window.location.origin);
        Object.entries(params || {}).forEach(([k, v]) => {
            if (v === undefined || v === null) return;
            const sv = String(v).trim();
            if (!sv) return;
            u.searchParams.set(k, sv);
        });
        return u.toString();
    }

    async function fetchJson(url) {
        const res = await fetch(url, { credentials: "same-origin" });
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
    }

    async function postJson(url, payload) {
        const csrftoken = getCookie("csrftoken");
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrftoken },
            credentials: "same-origin",
            body: JSON.stringify(payload || {})
        });
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
    }

    function render(items) {
        const list = Array.isArray(items) ? items : [];
        setCount(list.length);

        if (!list.length) {
            setRowsHtml(`<tr class="dg-tr-empty"><td colspan="6">활성 제재가 없습니다.</td></tr>`);
            return;
        }

        const html = list.map(it => {
            const actor = esc(it.actor_key || "");
            const kind = esc(it.kind || "");
            const until = esc(it.until || (kind === "permaban" ? "permanent" : ""));
            const reason = esc(it.reason || "");
            const created = esc(it.created_at || "");
            const id = esc(it.id || "");

            return `
        <tr data-actor="${actor}">
          <td class="mono">
            <button class="dg-link-btn" type="button" data-action="filter">${actor || "-"}</button>
          </td>
          <td>${kind || "-"}</td>
          <td class="mono small">${until || "-"}</td>
          <td>${reason || "-"}</td>
          <td class="mono small">${created || "-"}</td>
          <td>
            <button class="dg-btn xs danger" type="button" data-action="lift" data-actor="${actor}">해제</button>
          </td>
        </tr>
      `;
        }).join("");

        setRowsHtml(html);
    }

    async function loadList(actorKey) {
        if (!listUrl) {
            toast("list API 없음", "err");
            return;
        }
        setRowsHtml(`<tr class="dg-tr-empty"><td colspan="6">로딩 중…</td></tr>`);

        const url = buildUrl(listUrl, { actor_key: actorKey || "" });
        const { ok, data } = await fetchJson(url);

        if (!ok || !data || !data.ok) {
            toast("목록 로드 실패", "err");
            setRowsHtml(`<tr class="dg-tr-empty"><td colspan="6">목록 로드 실패: ${esc(data?.error || "unknown")}</td></tr>`);
            return;
        }

        render(data.items || []);
    }

    async function lift(actorKey) {
        const ak = String(actorKey || "").trim();
        if (!ak) {
            toast("actor_key 없음", "err");
            return;
        }
        if (!liftUrl) {
            toast("lift API 없음", "err");
            return;
        }

        const reason = String(reasonEl?.value || "").trim() || "선처(관리 화면)";
        const okGo = confirm(`선처로 정지/제재를 해제할까요?\nactor_key=${ak}`);
        if (!okGo) return;

        const { ok, data } = await postJson(liftUrl, { actor_key: ak, reason });

        if (!ok || !data || !data.ok) {
            toast("선처 실패", "err");
            return;
        }

        toast("선처 완료", "ok");
        // 성공 후 목록 갱신
        await loadList(actorEl?.value || "");
    }

    function initFromQuery() {
        const qs = new URLSearchParams(location.search);
        const ak = (qs.get("actor_key") || "").trim();
        if (ak && actorEl) actorEl.value = ak;
        return ak;
    }

    // --- events ---
    $("#dgSearchBtn")?.addEventListener("click", () => loadList(actorEl?.value || ""));
    $("#dgAllBtn")?.addEventListener("click", () => { if (actorEl) actorEl.value = ""; loadList(""); });
    $("#dgRefreshBtn")?.addEventListener("click", () => loadList(actorEl?.value || ""));
    $("#dgLiftBtn")?.addEventListener("click", () => lift(actorEl?.value || ""));

    actorEl?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            loadList(actorEl.value || "");
        }
    });

    rowsEl?.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;

        const act = btn.dataset.action;
        if (act === "filter") {
            const tr = btn.closest("tr");
            const ak = (tr?.dataset.actor || "").trim();
            if (actorEl) actorEl.value = ak;
            loadList(ak);
            return;
        }
        if (act === "lift") {
            const ak = (btn.dataset.actor || "").trim();
            lift(ak);
            return;
        }
    });

    // 첫 로드: actor_key 있으면 그걸로, 없으면 전체
    const initAk = initFromQuery();
    loadList(initAk || "");
})();
