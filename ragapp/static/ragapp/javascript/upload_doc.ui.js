// ragapp/static/ragapp/javascript/upload_doc.ui.js
// RAG Admin - Upload UI (drag & drop, chips, summary, basic validation)
// 기대 요소 id/name:
//  - form#uform
//  - input[type=file]#files
//  - #drop (드롭존 label)
//  - #chips, #sum, #statusHint, #clearBtn, #submitBtn, #spin
//  - textarea[name="rawtext"]

(function () {
    'use strict';

    // ✅ 중복 초기화 방지 (실수로 인라인/다른 스크립트와 함께 로드되어도 1회만)
    if (window.__UPLOAD_DOC_UI_INIT__) return;
    window.__UPLOAD_DOC_UI_INIT__ = true;

    const input = document.getElementById('files');
    const drop = document.getElementById('drop');
    const chips = document.getElementById('chips');
    const sumEl = document.getElementById('sum');
    const clearBtn = document.getElementById('clearBtn');
    const submitBtn = document.getElementById('submitBtn');
    const form = document.getElementById('uform');
    const ta = document.querySelector('textarea[name="rawtext"]');
    const status = document.getElementById('statusHint');
    const spinner = document.getElementById('spin');

    if (!form || !input) return;

    function fmt(bytes) {
        if (!bytes) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0, n = bytes;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return (Math.round(n * 10) / 10) + ' ' + u[i];
    }

    function isAllowedFile(f) {
        const name = (f && f.name ? f.name : '').toLowerCase();
        return name.endsWith('.pdf') || name.endsWith('.txt');
    }

    function hasPayload() {
        const files = input.files ? input.files : [];
        const hasFiles = files && files.length > 0;
        const hasText = ta && (ta.value || '').trim().length > 0;
        return hasFiles || hasText;
    }

    function refreshSummary() {
        try {
            const files = input.files ? input.files : [];
            let total = 0;
            for (const f of files) total += f.size || 0;

            if (sumEl) sumEl.textContent = `선택된 파일 ${files.length}개 · ${fmt(total)}`;
            if (submitBtn) submitBtn.disabled = !hasPayload();
        } catch (_) { /* noop */ }
    }

    function rebuildChips() {
        if (!chips) { refreshSummary(); return; }

        chips.innerHTML = '';
        const files = input.files ? Array.from(input.files) : [];

        files.forEach((f, idx) => {
            const wrap = document.createElement('span');
            wrap.className = 'chip';
            wrap.title = `${f.name} (${fmt(f.size || 0)})`;

            const name = document.createElement('span');
            name.textContent = f.name;

            const del = document.createElement('button');
            del.type = 'button';
            del.className = 'chip-del';
            del.dataset.i = String(idx);
            del.setAttribute('aria-label', `${f.name} 제거`);
            del.textContent = '×';

            // 버튼이 CSS 없어서 깨지지 않도록 최소 인라인 스타일
            del.style.marginLeft = '6px';
            del.style.border = '0';
            del.style.background = 'transparent';
            del.style.cursor = 'pointer';
            del.style.color = 'inherit';
            del.style.fontWeight = '900';
            del.style.lineHeight = '1';

            wrap.appendChild(name);
            wrap.appendChild(del);
            chips.appendChild(wrap);
        });

        refreshSummary();
    }

    function setFiles(fileList) {
        input.value = ''; // ✅ (강추 1줄) 같은 파일 재선택 시 change 미발화 케이스 방지

        const arr = Array.from(fileList || []).filter(isAllowedFile);

        if (fileList && fileList.length && arr.length === 0) {
            if (status) status.textContent = 'PDF/TXT 파일만 업로드할 수 있어요.';
            return;
        }

        const dt = new DataTransfer();
        arr.forEach(f => dt.items.add(f));
        input.files = dt.files;

        // change 이벤트로 다른 로직도 확실히 갱신되게
        input.dispatchEvent(new Event('change', { bubbles: true }));

        if (status) status.textContent = '';
    }

    // chip 삭제
    chips && chips.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest ? e.target.closest('.chip-del') : null;
        if (!btn) return;

        const i = Number(btn.dataset.i || '-1');
        if (i < 0) return;

        const files = Array.from(input.files || []);
        files.splice(i, 1);
        setFiles(files);
    });

    // 드래그 & 드롭
    if (drop) {
        ['dragenter', 'dragover'].forEach(ev => {
            drop.addEventListener(ev, (e) => {
                e.preventDefault();
                e.stopPropagation();
                drop.classList.add('is-over'); // 템플릿 CSS: .drop.is-over
            });
        });

        ['dragleave', 'drop'].forEach(ev => {
            drop.addEventListener(ev, (e) => {
                e.preventDefault();
                e.stopPropagation();
                drop.classList.remove('is-over');
            });
        });

        drop.addEventListener('drop', (e) => {
            const fs = e.dataTransfer && e.dataTransfer.files ? e.dataTransfer.files : null;
            if (fs && fs.length) setFiles(fs);
        });
    }

    // input / textarea 이벤트
    input.addEventListener('change', rebuildChips);
    ta && ta.addEventListener('input', refreshSummary);

    // clear
    clearBtn && clearBtn.addEventListener('click', () => {
        input.value = '';
        setFiles([]); // 파일 리스트까지 확실히 비우기
        if (ta) ta.value = '';
        refreshSummary();
        if (status) status.textContent = '';
        if (spinner) spinner.classList.add('hidden');
    });

    // Enter로 실수 제출 방지(텍스트영역 제외)
    form.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target && e.target.tagName !== 'TEXTAREA') {
            if (!hasPayload()) e.preventDefault();
        }
    });

    // 제출 UX
    form.addEventListener('submit', (e) => {
        if (!hasPayload()) {
            e.preventDefault();
            e.stopPropagation();
            status && (status.textContent = '파일을 선택하거나 텍스트를 입력해 주세요.');
            return;
        }
        if (submitBtn) submitBtn.disabled = true;
        if (spinner) spinner.classList.remove('hidden');
        if (status) status.textContent = '업로드/인덱싱 중…';
    });

    // 초기 렌더
    rebuildChips();

    // 결과 섹션 자동 스크롤
    const rs = document.getElementById('results-ssr') || document.getElementById('results');
    if (rs) rs.scrollIntoView({ behavior: 'smooth', block: 'start' });
})();
