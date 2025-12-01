// ragapp/static/ragapp/javascript/upload_doc.ui.js
// RAG Admin - Upload UI (drag & drop, chips, summary, basic validation)
// 기대 요소 id/name:
//  - form#uform
//  - input[type=file]#files (또는 #file)
//  - label/#drop (드롭존)
//  - #chips, #sum, #statusHint, #clearBtn, #submitBtn, #spin
//  - textarea[name="rawtext"]

(function () {
    'use strict';

    const $ = (s, r = document) => r.querySelector(s);

    const input = document.getElementById('files') || document.getElementById('file');
    const drop = $('#drop');
    const chips = $('#chips');
    const sumEl = $('#sum');
    const clearBtn = $('#clearBtn');
    const submitBtn = $('#submitBtn');
    const form = $('#uform');
    const ta = document.querySelector('textarea[name="rawtext"]');
    const status = $('#statusHint');
    const spinner = document.getElementById('spin');

    function fmt(bytes) {
        if (!bytes) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0, n = bytes;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return (Math.round(n * 10) / 10) + ' ' + u[i];
    }

    function hasPayload() {
        const files = (input && input.files) ? input.files : [];
        const hasText = ta && (ta.value || '').trim().length > 0;
        return (files && files.length) || hasText;
    }

    function refreshSummary() {
        try {
            const files = input && input.files ? input.files : [];
            let total = 0;
            for (const f of files) total += f.size || 0;
            if (sumEl) sumEl.textContent = `선택된 파일 ${files.length}개 · ${fmt(total)}`;
            if (submitBtn) submitBtn.disabled = !hasPayload();
        } catch (_) { /* noop */ }
    }

    function rebuildChips() {
        if (!chips) return;
        chips.innerHTML = '';
        const files = (input && input.files) ? input.files : [];
        Array.from(files).forEach((f, idx) => {
            const el = document.createElement('div');
            el.className = 'dg-chip';
            el.innerHTML = `
        <span class="name" title="${f.name}">${f.name}</span>
        <span class="meta">${fmt(f.size || 0)}</span>
        <button type="button" class="del" aria-label="${f.name} 제거" data-i="${idx}">×</button>
      `;
            chips.appendChild(el);
        });
        refreshSummary();
    }

    function setFiles(fileList) {
        const dt = new DataTransfer();
        Array.from(fileList).forEach(f => dt.items.add(f));
        if (input) input.files = dt.files;
        rebuildChips();
    }

    // chip 삭제
    chips && chips.addEventListener('click', (e) => {
        const btn = e.target.closest('.del'); if (!btn) return;
        const i = +btn.dataset.i;
        const files = Array.from(input.files || []);
        files.splice(i, 1);
        setFiles(files);
    });

    // 드래그 & 드롭
    if (drop) {
        ['dragenter', 'dragover'].forEach(ev => {
            drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('dg-hover'); });
        });
        ['dragleave', 'drop'].forEach(ev => {
            drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('dg-hover'); });
        });

        // drop이 <label>이면 기본 클릭 동작 사용(파일 선택창 자동 표시). 별도 click 주입 안 함.
        if (drop.tagName !== 'LABEL') {
            drop.addEventListener('click', () => input && input.click());
        }

        drop.addEventListener('drop', (e) => {
            const fs = (e.dataTransfer && e.dataTransfer.files) ? e.dataTransfer.files : null;
            if (fs && fs.length) setFiles(fs);
        });
    }

    input && input.addEventListener('change', rebuildChips);
    ta && ta.addEventListener('input', refreshSummary);

    clearBtn && clearBtn.addEventListener('click', () => {
        if (input) { input.value = ''; setFiles([]); }
        if (ta) ta.value = '';
        refreshSummary();
        if (status) status.textContent = '';
    });

    // Enter로 실수 제출 방지(텍스트영역 제외)
    form && form.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target && e.target.tagName !== 'TEXTAREA') {
            if (!hasPayload()) e.preventDefault();
        }
    });

    // 제출 UX (일반 폼 제출)
    form && form.addEventListener('submit', (e) => {
        if (!hasPayload()) {
            e.preventDefault();
            e.stopPropagation();
            status && (status.textContent = '파일을 선택하거나 텍스트를 입력해 주세요.');
            submitBtn && submitBtn.classList.add('dg-shake');
            setTimeout(() => submitBtn && submitBtn.classList.remove('dg-shake'), 450);
            return;
        }
        // 제출 중 상태 표시 (페이지 전환 전까지)
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.classList.add('busy');
        }
        if (spinner) spinner.hidden = false;
        if (status) status.textContent = '업로드/인덱싱 중...';
        // 이후 서버 응답으로 새 페이지 렌더
    });

    // 초기 렌더 후 상태 세팅
    rebuildChips();

    // 결과 섹션이 있으면 자동 스크롤
    const rs = document.getElementById('results');
    if (rs) rs.scrollIntoView({ behavior: 'smooth', block: 'start' });
})();
