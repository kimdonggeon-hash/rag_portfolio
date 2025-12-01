RAG 통합 검색 콘솔 + 질문 챗봇 + 실시간 상담

## AI 활용 범위(투명성)
- 사용: 리서치, 코드/문서 초안, 대안 비교, 리팩토링 아이디어, 디버깅 체크리스트
- 미사용/직접수행: 요구사항 정의, 설계 결정, 핵심 로직 구현, 통합, 테스트, 운영/보안 반영
- 원칙: 민감정보(API 키/개인정보/회사정보) 입력 금지, 적용 전 코드 리뷰 및 로컬 테스트

> 뉴스/문서(PDF/TXT)를 인덱싱해 RAG로 답변하고, 필요 시 WebSocket 실시간 상담으로 전환되는 AI 검색/상담 통합 웹서비스
---

## 1) 한눈에 보기 (TL;DR)
- **문서 업로드(PDF/TXT/붙여넣기)** → 텍스트 추출/청킹 → 임베딩 → **벡터DB(SQLite 기반 vector store)** 저장
- **질문 챗봇(RAG)**: top-k 검색 컨텍스트로 답변 생성
- **실시간 상담(WebSocket)**: 챗봇으로 해결이 안 될 때 상담사 콘솔로 전환
- **운영/보안 기본기**: 동의(Consent) 흐름, 로깅, CSRF/보안헤더, robots.txt 준수 크롤링

---

## 2) 주요 기능
### 질문 챗봇(RAG)
- 질의 → top-k 문서 검색 → 컨텍스트 구성 → 답변 생성
- (있으면) 출처/근거 표시, 안전 요약 옵션

### 문서 업로드 & 인덱싱
- PDF/TXT 파일 업로드, 붙여넣기 텍스트 지원
- 처리 결과/에러를 사용자에게 친절히 표시

### 뉴스 수집(크롤링)
- robots.txt 준수
- (선택) 메타 기반 안전 요약/인덱싱

### 실시간 상담 콘솔
- Django Channels(WebSocket)
- 세션 상태(대기/진행/종료) 관리
- 상담사 ↔ 사용자 메시지 양방향 동기화

---

## 3) 기술 스택
- Backend: **Python, Django, Django ORM, Django Channels(WebSocket)**
- Frontend: **HTML/CSS, JavaScript(AJAX/Fetch)**
- DB: **SQLite** (서비스/벡터 저장), (선택) ChromaDB 사용 경험
- Crawling: **requests, BeautifulSoup, robotparser**
- Etc: Git, .env 환경변수

---
