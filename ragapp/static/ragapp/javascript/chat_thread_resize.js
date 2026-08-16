(function () {
    "use strict";

    const STORAGE_KEY = "rag_chat_thread_height";
    const thread = document.getElementById("chatThread");
    if (!thread) return;

    const minHeight = 320;
    const maxHeight = function () {
        return Math.max(minHeight, Math.round(window.innerHeight * 0.78));
    };
    const setHeight = function (height) {
        const next = Math.max(minHeight, Math.min(maxHeight(), Math.round(height)));
        thread.style.setProperty("height", next + "px", "important");
        return next;
    };

    try {
        const saved = Number(localStorage.getItem(STORAGE_KEY));
        if (Number.isFinite(saved) && saved >= minHeight) {
            setHeight(saved);
        }
    } catch (_) { }

    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "chat-thread-resizer";
    handle.setAttribute("aria-label", "답변 영역 높이 조절");
    handle.setAttribute("title", "위아래로 드래그해 답변 영역 크기 조절");
    thread.insertAdjacentElement("afterend", handle);

    let startY = 0;
    let startHeight = 0;

    handle.addEventListener("pointerdown", function (event) {
        if (event.button !== 0) return;
        startY = event.clientY;
        startHeight = thread.getBoundingClientRect().height;
        handle.classList.add("is-dragging");
        document.body.classList.add("is-chat-thread-resizing");
        handle.setPointerCapture(event.pointerId);
        event.preventDefault();
    });

    handle.addEventListener("pointermove", function (event) {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        setHeight(startHeight + event.clientY - startY);
    });

    const finishDrag = function (event) {
        if (handle.hasPointerCapture(event.pointerId)) {
            handle.releasePointerCapture(event.pointerId);
        }
        handle.classList.remove("is-dragging");
        document.body.classList.remove("is-chat-thread-resizing");
    };
    handle.addEventListener("pointerup", finishDrag);
    handle.addEventListener("pointercancel", finishDrag);

    handle.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        const step = event.shiftKey ? 80 : 24;
        const direction = event.key === "ArrowDown" ? 1 : -1;
        setHeight(thread.getBoundingClientRect().height + step * direction);
        event.preventDefault();
    });

    window.addEventListener("resize", function () {
        setHeight(thread.getBoundingClientRect().height);
    }, { passive: true });

    if (!("ResizeObserver" in window)) return;
    let timer = null;
    const observer = new ResizeObserver(function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
            try {
                localStorage.setItem(STORAGE_KEY, String(Math.round(thread.getBoundingClientRect().height)));
            } catch (_) { }
        }, 120);
    });
    observer.observe(thread);
})();
