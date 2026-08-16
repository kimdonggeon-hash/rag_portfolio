(() => {
  "use strict";
  const root = document.querySelector("#rejectedArchive");
  if (!root) return;

  const cookie = (name) => document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith(`${name}=`))?.slice(name.length + 1) || "";
  const toast = (message) => {
    const el = document.querySelector("#toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 2200);
  };

  document.querySelector("#cardGrid")?.addEventListener("click", async (event) => {
    const button = event.target.closest("button.delete");
    if (!button) return;
    const card = button.closest("[data-rejected-id]");
    const rejectedId = card?.dataset.rejectedId;
    if (!rejectedId || !confirm("원본 파일과 검수 기록을 완전히 삭제할까요? 이 작업은 되돌릴 수 없습니다.")) return;

    button.disabled = true;
    button.textContent = "삭제 중…";
    try {
      const response = await fetch(root.dataset.deleteUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type":"application/json", "X-CSRFToken":cookie("csrftoken"), "X-Requested-With":"XMLHttpRequest"},
        body: JSON.stringify({rejected_id: rejectedId}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "delete_failed");
      card.remove();
      const count = document.querySelector("#itemCount");
      count.textContent = String(Math.max(0, Number(count.textContent) - 1));
      toast("파일과 기록을 완전히 삭제했습니다.");
    } catch (error) {
      button.disabled = false;
      button.textContent = "파일과 기록 완전 삭제";
      toast(`삭제 실패: ${error.message}`);
    }
  });
})();
