# AI RAG Portfolio Service

뉴스 데이터와 PDF 문서를 기반으로 사용자의 질문에 답변하는 **AI 기반 RAG 검색/질의응답 서비스**입니다.
단순 챗봇 구현을 넘어서, 문서 수집 → 텍스트 전처리 → 임베딩 생성 → 벡터 검색 → LLM 답변 생성 → 실시간 상담 전환까지의 흐름을 하나의 웹 서비스로 구성했습니다.

> 개인 포트폴리오 프로젝트로, AI 기능을 실제 웹 서비스에 적용하고 운영 관점까지 고려하는 것을 목표로 개발했습니다.

---

## 주요 기능

### 1. 뉴스 / 문서 기반 RAG 질의응답

* 뉴스 및 문서 데이터를 수집하고 텍스트를 정제합니다.
* 문서를 적절한 단위로 분할한 뒤 임베딩을 생성합니다.
* 사용자의 질문과 가장 관련 있는 문서를 검색합니다.
* 검색된 문맥을 기반으로 LLM이 답변을 생성합니다.

### 2. PDF 업로드 기반 질의응답

* 사용자가 PDF 파일을 업로드할 수 있습니다.
* PDF에서 텍스트를 추출하고 필요한 범위만 처리합니다.
* 업로드된 문서를 기반으로 질문에 답변합니다.
* 파일 개수, 용량, 처리 가능한 텍스트 길이에 제한을 두어 과도한 사용을 방지했습니다.

### 3. 실시간 상담 전환

* AI 답변만으로 해결이 어려운 경우 실시간 상담으로 전환할 수 있습니다.
* Django Channels 기반 WebSocket을 사용했습니다.
* 상담 대기, 진행, 종료 상태를 관리합니다.
* 사용자와 관리자 양쪽 메시지가 동일하게 동기화되도록 room routing과 event type을 정리했습니다.

### 4. 관리자 기능

* 관리자 전용 페이지를 통해 사용량과 로그를 확인할 수 있습니다.
* 질문 로그, 상담 세션 로그, 피드백 로그를 분리해 관리합니다.
* 서비스 상태와 일일 사용량을 확인할 수 있도록 구성했습니다.

### 5. 보안 및 운영 고려

* CSRF 보호, 보안 헤더, 요청 제한을 적용했습니다.
* 비정상적인 경로 접근을 차단하는 미들웨어를 추가했습니다.
* 욕설, 성희롱성 표현 등 부적절한 입력을 차단하는 Content Guard를 구현했습니다.
* 기능별 사용량 제한을 두어 과도한 요청을 방지했습니다.
* Cloud Run 환경에서 운영할 수 있도록 환경변수 기반 설정을 구성했습니다.

---

## 기술 스택

| 구분                   | 기술                                |
| -------------------- | --------------------------------- |
| Backend              | Django                            |
| Realtime             | Django Channels, WebSocket        |
| Database             | PostgreSQL, Cloud SQL             |
| Vector Store         | Chroma DB                         |
| AI / LLM             | Vertex AI, Gemini, Text Embedding |
| Storage              | Google Cloud Storage              |
| Deployment           | Google Cloud Run                  |
| Container / Registry | Docker, Artifact Registry         |
| Static Files         | WhiteNoise                        |
| Environment          | Python, dotenv                    |

---

## 시스템 구조

```text
사용자 질문
   ↓
Django View
   ↓
질문 전처리 및 사용량 검사
   ↓
Embedding 생성
   ↓
Vector DB 검색
   ↓
관련 문서 Top-K 추출
   ↓
LLM 답변 생성
   ↓
사용자에게 응답 반환
```

PDF 기반 질의응답의 경우 다음 흐름을 따릅니다.

```text
PDF 업로드
   ↓
텍스트 추출
   ↓
텍스트 정제 / 분할
   ↓
Embedding 생성
   ↓
Vector Store 저장
   ↓
질문 시 관련 Chunk 검색
   ↓
LLM 답변 생성
```

실시간 상담 전환은 다음 흐름으로 구성했습니다.

```text
사용자 상담 요청
   ↓
상담 세션 생성
   ↓
WebSocket Room 연결
   ↓
관리자와 사용자 메시지 동기화
   ↓
상담 종료 및 로그 저장
```

---

## 프로젝트에서 중점적으로 고려한 부분

### 1. AI 기능의 서비스화

단순히 LLM API를 호출하는 것에서 끝내지 않고, 실제 사용자가 사용할 수 있는 웹 서비스 흐름으로 구성했습니다.
질문 입력, 문서 검색, 답변 생성, 예외 처리, 사용량 제한, 로그 기록까지 하나의 기능으로 연결했습니다.

### 2. 운영 가능한 구조

포트폴리오 프로젝트이지만 실제 배포 환경을 고려했습니다.

* Cloud Run 기반 배포
* Cloud SQL PostgreSQL 연결
* GCS 기반 파일 저장
* 환경변수 기반 설정 분리
* 관리자 로그 확인
* 요청 제한 및 예외 처리

### 3. 문제 발생 시 추적 가능한 로그 구조

서비스에서 발생하는 주요 이벤트를 분리해 기록했습니다.

* 질문 로그
* PDF 처리 로그
* 상담 세션 로그
* 피드백 로그
* 관리자 사용량 로그

이를 통해 문제가 발생했을 때 원인을 더 쉽게 추적할 수 있도록 했습니다.

### 4. 사용자 경험 개선

AI 답변 실패나 오류가 발생했을 때 내부 에러를 그대로 보여주지 않고, 사용자가 이해할 수 있는 메시지를 반환하도록 처리했습니다.
또한 AI 답변으로 해결되지 않는 상황을 고려해 실시간 상담 전환 기능을 추가했습니다.

---

## 문제 해결 경험

### 실시간 상담 메시지 동기화 문제

초기 구현에서는 사용자와 관리자 중 한쪽 화면에만 메시지가 표시되는 문제가 있었습니다.
이를 해결하기 위해 WebSocket room routing 구조와 event type을 다시 정리했습니다.

개선한 부분은 다음과 같습니다.

* 사용자 / 관리자 room 연결 규칙 통일
* 메시지 이벤트 타입 정리
* 상담 상태값 관리
* 양쪽 메시지 로그 저장 방식 개선
* 상담 종료 시 세션 상태 업데이트

이 과정을 통해 단순 기능 구현보다, 실시간 기능에서는 상태 관리와 이벤트 설계가 중요하다는 것을 경험했습니다.

---

## 실행 방법

### 1. 저장소 클론

```bash
git clone https://github.com/kimdonggeon-hash/rag_portfolio.git
```

### 2. 가상환경 생성 및 실행

```bash
python -m venv .venv
```

Windows PowerShell 기준:

```bash
.venv\Scripts\activate
```

### 3. 패키지 설치

```bash
pip install -r requirements.txt
```

### 4. 환경변수 설정

`.env` 파일을 생성하고 필요한 환경변수를 설정합니다.

```env
DJANGO_SECRET_KEY=
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

DB_NAME=
DB_USER=
DB_PASSWORD=
DB_HOST=
DB_PORT=

GOOGLE_CLOUD_PROJECT=
VERTEX_PROJECT=
VERTEX_LOCATION=
GS_BUCKET_NAME=
```

### 5. 데이터베이스 마이그레이션

```bash
python manage.py migrate
```

### 6. 개발 서버 실행

```bash
python manage.py runserver
```

---

## 배포 환경

이 프로젝트는 Google Cloud Platform 기반 배포를 고려해 구성했습니다.

* Cloud Run: Django 서비스 실행
* Cloud SQL: PostgreSQL 데이터베이스
* Google Cloud Storage: 업로드 파일 저장
* Artifact Registry: Docker 이미지 저장
* Vertex AI: Embedding 및 LLM 호출

---

## 향후 개선 방향

* RAG 답변 품질 평가용 테스트셋 구성
* 검색 결과 신뢰도 점수 표시
* 관리자 대시보드 고도화
* 비동기 작업 큐 도입
* Redis 기반 Channel Layer 적용
* 사용자 계정 기능 추가
* 문서별 권한 관리 기능 추가

---

## 프로젝트를 통해 배운 점

이 프로젝트를 진행하면서 AI 기능은 단순히 모델을 호출하는 것만으로 완성되지 않는다는 것을 배웠습니다.
실제 서비스에서는 데이터 전처리, 검색 품질, 예외 처리, 사용량 제한, 로그 관리, 배포 환경, 사용자 경험까지 함께 고려해야 했습니다.

또한 문제를 해결할 때 감으로 수정하기보다, 로그와 흐름을 확인하고 원인을 좁혀가는 방식이 중요하다는 것을 경험했습니다.
앞으로도 AI 기능을 실제 서비스에 안정적으로 연결할 수 있는 개발자로 성장하고자 합니다.
