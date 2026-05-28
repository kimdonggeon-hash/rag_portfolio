/* ragapp/static/ragapp/javascript/media_upload.js */
(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);

    const form = $("miForm");
    const input = $("miInput");
    const drop = $("miDrop");
    const prev = $("miPreview");
    const capEl = $("miCaption");
    const tagEl = $("miTags");
    const btn = $("miSubmit");
    const busy = $("miBusy");

    // 필수 요소 없으면 종료
    if (!form || !input || !drop || !prev || !btn) return;

    // (옵션) 템플릿에서 data-max-files / data-max-mb를 줄 수도 있음
    const maxFiles = parseInt(form.dataset.maxFiles || "10", 10) || 10;
    const maxMB = parseFloat(form.dataset.maxMb || "15") || 15;

    // ----------------------------
    // ✅ 제출(연타) 방지 + 친절 UX
    // ----------------------------
    let isSubmitting = false;

    // 버튼 원래 텍스트를 보존(나중에 복구할 일 있으면 사용)
    const originalBtnText = (btn.textContent || "").trim() || "업로드 접수";

    // 버튼에 data-busy-text="접수 중…" 같은 걸 넣어두면 그걸 사용
    const busyText = (btn.dataset.busyText || "접수 중…").trim();

    function setSubmitting(on) {
        isSubmitting = !!on;

        // 폼 전체에 상태 클래스(필요하면 CSS로도 제어 가능)
        form.classList.toggle("mi-is-submitting", isSubmitting);

        if (isSubmitting) {
            // 버튼 잠금 + 문구 변경
            btn.disabled = true;
            btn.textContent = busyText;

            // 안내 문구 표시(있으면)
            if (busy) busy.hidden = false;

            // 업로드 중에는 파일/드롭 비활성(중복 업로드 체감 방지)
            drop.style.pointerEvents = "none";
            drop.setAttribute("aria-disabled", "true");
        } else {
            // (원하면 폴백용) 복구
            btn.disabled = false;
            btn.textContent = originalBtnText;

            if (busy) busy.hidden = true;

            input.disabled = false;
            drop.style.pointerEvents = "";
            drop.removeAttribute("aria-disabled");
        }
    }

    // 새로고침/뒤로가기(bfcache)로 돌아왔는데 버튼이 잠긴 상태면 풀어줌
    window.addEventListener("pageshow", () => {
        if (btn.disabled) setSubmitting(false);
    });

    // ----------------------------
    // 미리보기(기존 유지)
    // ----------------------------
    function safeText(s) {
        return (s || "")
            .toString()
            .replace(/[&<>"']/g, (m) => ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            }[m]));
    }

    function renderPreview(files) {
        prev.innerHTML = "";
        const slice = Array.prototype.slice.call(files || [], 0, maxFiles);

        const caption = capEl ? (capEl.value || "").trim() : "";
        const tags = tagEl ? (tagEl.value || "").trim() : "";

        slice.forEach((file) => {
            const url = URL.createObjectURL(file);
            const item = document.createElement("div");
            item.className = "mi-item";

            const meta2 = [];
            if (caption) meta2.push(`<div class="mi-tag">📝 ${safeText(caption)}</div>`);
            if (tags) meta2.push(`<div class="mi-tag">🏷️ ${safeText(tags)}</div>`);

            item.innerHTML = `
                <img class="mi-thumb" src="${url}" alt="">
                <div class="mi-meta">
                  <div class="mi-cap">${safeText(file.name)}</div>
                  <div class="mi-tag">${(file.size / 1024 / 1024).toFixed(2)} MB</div>
                  ${meta2.join("")}
                </div>`;
            prev.appendChild(item);
        });
    }

    input.addEventListener("change", () => renderPreview(input.files));
    if (capEl) capEl.addEventListener("input", () => renderPreview(input.files));
    if (tagEl) tagEl.addEventListener("input", () => renderPreview(input.files));

    // ----------------------------
    // 드래그 앤 드롭(기존 유지 + 제출중 방지)
    // ----------------------------
    ["dragenter", "dragover"].forEach((ev) => {
        drop.addEventListener(ev, (e) => {
            if (isSubmitting) return;
            e.preventDefault();
            e.stopPropagation();
            drop.classList.add("is-drag");
        });
    });

    ["dragleave", "drop"].forEach((ev) => {
        drop.addEventListener(ev, (e) => {
            e.preventDefault();
            e.stopPropagation();
            drop.classList.remove("is-drag");
        });
    });

    drop.addEventListener("drop", (e) => {
        if (isSubmitting) return;

        const files = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
            .filter((f) => (f.type || "").startsWith("image/"));

        if (!files.length) return;

        const filtered = files
            .slice(0, maxFiles)
            .filter((f) => (f.size / 1024 / 1024) <= maxMB);

        const dt = new DataTransfer();
        filtered.forEach((f) => dt.items.add(f));
        input.files = dt.files;

        renderPreview(input.files);
    });

    // ----------------------------
    // ✅ 연타/중복 제출 방지 (버튼/엔터 둘 다 커버)
    // ----------------------------
    form.addEventListener("submit", (e) => {
        // 이미 제출중이면 무조건 차단
        if (isSubmitting) {
            e.preventDefault();
            e.stopPropagation();
            return;
        }

        // 파일이 없으면 잠그지 않음(브라우저 required가 막거나 서버가 에러)
        if (!input.files || input.files.length === 0) {
            return;
        }

        // 여기서부터 “확실히 눌린 티”를 즉시 냄
        setSubmitting(true);
        // 이후 네트워크 전송은 브라우저가 진행
    });

    // 제출 중 Enter 연타 방지(폼 내부에서 엔터로 submit 유발되는 것 차단)
    form.addEventListener("keydown", (e) => {
        if (!isSubmitting) return;
        if (e.key === "Enter") {
            e.preventDefault();
            e.stopPropagation();
        }
    });
})();
