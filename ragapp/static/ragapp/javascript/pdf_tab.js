// ragapp/static/ragapp/javascript/pdf_tab.js
// PDF 탭: 업로드 + Vertex 호출 + HTML 결과(표) 렌더링 (버튼 클릭 기반)

console.log("[pdf_tab] script loaded from file");

(function () {
    function $(id) {
        return document.getElementById(id);
    }

    function debug() {
        if (window.console && console.log) {
            var args = Array.prototype.slice.call(arguments);
            args.unshift("[pdf_tab]");
            console.log.apply(console, args);
        }
    }

    function attachPdfHandler() {
        var form = $("pdf-form");
        if (!form) {
            debug("폼(#pdf-form)을 찾지 못했습니다.");
            return;
        }
        if (form._pdfHandlerAttached) {
            debug("이미 핸들러가 붙어 있습니다.");
            return;
        }
        form._pdfHandlerAttached = true;

        var runBtn = $("pdf-run-btn");
        if (!runBtn) {
            debug("실행 버튼(#pdf-run-btn)을 찾지 못했습니다.");
            return;
        }

        var resultBox = $("pdf-result-box");
        var resultText = $("pdf-result-text");

        // ✅ HTML/텍스트 둘 다 지원
        function setResult(text, isHtml) {
            var msg = text || "";
            if (resultText) {
                if (isHtml) {
                    resultText.innerHTML = msg;
                } else {
                    resultText.textContent = msg;
                }
                if (resultBox) {
                    resultBox.classList.add("has-result");
                }
            } else {
                if (msg) {
                    alert(msg || "결과를 표시할 수 없습니다. (#pdf-result-text 없음)");
                }
            }
        }

        function runRequest() {
            debug("PDF 실행 로직 시작");

            var apiUrl = form.getAttribute("data-api-url");
            if (!apiUrl) {
                setResult("내부 설정 오류: data-api-url이 비어 있습니다.", false);
                return;
            }

            var fileInput = form.querySelector("input[name='pdf']");
            if (!fileInput || !fileInput.files || !fileInput.files.length) {
                setResult("PDF 파일을 먼저 선택해 주세요.", false);
                return;
            }

            var formData = new FormData(form);

            setResult("PDF 분석 중입니다… (Vertex 모델 호출 중)", false);

            fetch(apiUrl, {
                method: "POST",
                body: formData,
                credentials: "same-origin",
                headers: {
                    "X-Requested-With": "XMLHttpRequest"
                }
            })
                .then(function (resp) {
                    debug("응답 상태코드:", resp.status);

                    if (!resp.ok) {
                        return resp.json().catch(function () {
                            throw new Error("서버 오류가 발생했습니다: " + resp.status);
                        }).then(function (data) {
                            throw new Error(data.error || ("서버 오류가 발생했습니다: " + resp.status));
                        });
                    }
                    return resp.json();
                })
                .then(function (data) {
                    debug("응답 JSON:", data);

                    if (!data || !data.ok) {
                        setResult(
                            data && data.error ? data.error : "알 수 없는 오류가 발생했습니다.",
                            false
                        );
                        return;
                    }

                    // ✅ 서버에서 answer_html 이 오면 HTML로, 아니면 텍스트로
                    if (data.answer_html) {
                        setResult(data.answer_html, true);
                    } else {
                        setResult(data.answer_text || "", false);
                    }
                })
                .catch(function (err) {
                    debug("fetch 에러:", err);
                    setResult("요청 처리 중 오류가 발생했습니다: " + err.message, false);
                });
        }

        debug("PDF 실행 버튼 클릭 핸들러 연결 완료");

        runBtn.addEventListener("click", function (ev) {
            ev.preventDefault();
            debug("PDF 실행 버튼 클릭");
            runRequest();
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        debug("DOMContentLoaded 발생");
        attachPdfHandler();
    });

    if (document.readyState === "interactive" || document.readyState === "complete") {
        debug("readyState=" + document.readyState + " → 즉시 attachPdfHandler 호출");
        attachPdfHandler();
    }
})();
