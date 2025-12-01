# scripts/djtest_websearch.py
from django.test import Client
import re, os, io

c = Client()
t = c.get("/")  # ensure_csrf_cookie 때문에 먼저 GET
csrftoken = t.cookies.get("csrftoken").value

# 홈에서 웹 검색 트리거
r = c.post("/", {
    "action": "web_search",
    "query_web": "테스트 질문",
    "csrfmiddlewaretoken": csrftoken,
})
html = r.content.decode("utf-8", "ignore")

# HTML도 프로젝트 내부에 떨궈서 직접 눈으로 확인
os.makedirs("scripts/_out", exist_ok=True)
with io.open("scripts/_out/tmp_news.html", "w", encoding="utf-8") as f:
    f.write(html)

# 답변 블록( <pre> 또는 class에 answer류가 들어간 div )을 통합 패턴으로 추출
pat = re.compile(
    r'(?s)(?:<pre[^>]*>|<div[^>]*class="[^"]*(?:web-answer|gemini-answer|answer)[^"]*"[^>]*>)(.*?)(?:</pre>|</div>)'
)
m = pat.search(html)
extracted = (m.group(1).strip() if m else "")

print("STATUS:", r.status_code)
print("EXTRACTED:", extracted[:400])
print("OUT_HTML:", os.path.abspath("scripts/_out/tmp_news.html"))
