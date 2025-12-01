# scripts/djtest_api_news_ingest.py
from django.test import Client
import json, os, io, pprint

def run_ingest(q: str):
    c = Client(enforce_csrf_checks=True)
    t = c.get("/")                             # CSRF 쿠키 받기
    csrftoken = t.cookies.get("csrftoken").value

    r = c.post(
        "/api/news_ingest",
        data=json.dumps({"q": q}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrftoken,
    )
    print("STATUS:", r.status_code)
    try:
        data = r.json()
    except Exception:
        print("RAW:", r.content[:400])
        return

    # 핵심 필드 요약
    news = data.get("news") or []
    ind = data.get("indexto_chroma") or {}
    print("NEWS COUNT:", len(news))
    print("INDEX SUMMARY:", {
        "inserted": ind.get("inserted"),
        "answer_chunks": ind.get("answer_chunks"),
        "news_total_chunks": ind.get("news_total_chunks"),
        "collection": ind.get("collection"),
        "dir": ind.get("dir"),
        "note": ind.get("note"),
    })

    # 프로젝트 내부에 결과도 저장해서 눈으로 확인
    os.makedirs("scripts/_out", exist_ok=True)
    with io.open("scripts/_out/ingest_result.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("SAVED:", os.path.abspath("scripts/_out/ingest_result.json"))

    # 뉴스 샘플 3개만 출력
    for i, n in enumerate(news[:3], 1):
        print(f"[{i}] {n.get('title')}  {n.get('source')}  {n.get('url')}")

if __name__ == "__main__":
    # 여기 검색어를 바꿔서 테스트 (예: '삼성전자', '초전도체', '오픈AI')
    run_ingest("AI 반도체 동향")
