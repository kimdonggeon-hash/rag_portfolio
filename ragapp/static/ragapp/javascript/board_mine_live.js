(function () {
    "use strict";

    var host = document.querySelector(".mine-page");
    var API = (host && host.dataset && host.dataset.summaryApi) ? host.dataset.summaryApi : "/board/api/mine/summary/";

    var elPosts = document.getElementById("mineStatPosts");
    var elComments = document.getElementById("mineStatComments");
    var elRole = document.getElementById("mineStatRole");
    var elPostsBadge = document.getElementById("minePostsBadge");
    var elCommentsBadge = document.getElementById("mineCommentsBadge");

    var last = { posts: null, comments: null, role: null };
    var timer = null;
    var backoff = 3000;

    function setText(el, v) { if (el) el.textContent = String(v); }

    function schedule(ms) {
        if (timer) clearTimeout(timer);
        timer = setTimeout(tick, ms);
    }

    async function tick() {
        try {
            if (document.visibilityState !== "visible") {
                schedule(10000);
                return;
            }

            var r = await fetch(API, { credentials: "same-origin", cache: "no-store" });
            if (!r.ok) throw new Error("HTTP " + r.status);

            var data = await r.json();
            if (!data || !data.ok) throw new Error("bad payload");

            last = { posts: data.posts, comments: data.comments, role: data.role };

            setText(elPosts, data.posts);
            setText(elComments, data.comments);
            setText(elRole, data.role || "Staff");
            setText(elPostsBadge, data.posts);
            setText(elCommentsBadge, data.comments);

            backoff = 3000;
            schedule(4000);
        } catch (e) {
            backoff = Math.min(Math.floor(backoff * 1.6), 20000);
            schedule(backoff);
        }
    }

    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") tick();
    });

    tick();
})();
