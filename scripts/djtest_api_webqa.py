# scripts/djtest_api_webqa.py
from django.test import Client
import json

c = Client(enforce_csrf_checks=True)
t = c.get("/")
csrftoken = t.cookies.get("csrftoken").value

r = c.post(
    "/api/web_qa",
    data=json.dumps({"q": "테스트 질문"}),
    content_type="application/json",
    HTTP_X_CSRFTOKEN=csrftoken,
)

print("STATUS:", r.status_code)
try:
    data = r.json()
except Exception:
    print("RAW:", r.content[:400])
else:
    print("KEYS:", list(data.keys()))
    print("ANSWER_TEXT[:200]:", (data.get("answer_text") or data.get("answer") or "")[:200])
