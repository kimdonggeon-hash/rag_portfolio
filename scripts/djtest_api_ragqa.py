# scripts/djtest_api_ragqa.py
from django.test import Client
import json

def hit(q: str):
    c = Client(enforce_csrf_checks=True)
    t = c.get("/")                             # CSRF 쿠키 받기
    csrftoken = t.cookies.get("csrftoken").value

    r = c.post(
        "/api/rag_qa",
        data=json.dumps({"query": q}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrftoken,
    )
    print("STATUS:", r.status_code)
    try:
        data = r.json()
    except Exception:
        print("RAW:", r.content[:400])
        return
    print("KEYS:", list(data.keys()))
    print("MODE:", data.get("mode"))
    print("MODEL:", data.get("model"))
    print("ANSWER[:200]:", (data.get("answer_text") or data.get("answer") or "")[:200])
    hits = data.get("hits") or []
    print("HITS:", len(hits))
    if hits[:3]:
        print("HIT SAMPLES:", [
            {"title": h.get("title"), "source": h.get("source"), "url": h.get("url")}
            for h in hits[:3]
        ])

if __name__ == "__main__":
    # 여기에 테스트 질문 바꿔가며 써봐
    hit("내 프로젝트 RAG 파이프라인 설명해줘")
