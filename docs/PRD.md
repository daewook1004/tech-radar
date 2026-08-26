# Personal Tech Intelligence / Tech Radar — MVP 제품 요구사항 정의서 (PRD)

- 작성일: 2026-08-26
- 상태: MVP 요구사항 확정 + 기술 스파이크(Threads/GitHub Trending) 완료. 데이터 모델/아키텍처 상세 설계 단계 진입 전.
- 작성 방식: Deep Interview (8라운드, 32개 질문/답변) + 기술 스파이크(웹 리서치) 기반 정리

---

## 1. 제품 정의

인터넷에 매일 쏟아지는 AI·LLM·개발 관련 정보를 여러 사이트를 돌아다니며 확인하지 않아도 되도록,

> **여러 정보원을 자동으로 수집 → 중복/저품질 제거 → LLM이 분석 → 개인 관심사에 맞춰 랭킹 → "오늘 읽어야 할 5~10개"를 자동으로 결정**

해주는 개인용 기술 정보 시스템. 단순 RSS Reader나 SNS 수집기가 아니라, 최종 판단(무엇을 읽을지)까지 자동화하는 것이 핵심 가치.

### 성공 기준 (Definition of Done)
- 정성적 기준: 몇 주간 실사용 후 **"다른 사이트를 굳이 안 돌아다녀도 되겠다"**고 체감하면 성공.
- 별도의 정량 지표(클릭률 등)는 MVP 단계에서 강제하지 않음.

---

## 2. MVP 범위 (Sources)

Source Adapter 구조로 설계하되, MVP에서는 아래 소스만 구현한다.

| Source | 방식 | 비고 |
|---|---|---|
| GeekNews | RSS | |
| Hacker News | RSS/API | |
| GitHub | 공식 API | Watch Repo + Release + Star 변화 + **Trending 근사치** (Search API로 "최근 생성/푸시 + star 급증" 쿼리, §8 스파이크 완료) |
| arXiv | 공식 API | 카테고리/키워드 기반 (cs.CL, cs.LG, cs.AI 등 매핑은 설계 단계에서 확정) |
| RSS 기술 블로그 | RSS | OpenAI/Anthropic/Google/Meta 등 공식 블로그 + 스타트업 엔지니어링 블로그 |
| ~~Threads~~ | **MVP 제외** | 기술 스파이크 결과 Standard Access로는 지정 개인 계정 접근이 불가능한 것으로 판명 (§8 참조) |

### v2 이후로 명시적으로 미루는 것
- Cross-source Topic/Event Clustering (동일 사건을 하나의 Topic으로 묶기)
- 행동 로그(클릭/저장/평가) 기반 개인화 학습 및 랭킹 반영
- RAG/자연어 질의 검색 (#9 장기 비전)
- Threads 전면 도입 (Meta Advanced Access 승인 시 재검토 — MVP에서는 완전 제외, §8 참조)
- 대시보드 내 설정 UI (관심사/소스 관리)
- 대시보드 히스토리(지난 날짜 Radar 조회) 기능

---

## 3. 아키텍처 개요

```
Source (Threads/GitHub/GeekNews/HN/arXiv/RSS)
  → Collector (Source Adapter, 소스별 개별 구현)
  → 공통 Content Schema로 변환
  → Raw 저장 (90일 롤링 보존)
  → 정규화 + 단순 Dedup (URL 정규화 + 제목/본문 유사도)
  → 임베딩 기반 1차 필터 (관심사 카테고리 벡터 대비 Relevance)
  → LLM 2단계 분석 (OpenAI)
  → Score 계산 + 다양성 보장 + 컷오프
  → Daily Tech Radar 생성
  → 이메일 발송 + 대시보드 게시
```

### 인프라 원칙
- **Celery/Redis 없음.** Cron/APScheduler + 동기 처리로 충분 (1인 사용자, 1일 1회 배치 규모).
- **pgvector 불필요 (MVP 한정).** 임베딩 비교 대상이 관심사 카테고리(10~15개) 수준으로 작아 in-memory cosine similarity로 충분. pgvector는 v2 RAG 단계에서 필요 시 도입.
- DB: PostgreSQL.
- 배포: 저가형 VPS(예: Hetzner/DigitalOcean/Vultr $5~10/월)에 FastAPI + Next.js + Postgres + cron을 한 대로 상시 운영.
- 대시보드는 퍼블릭 VPS에 노출되므로 **Basic Auth**로 최소 보호.

---

## 4. Content 데이터 모델 (확정, 상세 설계 단계로 이관)

원안 필드에 아래 필드를 추가한 것을 기준으로 다음 설계 단계(DB 테이블 설계)를 진행한다.

| 필드 | 설명 |
|---|---|
| `source`, `source_type` | 원안대로 |
| `author` | 원안대로 |
| `title`, `text` | 원안대로 |
| `url` | 원안대로 |
| `published_at`, `collected_at` | 원안대로 |
| `tags` | 원안대로 |
| `raw_data` | 원안대로 |
| `content_hash` | 단순 Dedup용 (URL 정규화 + 본문 해시) |
| `engagement_metrics` | HN 포인트/GitHub star/Threads 좋아요 등 — Importance 판단 신호 |
| `language` | 원문 언어 (ko/en 등) |
| `scores` | relevance / importance / novelty / credibility |
| `llm_analysis` | 요약, 추천 이유, topic, 키워드 등 LLM 산출물 |
| `status` | 어제 노출 여부 등 freshness 추적용 |

---

## 5. 처리 파이프라인 상세

### 5.1 수집
- 전체 소스 1일 1회 배치 (소스별 차등 주기 아님).

### 5.2 저장 및 Dedup
- Raw 데이터는 **90일 롤링 보존** (30/90/180일 중 90일 선택 — 향후 RAG의 "최근 3개월" 질의 예시와 정합).
- Dedup은 URL 정규화 + 제목/본문 유사도 기반 **단순 중복 제거만** MVP에 포함. Cross-source Topic Clustering은 v2.

### 5.3 1차 필터 (임베딩, LLM 미사용)
- 관심사 카테고리(§6) 임베딩과의 코사인 유사도로 Relevance를 계산, 수백 건 → 수십 건으로 축소.
- 임베딩 모델은 로컬 오픈소스 모델 사용을 권장 (비용 $0, 별도 의존성 최소화). 매니지드 API(Voyage 등) 사용 시에도 이 볼륨에서는 비용 무시할 수준.

### 5.4 LLM 분석 (OpenAI만 사용, 2단계 — 2026-08-26 Claude에서 전환)
- **1차 (gpt-5.6-luna)**: 1차 필터 통과 후보(30~80건/일)에 대해 Importance, Novelty, Credibility 스코어링. 공식 문서상 "cost-sensitive, high-volume workloads" 전용 모델.
- **2차 (gpt-5.6-sol)**: 상위 후보(5~10건)만 정밀 요약 + 추천 이유 생성. 볼륨이 작아 비용 차이가 미미하므로 중간 티어(terra) 대신 최상위 플래그십 선택(사용자 확정).
- **트렌드 요약 (추가 LLM 호출 1회)**: 1차 필터 통과 후보군 전체를 대상으로 "오늘의 주요 흐름"(3~5줄) 생성.
- **출력 언어**: 원문이 영어든 한국어든, 요약/분석 결과는 **한국어로 통일**.

### 5.5 랭킹 및 컷오프
- Score 임계치 기반 컷오프 (고정 Top-N 아님).
- **다양성 보장**: High 관심사 카테고리 중 그날 후보가 있다면 최소 1개는 노출되도록 보정 (순수 Score 순위만 따르지 않음).
- 어제 이미 노출된 항목/유사 항목은 자동 제외 (freshness 유지).

### 5.6 산출 및 전달
- **이메일 (주 채널)**: 요약, Score, 추천 이유, Source 링크까지 **전체 내용 포함**. 대시보드를 거의 안 열어도 이메일만으로 완결되게 구성.
- **대시보드 (보조 채널)**: MVP는 **"오늘" 화면만** 제공. 히스토리 조회는 v2.
- 예시 출력 형태:
  ```
  오늘의 Tech Radar
  [MUST READ] Agent Memory에 관한 새로운 Framework — Score: 95
  왜 중요한가: ...
  Source: GitHub / arXiv / Hacker News

  오늘의 주요 흐름
  - Agent Memory 관련 논의 증가
  - Inference Optimization 관련 Repository 증가
  - MCP 관련 Tool 생태계 확장
  ```

---

## 6. Personalization (MVP)

### 방식
- **정적 관심사 가중치만 사용** (High/Medium/Low). 클릭/저장/평가 로그 수집 및 랭킹 반영은 v2.
- 관심사는 **카테고리 + 가중치 고정 목록** 형태로 표현 (카테고리 설명문 기준으로 임베딩 생성).

### 초기 관심사 목록 (config 초기값으로 그대로 사용)
| 등급 | 카테고리 |
|---|---|
| High | LLM Engineering, RAG, Agent, AI Coding, LLM Evaluation, Inference, Healthcare AI |
| Medium | AI Product, AI UX, Data Engineering, AI Infrastructure |
| Low | 일반 스타트업 뉴스, 단순 AI 홍보 뉴스 |

### 설정 관리
- YAML/JSON 설정 파일을 **직접 편집**하는 방식. 대시보드는 읽기 전용 (설정 UI는 v2).

---

## 7. 초기 시드 데이터 (GitHub Watch / RSS 블로그)

브리핑에서 언급된 후보를 거의 그대로 사용 (10~15개 내외로 시작):

PyTorch, Hugging Face, LangChain, LlamaIndex, vLLM, Transformers, Ollama, OpenAI, Anthropic, Google DeepMind, Meta AI 등

### 추가된 국내 RSS 소스 (2026-08-26, 실제 피드 검증 완료)
- **PyTorch Korea** (`pytorch.kr/feed.xml`) — 파이토치 한국 사용자 모임 공식 블로그. GitHub `pytorch/pytorch` Watch와 보완 관계.
- **TechBlogPosts** (`techblogposts.com/rss.xml`) — 데보션/한글과컴퓨터/컴투스 등 **국내 여러 기업 기술 블로그를 모아주는 애그리게이터**. 개별 국내 스타트업 블로그를 일일이 등록하지 않고 이 피드 하나로 "국내 스타트업 및 엔지니어링 블로그" 요구사항(§원 브리핑)을 충족. AI 무관 콘텐츠도 섞여 들어오지만 임베딩 기반 1차 필터가 처리하도록 설계되어 있어 문제 없음.

---

## 8. 기술 검증 스파이크 (완료)

### Threads — 조사 결과: 기준 미충족, MVP 제외
- `GET /{threads-user-id}/threads`: 인증된 본인(app-scoped) 계정만 조회 가능. 타인 계정 불가.
- `GET /profile_posts?username=...`: 타인 공개 프로필 조회용 엔드포인트가 존재하나
  - `threads_basic` + `threads_profile_discovery` 권한 필요 (둘 다 Meta App Review 대상)
  - **Standard Access**: 오직 공식 Meta 계정(@meta, @threads, @instagram, @facebook)만 조회 가능 + 대상 계정 팔로워 100명 이상 조건 — 사용자가 원하는 개발자/연구자/인플루언서 개인 계정은 처음부터 조회 불가
  - **Advanced Access**로 승급해야 임의의 공개 프로필(팔로워 100+) 접근 가능하나, Meta App Review 통과가 필요하고 비즈니스 인증/사용목적 소명 등 개인 프로젝트 대비 과도한 절차이며 승인 여부도 불확실
  - Rate limit(1,000 req/24h)은 문제 아님 — 핵심 제약은 접근 범위 자체
- **결론**: MVP 편입 기준("지정한 계정들의 게시물을 API로 가져올 수 있으면 충분")을 충족하지 못함 → **MVP에서 완전 제외**. v2에서 Advanced Access 신청을 별도로 검토할 수 있음.

### GitHub Trending — 조사 결과: 공식 API 없음, Search API 근사치로 MVP 포함
- 공식 REST/GraphQL에 trending/explore 엔드포인트 없음 확인 (GitHub이 의도적으로 UI 전용 유지).
- **채택안**: `GET /search/repositories`(공식 Search API)로 "AI/LLM 키워드 + 최근 N일 내 생성/푸시 + star 내림차순" 쿼리를 사용해 트렌딩과 유사한 근사치 확보. 완전히 공식적인 방식이라 ToS 리스크 없음. Rate limit 30 req/min으로 충분.
- 기각안: `github.com/trending` 페이지 스크래핑 — 비공식/ToS 회색지대라 채택하지 않음.
- **결론**: **Search API 근사치로 MVP에 포함**.

---

## 9. 운영/안전장치

- **실패 처리**: 파이프라인 일부(특정 소스 API 다운, LLM 호출 에러) 실패 시, 실패한 부분은 건너뛰고 나머지로 발송하되 **실패 사실을 이메일 하단에 투명하게 표시**.
- **비용 폭주 방지**: 버그/소스 이상으로 수집량이 폭증할 경우를 대비해 **일일/월 예산(건수 또는 토큰) 상한을 코드 레벨에서 설정**, 초과 시 파이프라인 중단 + 알림.

---

## 10. 예상 월 유지비용 (참고용 추정치)

| 항목 | 예상 비용 |
|---|---|
| VPS | $5~10/월 |
| LLM API (OpenAI, 2단계) | $5~12/월 |
| 임베딩 (로컬 모델 권장) | $0~1/월 |
| 이메일 발송 | $0 (무료 티어로 충분) |
| 도메인 (선택) | $0~1/월 |
| **합계** | **약 $10~23/월** |

세부 근거(2026-08-26 OpenAI 공식 가격 기준): gpt-5.6-luna($0.20/$1.20 per MTok)로 1차 스코어링 40~80건/일 → 월 $0.6~1.1, gpt-5.6-sol($4/$20 per MTok, 2026-11-21까지 프로모션가)로 최종분석 5~10건/일 → 월 $4.3~8.7, 트렌드 요약 1일 1회 → 월 $0.4 내외. 서버비가 LLM비와 비슷하거나 더 큰 비중일 정도로 개인 프로젝트 규모에서 LLM API 비용 자체는 낮은 편.

---

## 11. 아직 결정되지 않은 실행 세부사항 (다음 설계/구현 단계에서 확정)

- arXiv 카테고리/키워드 정확한 매핑 (cs.CL / cs.LG / cs.AI 등)
- 이메일 발송 서비스 선정 (SMTP 직접 구성 vs Resend/SES 등 트랜잭셔널 서비스)
- Content 스키마의 실제 DB 테이블/인덱스 설계
- Basic Auth 자격증명 관리 방식
- GitHub Trending 근사치 쿼리의 정확한 파라미터(기간/키워드/정렬 기준) 설계

---

## 부록: 인터뷰 결정 이력 요약

| 주제 | 결정 |
|---|---|
| 전달 방식 | 하이브리드 (웹 대시보드 + 이메일 푸시) |
| Push 채널 | Email (주 채널로 확정) |
| Threads MVP 포함 | **스파이크 결과 완전 제외** (Standard Access로 개인 계정 접근 불가, Advanced Access는 개인 프로젝트 대비 과도) |
| GitHub Trending | **스파이크 결과 Search API 근사치로 MVP 포함** (공식 API 없음, 스크래핑은 기각) |
| MVP 소스 | GeekNews+HN, GitHub(Watch/Release/Trending 근사치), arXiv, RSS 블로그 (Threads 제외) |
| Dedup 범위 | 단순 중복 제거만 (Topic Clustering은 v2) |
| 인프라 | Cron/APScheduler + 동기 처리 (Celery/Redis 없음) |
| Personalization | 정적 관심사 가중치만 (행동 로그 반영은 v2) |
| 데이터 보존 | 90일 롤링 |
| LLM 파이프라인 | 2단계 (임베딩 사전필터 + LLM 정밀분석), LLM도 저가/고가 2티어 |
| Score 차원 | 사전필터=Relevance(임베딩), LLM=Importance+Novelty+Credibility |
| Digest 컷오프 | Score 임계치 + 어제 노출 항목 제외 + High 카테고리 최소 1개 다양성 보장 |
| 수집 주기 | 전체 1일 1회 배치 |
| 설정 관리 | YAML/JSON 파일 직접 편집 |
| LLM Provider | OpenAI만 사용 (2026-08-26: Claude → OpenAI로 전환, gpt-5.6-luna/gpt-5.6-sol) |
| 배포 환경 | 저가형 VPS 상시 배포 |
| 트렌드 요약 섹션 | MVP 포함 (간단한 LLM 요약) |
| 관심사 표현 | 카테고리+가중치 고정 목록 |
| 대시보드 보호 | Basic Auth |
| 성공 기준 | "다른 사이트를 덜 돌아다니게 됨" (정성적) |
| 실패 처리 | 부분 실패시 스킵 후 발송 + 실패 사실 표시 |
| 초기 시드 | GitHub/블로그 후보 거의 그대로, 관심사 카테고리 브리핑 원안 그대로 |
| 모바일 접근 | 이메일이 주 채널, 대시보드는 가끔만 (모바일 반응형 최우선 아님) |
| 출력 언어 | 한국어로 통일 |
| 비용 안전장치 | 일일/월 예산 상한 코드 레벨 설정 |
| 이메일 콘텐츠 | 전체 내용 포함 (티저 아님) |
| 대시보드 히스토리 | MVP는 "오늘" 화면만 |
| 랭킹 다양성 | High 관심사 카테고리 최소 1개 보장 |
