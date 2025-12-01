// (function () {
//     const $ = (s, r = document) => r.querySelector(s);

//     const input = document.getElementById('files') || document.getElementById('file');
//     const drop = $('#drop');
//     const chips = $('#chips');
//     const sumEl = $('#sum');
//     const clearBtn = $('#clearBtn');
//     const submitBtn = $('#submitBtn');
//     const form = $('#uform');
//     const ta = document.querySelector('textarea[name="rawtext"]');
//     const status = $('#statusHint');

//     function fmt(b) {
//         if (!b) return '0 B';
//         const u = ['B', 'KB', 'MB', 'GB']; let i = 0, n = b;
//         while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
//         return (Math.round(n * 10) / 10) + ' ' + u[i];
//     }

//     function hasPayload() {
//         const files = (input && input.files) ? input.files : [];
//         const hasText = ta && (ta.value || '').trim().length > 0;
//         return (files && files.length) || hasText;
//     }

//     function refreshSummary() {
//         try {
//             const files = input && input.files ? input.files : [];
//             let total = 0;
//             for (const f of files) total += f.size || 0;
//             if (sumEl) sumEl.textContent = `선택된 파일 ${files.length}개 · ${fmt(total)}`;
//             if (submitBtn) submitBtn.disabled = !hasPayload();
//         } catch (_) { }
//     }

//     function rebuildChips() {
//         if (!chips) return;
//         chips.innerHTML = '';
//         const files = (input && input.files) ? input.files : [];
//         [...files].forEach((f, idx) => {
//             const el = document.createElement('div');
//             el.className = 'dg-chip';
//             el.innerHTML = `
//         <span class="name" title="${f.name}">${f.name}</span>
//         <span class="meta">${fmt(f.size || 0)}</span>
//         <button type="button" class="del" aria-label="${f.name} 제거" data-i="${idx}">×</button>
//       `;
//             chips.appendChild(el);
//         });
//         refreshSummary();
//     }

//     function setFiles(fileList) {
//         const dt = new DataTransfer();
//         [...fileList].forEach(f => dt.items.add(f));
//         if (input) input.files = dt.files;
//         rebuildChips();
//     }

//     // chip 삭제
//     chips && chips.addEventListener('click', (e) => {
//         const btn = e.target.closest('.del'); if (!btn) return;
//         const i = +btn.dataset.i;
//         const files = [...(input.files || [])];
//         files.splice(i, 1);
//         setFiles(files);
//     });

//     // drop UX
//     if (drop) {
//         ['dragenter', 'dragover'].forEach(ev => {
//             drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('dg-hover'); });
//         });
//         ['dragleave', 'drop'].forEach(ev => {
//             drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('dg-hover'); });
//         });

//         // ✅ FIX: drop이 <label>이면 기본 클릭 동작만 사용(파일창이 자동으로 뜸).
//         //       따라서 프로그램matic input.click()을 "하지 않는다".
//         if (drop.tagName !== 'LABEL') {
//             drop.addEventListener('click', () => input && input.click());
//         }

//         drop.addEventListener('drop', (e) => {
//             const fs = (e.dataTransfer && e.dataTransfer.files) ? e.dataTransfer.files : null;
//             if (fs && fs.length) setFiles(fs);
//         });
//     }

//     input && input.addEventListener('change', rebuildChips);
//     ta && ta.addEventListener('input', refreshSummary);

//     clearBtn && clearBtn.addEventListener('click', () => {
//         if (input) { input.value = ''; setFiles([]); }
//         if (ta) ta.value = '';
//         refreshSummary();
//         if (status) status.textContent = '';
//     });

//     // Enter 기본 제출 방지(텍스트영역 제외)
//     form && form.addEventListener('keydown', (e) => {
//         if (e.key === 'Enter' && e.target && e.target.tagName !== 'TEXTAREA') {
//             if (!hasPayload()) e.preventDefault();
//         }
//     });

//     form && form.addEventListener('submit', (e) => {
//         if (!hasPayload()) {
//             e.preventDefault();
//             e.stopPropagation();
//             status && (status.textContent = '파일을 선택하거나 텍스트를 입력해 주세요.');
//             submitBtn && submitBtn.classList.add('dg-shake');
//             setTimeout(() => submitBtn && submitBtn.classList.remove('dg-shake'), 450);
//             return;
//         }
//         // busy 표시는 서버 응답으로 페이지 전환되므로 별도 처리 불필요
//     });

//     // init
//     rebuildChips();

//     const rs = document.getElementById('results');
//     if (rs) rs.scrollIntoView({ behavior: 'smooth', block: 'start' });
// })();
