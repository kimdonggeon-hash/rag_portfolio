(() => {
  "use strict";
  const root = document.querySelector("#approvedGrid");
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
    const button = event.target.closest("button.remove");
    if (!button || button.disabled) return;
    const card = button.closest("[data-approved-id]");
    const approvedId = card?.dataset.approvedId;
    if (!approvedId || !confirm("이 이미지를 검색 결과에서 제거할까요?")) return;

    button.disabled = true;
    button.textContent = "제거 중…";
    try {
      const response = await fetch(root.dataset.removeUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type":"application/json", "X-CSRFToken":cookie("csrftoken"), "X-Requested-With":"XMLHttpRequest"},
        body: JSON.stringify({approved_id: approvedId}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "remove_failed");

      card.classList.add("is-removed");
      const status = card.querySelector(".status");
      if (status) {
        status.textContent = "검색에서 제거됨";
        status.classList.add("is-removed-chip");
      }
      button.textContent = "이미 제거됨";
      toast("검색 결과에서 제거했습니다.");
    } catch (error) {
      button.disabled = false;
      button.textContent = "검색에서 이미지 제거";
      toast(`제거 실패: ${error.message}`);
    }
  });
})();
