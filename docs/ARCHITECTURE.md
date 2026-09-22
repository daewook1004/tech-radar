# Personal Tech Intelligence / Tech Radar — 아키텍처 & 데이터 모델 설계

- 작성일: 2026-08-26 (2026-09-18 갱신 — 운영 배포, 본문 보강, 실패 알림, 평가셋)
- 전제: [PRD.md](./PRD.md)의 MVP 요구사항 확정 + 기술 스파이크 결과를 그대로 따른다.
- 상태: **AWS EC2에서 매일 07:00(KST) 자동 실행 중.** 5개 소스 수집 → 임베딩 필터 → 본문 보강 → LLM 채점·분석 → RRF 랭킹 → 이메일 발송 + 웹 대시보드(Basic Auth)까지 전부 돌아간다. 실패 알림과 야간 DB 백업도 붙어 있다.

---

## 1. 아키텍처 원칙 요약

| 결정 | 근거 |
|---|---|
| 모노레포 (backend + frontend 한 리포) | 1인 개발, 배포도 한 VPS — 리포 분리로 얻을 이득이 없음 |
| 파이프라인 = **OS Cron + 독립 스크립트** | 웹 서버 프로세스와 완전히 분리. 웹 서버가 죽어도 수집/발송에 영향 없음 |
| 배포 = **Docker Compose** | 재현 가능한 배포, 서비스(FastAPI/Next.js/Postgres/Proxy) 단위 관리 |
| DB = PostgreSQL (pgvector 미사용) | 임베딩 비교 대상이 관심사 카테고리 10~15개뿐이라 in-memory로 충분 |
| Celery/Redis 없음 | 1일 1회 배치, 동기 처리로 충분 |
| 파이프라인은 **단계별로 DB에 중간 상태 기록** | 소스 하나가 실패해도 나머지로 계속 진행 (§9 실패 처리 요건) |

---

## 2. 디렉터리 구조

```
tech-radar/
├── docker-compose.yml
├── config/
│   ├── interests.yaml        # 관심사 카테고리 + 가중치
│   ├── sources.yaml          # GitHub watch, RSS 블로그, arXiv, GeekNews/HN 설정
│   └── settings.yaml         # 예산 상한, 컷오프 임계값, 리텐션 등
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/                       # DB 마이그레이션
│   └── app/
│       ├── main.py                    # FastAPI 앱 (대시보드 API + Basic Auth)
│       ├── api/
│       │   └── digest.py              # GET /api/today
│       ├── config.py                  # YAML config 로더
│       ├── db/
│       │   ├── models.py              # SQLAlchemy 모델
│       │   └── session.py
│       ├── schemas/
│       │   └── content.py             # Content Pydantic 모델 (공통 스키마)
│       ├── collectors/
│       │   ├── base.py                # BaseCollector 추상 인터페이스
│       │   ├── geeknews.py
│       │   ├── hackernews.py
│       │   ├── github.py              # Watch + Release + Trending 근사치
│       │   ├── arxiv.py
│       │   └── rss.py
│       ├── llm/
│       │   └── client.py              # OpenAI 클라이언트 + 예산 가드
│       └── pipeline/
│           ├── run_pipeline.py        # 진입점 — cron이 호출
│           ├── normalize.py
│           ├── dedup.py
│           ├── embed_filter.py
│           ├── llm_score.py           # gpt-5.6-luna
│           ├── llm_analyze.py         # gpt-5.6-sol
│           ├── trend_summary.py
│           ├── rank.py                # 컷오프 + 다양성 보장
│           └── deliver.py             # 이메일 발송 + digest 영속화
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   └── app/
│       └── page.tsx                   # "오늘" 화면 (Basic Auth 뒤)
└── docs/
    ├── PRD.md
    └── ARCHITECTURE.md
```

---

## 3. Source Adapter 패턴

### 3.1 추상 인터페이스

```python
# app/collectors/base.py
from abc import ABC, abstractmethod
from app.schemas.content import Content

class BaseCollector(ABC):
    source_name: str

    @abstractmethod
    def collect(self) -> list[Content]:
        """소스에서 원본 데이터를 가져와 공통 Content 스키마로 변환해 반환.
        실패 시 예외를 던진다 — 호출부(run_pipeline)가 소스 단위로 격리 처리."""
        ...
```

새 소스를 추가할 때는 `BaseCollector`를 상속한 클래스 하나만 추가하면 되고, 이후 파이프라인(정규화/dedup/필터/분석)은 소스를 몰라도 동작한다.

### 3.2 공통 Content 스키마 (최종 확정)

```python
# app/schemas/content.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Literal
import uuid

class Content(BaseModel):
    id: uuid.UUID
    source: str                          # "geeknews" | "hackernews" | "github_watch" |
                                          # "github_trending_approx" | "arxiv" | "rss:<blog_slug>"
    source_type: Literal["community", "code_repo", "paper", "blog"]
    author: Optional[str] = None
    title: str
    text: str                            # 본문 또는 발췌(arXiv는 초록만 — 비용 절감)
                                         # arXiv 수집은 2026-09-14부터 검색 API가 아니라 카테고리 RSS —
                                         # API가 어느 IP에서든 429 "Rate exceeded."를 돌려준다. 키워드
                                         # 필터는 코드에서 걸고, 카테고리별로 번갈아 뽑아 50건으로 자른다.
    url: str
    published_at: Optional[datetime] = None
    collected_at: datetime
    tags: list[str] = []
    raw_data: dict                       # 원본 API 응답 (재처리/디버깅용)

    content_hash: str                    # dedup용 (URL 정규화 + 제목/본문 해시)
    engagement_metrics: dict = {}        # {"points": 120, "comments": 45, "stars": 3400}
    language: str                        # "ko" | "en" 등, 자동 감지

    scores: Optional[dict] = None        # {"relevance": 0.82, "importance": 7, "novelty": 6, "credibility": 8}
    llm_analysis: Optional[dict] = None  # {"summary", "why_important", "topic", "keywords", "content_type"}
    status: Literal[
        "collected", "filtered_out", "scored", "analyzed", "included", "excluded"
    ] = "collected"
```

`status`는 파이프라인 단계 진행 상황을 기록해 관찰성(observability)과 재실행 가능성을 확보하기 위함이며, "어제 노출 여부"(freshness) 판단은 `status`가 아니라 `digest_item` 테이블 조회로 처리한다(§4.2).

### 3.3 소스별 Collector 요약

| Collector | 방식 | 비고 |
|---|---|---|
| `GeekNewsCollector` | RSS | |
| `HackerNewsCollector` | 공식 API (Firebase) | `min_points` 임계치로 저품질 사전 배제 |
| `GitHubCollector` | 공식 REST API | Watch Repo Release/Star 변화 + Search API 기반 Trending 근사치 (§8 스파이크 결론) |
| `ArxivCollector` | 공식 API | 카테고리/키워드 기반, 초록만 수집 |
| `RSSCollector` | RSS | `sources.yaml`의 블로그 목록을 순회하는 범용 어댑터 |

Threads는 스파이크 결과에 따라 MVP에서 제외되어 Collector를 구현하지 않는다.

---

## 4. 데이터베이스 스키마 (PostgreSQL)

### 4.1 `content` — 수집된 원문 (90일 롤링 보존)

```sql
CREATE TABLE content (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source              TEXT NOT NULL,
    source_type         TEXT NOT NULL,
    author              TEXT,
    title               TEXT NOT NULL,
    text                TEXT NOT NULL,
    url                 TEXT NOT NULL,
    published_at        TIMESTAMPTZ,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    tags                TEXT[] DEFAULT '{}',
    raw_data            JSONB NOT NULL,

    content_hash        TEXT NOT NULL,
    engagement_metrics  JSONB DEFAULT '{}',
    language            TEXT,

    scores              JSONB,
    llm_analysis        JSONB,
    status              TEXT NOT NULL DEFAULT 'collected',

    UNIQUE (content_hash)
);
CREATE INDEX idx_content_collected_at ON content (collected_at);
CREATE INDEX idx_content_status ON content (status);
```

- 90일 리텐션은 `collected_at < now() - interval '90 days'` 조건으로 배치 삭제할 계획이나 **아직 미구현**(§10).
- `content_hash` UNIQUE 제약으로 DB 레벨에서도 완전 중복 삽입을 차단(어플리케이션 레벨 유사도 dedup과 별개의 안전망).

### 4.2 `pipeline_run` — 실행 이력 (실패 처리·관찰성)

```sql
CREATE TABLE pipeline_run (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date      DATE NOT NULL UNIQUE,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,            -- 'success' | 'partial_failure' | 'failed'
    failures      JSONB DEFAULT '[]',       -- [{"stage": "collect", "source": "github", "error": "..."}]
    stats         JSONB DEFAULT '{}'        -- {"collected": 210, "passed_filter": 47, "analyzed": 8}
);
```

### 4.3 `digest` — 그날의 Tech Radar

```sql
CREATE TABLE digest (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date       DATE NOT NULL UNIQUE REFERENCES pipeline_run(run_date),
    generated_at   TIMESTAMPTZ NOT NULL,
    trend_summary  TEXT,                    -- "오늘의 주요 흐름" LLM 산출물
    email_sent_at  TIMESTAMPTZ,
    email_status   TEXT                     -- 'sent' | 'failed' | 'skipped'
);

CREATE TABLE digest_item (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    digest_id               UUID NOT NULL REFERENCES digest(id),
    content_id              UUID NOT NULL REFERENCES content(id),
    rank                    INT NOT NULL,
    final_score             NUMERIC NOT NULL,
    is_diversity_pick       BOOLEAN NOT NULL DEFAULT false,  -- 다양성 보장으로 포함됐는지
    UNIQUE (digest_id, content_id)
);
CREATE INDEX idx_digest_item_content ON digest_item (content_id);
```

`digest_item`을 `content_id`로 조회하면 "이 콘텐츠(또는 유사 콘텐츠)가 최근 며칠 내 노출됐는가"를 바로 판단할 수 있어 별도 freshness 필드 없이 어제 노출 항목 제외 로직을 구현한다.

### 4.4 `cost_ledger` — LLM 비용 추적 (예산 상한 안전장치)

```sql
CREATE TABLE cost_ledger (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date       DATE NOT NULL,
    stage          TEXT NOT NULL,      -- 'haiku_scoring' | 'sonnet_analysis' | 'trend_summary'
    model          TEXT NOT NULL,
    input_tokens   INT NOT NULL,
    output_tokens  INT NOT NULL,
    cost_usd       NUMERIC NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

각 LLM 호출 직후 `usage`를 여기 기록하고, 다음 호출 전에 `SUM(cost_usd) WHERE run_date = today` / `WHERE run_date >= 이번달 1일`을 `settings.yaml`의 `budget.daily_max_usd` / `budget.monthly_max_usd`와 비교해 초과 시 파이프라인을 중단한다(§9 요건).

---

## 5. 설정 파일 스키마 (YAML, 직접 편집)

### `config/interests.yaml`

```yaml
high:
  - name: "LLM Engineering"
    description: "LLM 애플리케이션 설계, 프롬프트 엔지니어링, LLM 기반 시스템 구축"
  - name: "RAG"
    description: "검색 증강 생성, 리트리버/인덱싱 전략, RAG 평가"
  - name: "Agent"
    description: "AI 에이전트 아키텍처, 툴 사용, 멀티에이전트, 에이전트 메모리"
  - name: "AI Coding"
    description: "AI 코딩 어시스턴트, 코드 생성/리뷰 자동화"
  - name: "LLM Evaluation"
    description: "LLM 평가 방법론, 벤치마크, 품질 측정"
  - name: "Inference"
    description: "LLM 추론 최적화, 서빙, 배포"
  - name: "Healthcare AI"
    description: "의료 분야 AI 응용"
medium:
  - name: "AI Product"
    description: "AI 제품 기획/전략"
  - name: "AI UX"
    description: "AI 제품의 사용자 경험 설계"
  - name: "Data Engineering"
    description: "데이터 파이프라인, ETL, 데이터 인프라"
  - name: "AI Infrastructure"
    description: "AI 학습/서빙 인프라, MLOps"
low:
  - name: "일반 스타트업 뉴스"
    description: "AI와 직접 관련 없는 일반 스타트업/비즈니스 뉴스"
  - name: "AI 홍보성 뉴스"
    description: "기술적 깊이가 낮은 AI 마케팅/홍보성 콘텐츠"
```

가중치는 `high=1.0 / medium=0.5 / low=0.1` 같은 형태로 임베딩 필터 단계에서 코드에 매핑(설정값으로 노출할지는 구현 단계에서 결정).

### `config/sources.yaml`

```yaml
geeknews:
  rss_url: "https://feeds.feedburner.com/geeknews-feed"

hackernews:
  min_points: 50

github:
  watch_repos:
    - pytorch/pytorch
    - huggingface/transformers
    - langchain-ai/langchain
    - run-llama/llama_index
    - vllm-project/vllm
    - ollama/ollama
    - openai/openai-python
    - anthropics/anthropic-sdk-python
  trending_approx:
    keywords: ["LLM", "agent", "RAG", "inference", "fine-tuning"]
    created_within_days: 14
    min_stars: 50

arxiv:
  categories: ["cs.CL", "cs.LG", "cs.AI", "cs.MA"]   # 카테고리별 RSS를 각각 받아 합친다
  keywords: ["retrieval augmented generation", "agent", "fine-tuning", "evaluation"]

rss_blogs:
  - name: "OpenAI"
    url: "https://openai.com/blog/rss.xml"
  - name: "Anthropic"
    url: "https://www.anthropic.com/rss.xml"
  - name: "Google AI"
    url: "https://blog.google/technology/ai/rss/"
  - name: "Meta AI"
    url: "https://ai.meta.com/blog/rss/"
  - name: "Hugging Face"
    url: "https://huggingface.co/blog/feed.xml"
  - name: "PyTorch Korea"
    url: "https://pytorch.kr/feed.xml"        # 검증 완료 (Atom, Jekyll)
  - name: "TechBlogPosts (국내 기업 기술 블로그 애그리게이터)"
    url: "https://www.techblogposts.com/rss.xml"  # 검증 완료 (Atom) — 데보션/한글과컴퓨터/컴투스 등 다회사 통합
```

*(그 외 항목은 구현 단계에서 유효성 검증 필요 — §11 미결정 항목. `pytorch.kr`, `techblogposts.com`은 2026-08-26에 curl로 직접 검증 완료)*

> **참고**: `techblogposts.com`은 AI와 무관한 일반 기술 글도 함께 섞여 들어온다. 이는 §6 파이프라인의 `embed_filter` 단계(관심사 Relevance 기반 1차 필터)가 정확히 처리하도록 설계된 상황이므로 별도 필터링 로직이 필요 없다.

### `config/settings.yaml`

```yaml
budget:
  daily_max_usd: 2.0
  monthly_max_usd: 30.0

digest:
  max_items: 10
  diversity_min_per_high_category: 1
  max_items_per_source: 4   # 소스 하나가 최종 목록을 독식하지 못하게 (2026-08-27 도입)

retention_days: 90   # 설정값만 존재 — 실제 삭제 로직은 아직 미구현, §11 참조
```

`score_threshold`/`min_items`는 2026-08-27 RRF 도입 이후 제거됨 — RRF 점수는 절대적 "좋음"이 아니라 오늘 후보군 내 상대 순위라 절대 임계치가 의미 없어짐(§6.5 참조).

---

## 6. 파이프라인 단계 (실패 격리 원칙)

```
run_pipeline.py
  ├─ 1. collect            — 소스별로 try/except 격리. 실패한 소스는 pipeline_run.failures에 기록하고 계속 진행
  ├─ 2. normalize           — 공통 스키마 정규화, content_hash 계산
  ├─ 3. dedup               — URL 정규화 + 제목/본문 유사도로 단순 중복 제거
  ├─ 4. persist             — content 테이블에 upsert(ON CONFLICT DO NOTHING RETURNING) — 새로 들어온 행만 다음 단계로. 이전 실행이 저장만 하고 끝내지 못한 행(status='collected', 3일 이내)도 이어받음
  ├─ 5. embed_filter        — 관심사 카테고리 임베딩 대비 cosine similarity로 Relevance 계산, 상위 50건만 통과
  ├─ 5.5 fill_bodies        — 통과한 글 중 본문 1000자 미만(HN 링크 글, 제목·짧은 요약만 주는 피드)은 원문 링크에서 본문을 가져와 채움. 그래도 200자 미만이면 LLM 채점·정밀분석을 건너뛰고 LLM 신호는 가운데 순위('모름')로 둠
  ├─ 6. llm_score           — gpt-5.6-luna: Importance/Novelty/Credibility (필터 통과 후보 전체)
  ├─ 6.5 정밀분석 대상 선정  — rrf_scores()로 통과 후보 전체를 재랭킹, 상위 20건만 다음 단계로
  ├─ 7. llm_analyze         — gpt-5.6-sol: 20건만 요약 + 추천 이유 (구조화 출력)
  ├─ 8. trend_summary       — gpt-5.6-sol: 필터 통과 후보군 전체(~50건) 대상 "오늘의 주요 흐름" 1회 호출
  ├─ 9. rank_and_cutoff     — 어제 노출 항목(digest_item) 제외 → rrf_scores() 재적용 → 소스당 최대 4개 cap → High 카테고리 0개면 1개 강제 포함 → 최종 10개
  └─ 10. deliver             — digest/digest_item 영속화 + Gmail 발송(콘솔 폴백) + output/{date}.txt 저장 (MUST READ + 트렌드 요약 + 오늘 수집 전체 목록)
```

**6단계(llm_score)와 9단계(rank_and_cutoff)는 같은 `rrf_scores()` 함수를 재사용한다** — 처음엔 6.5단계가 `relevance*10+importance+novelty` 식의 별도 손튜닝 공식을 썼는데, 9단계와 똑같은 스케일 불일치 문제로 relevance 높은 콘텐츠(예: 기업 블로그)가 정밀분석 대상에도 못 드는 게 실측으로 확인돼 2026-08-27 통일함(§6.5 참조).

5.5단계는 본문을 채우다 실패해도 롤백하고 원래 본문 그대로 6단계로 넘어간다 — 있으면 좋은 단계가 다이제스트 발송을 막으면 안 된다(2026-09-12·13에 NUL 바이트가 섞인 페이지 한 건으로 이틀치를 놓쳤다).

각 단계는 **소스/항목 단위로 실패를 격리**하고 `pipeline_run.failures`에 기록한다. 6~8단계에서 매 LLM 호출 전 `cost_ledger` 합계를 확인해 예산 초과 시 파이프라인을 중단하고 그때까지의 결과로 발송한다(§9). `retention_days`(90일 삭제)는 설정값만 있고 실제 삭제 로직은 아직 구현 안 됨(§11).

---

## 7. 임베딩 & LLM

### 7.1 임베딩 모델 (1차 필터, LLM 미사용)
- 원문이 한국어(GeekNews)와 영어(HN/GitHub/arXiv/블로그)가 섞여 있으므로 **다국어 지원 모델 필수**.
- 추천: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (경량, CPU에서 빠름, 하루 수백 건 처리에 충분). 정확도를 더 원하면 `BAAI/bge-m3`로 교체 가능하나 VPS CPU 부하가 커짐 — MVP는 경량 모델로 시작.
- 비교 대상은 관심사 카테고리(§5, 10~15개)뿐이므로 매 실행 시 카테고리 임베딩을 재계산해도 비용/시간 무시 가능 (별도 벡터 캐시 불필요).

### 7.2 LLM 단계별 설계 (OpenAI, Responses API 구조화 출력 사용 — 2026-08-26 Claude에서 전환)
프롬프트 본문은 구현 단계에서 작성하되, 각 단계의 입출력 계약은 아래와 같다. `client.responses.parse(..., text_format=PydanticModel)` → `response.output_parsed`로 구조화 출력을 받는다(OpenAI 공식 문서 기준, `response_format`이 아닌 `text_format`).

| 단계 | 모델 | 입력 | 출력 (구조화) |
|---|---|---|---|
| llm_score | gpt-5.6-luna | title, text 발췌, source, tags | `{importance: 1-10, novelty: 1-10, credibility: 1-10, reason: str}` |
| llm_analyze | gpt-5.6-sol | 위 정보 + relevance + 관심사 프로필 | `{summary: str, why_important: str, topic: str, keywords: [str], content_type: str}` |
| trend_summary | gpt-5.6-sol (1일 1회) | 필터 통과 후보 전체의 title+tags 목록 | 3~5줄 텍스트 (`response.output_text`) |

### 7.3 랭킹 방법론: Weighted Reciprocal Rank Fusion (2026-08-27 도입)

원래는 `final_score = relevance*40 + importance*3 + novelty*2 + credibility*1`처럼 원점수에 손으로 정한 가중치를 곱해 더했으나, relevance(임베딩 0~1)와 importance/novelty/credibility(LLM 채점 1~10)는 스케일이 전혀 달라 "40"이라는 숫자에 이론적 근거가 없었다. **Elasticsearch/OpenSearch 등이 벡터검색+키워드검색을 합칠 때 쓰는 RRF(Reciprocal Rank Fusion)로 교체**:

```python
# app/pipeline/rank.py
RRF_score(item) = Σ  weight_i / (60 + rank_i(item))   # k=60은 업계 표준값
weights = {relevance: 5, importance: 1, novelty: 1, credibility: 1}
```

신호마다 후보군 전체를 따로 정렬해 순위를 매긴 뒤 합산 — 원점수의 절대적 크기가 아니라 순위만 쓰므로 스케일 문제가 애초에 발생하지 않는다. **동일 가중치(전부 1)는 오히려 역효과**였음을 실측으로 확인(relevance 0.19인 GeekNews 뉴스가 가중합 47위 → 동일 가중치 RRF 15위로 상승) — relevance가 "4표 중 1표"로만 취급되면서 importance 최상위 랭크 하나가 낮은 relevance를 압도했기 때문. relevance 3배 가중치로 교정.

**2026-09-22: 3배도 모자랐다 — 5배로 올렸다.** 하루 50건짜리 풀 6일치(300건)에 사람이 0/1/2 라벨을 직접 매겨 재보니 nDCG@10이 ×3 0.593, ×5 0.663, ×8 0.655였고, LLM 점수만 쓰면 0.499로 무작위(0.522)보다 낮았다. 라벨과의 순위 상관도 relevance만 6일 내내 양수(중앙값 +0.16)였고 importance/novelty/credibility는 0 근처에서 부호가 오갔다. 그렇다고 relevance 단독으로 가지 않은 이유는, 관심사 밖으로 관심이 퍼진 날(2026-09-22)에는 그쪽이 더 크게 무너지기 때문이다 — ×5가 평소에 가장 좋으면서 그런 날에도 덜 무너지는 지점이었다. 평가셋 생성·라벨링·계산 도구는 `docs/eval/`(로컬 전용).

**동점은 평균 순위를 나눠 갖는다**(2026-09-13). LLM 점수는 하루 50건 중 98%가 다른 글과 동점이라, 예전처럼 입력 순서대로 서로 다른 순위를 주면 임베딩 필터가 뱉은 순서가 랭킹에 그대로 새어들어왔다 — 의도한 규칙이 아니라 상류 단계의 우연이다. 8일 치로 재현해 보면 Top 20이 하루 평균 0.4자리 바뀐다. **LLM 채점을 건너뛴 글**(본문 200자 미만)은 점수가 아예 없으므로 채점된 글들의 가운데 순위를 받는다 — 꼴찌로 두면 '모름'을 '나쁨'으로 취급하는 셈이다.

`rank.rrf_scores()`는 **두 곳에서 재사용**된다: ①정밀분석(top 20) 선정 단계, ②최종 랭킹(rank_and_cutoff) 단계. 같은 스케일 불일치 문제가 ①에서도 발생해 relevance 높은 콘텐츠(기업 블로그 등)가 정밀분석 대상에도 못 드는 걸 확인해 통일함.

gpt-5.6-luna는 OpenAI 공식 모델 카탈로그에서 "cost-sensitive, high-volume workloads" 전용으로 포지셔닝된 모델이라 1차 스코어링에 적합. gpt-5.6-sol(=gpt-5.6, 최상위 플래그십)은 2차 분석 볼륨이 하루 5~10건뿐이라 중간 티어(gpt-5.6-terra)와 비용 차이가 미미해, 품질을 우선해 선택(사용자 확정).

출력은 `output_config.format`(구조화 출력)으로 스키마를 강제해 파싱 에러를 방지한다. 모든 출력은 한국어로 통일(PRD §5.4).

---

## 8. API 설계 (FastAPI)

대시보드는 "오늘" 화면만 필요하므로 API 표면을 최소화한다.

| Endpoint | 설명 |
|---|---|
| `GET /api/today` | 오늘자 `digest` + `digest_item`(+조인된 `content`)을 JSON으로 반환 |
| `GET /health` | 헬스체크 (Docker/모니터링용) |

- 파이프라인(`run_pipeline.py`)은 FastAPI 프로세스와 별개로 cron이 직접 실행하며, DB에 결과를 쓰는 것으로 API와 간접 연결된다.
- 전체 API/대시보드 라우트는 **Basic Auth 미들웨어**로 보호(PRD §9).

---

## 9. 배포 아키텍처 (Docker Compose)

```yaml
services:
  postgres:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]

  backend:
    build: ./backend
    depends_on: [postgres]
    env_file: .env

  frontend:
    build: ./frontend
    depends_on: [backend]

  proxy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes: [./Caddyfile:/etc/caddy/Caddyfile]
    # Caddy가 TLS + Basic Auth 처리
```

- 파이프라인 실행은 호스트 cron이 `docker compose run --rm backend python -m app.pipeline.run_pipeline` 형태로 트리거 (웹 서비스 컨테이너와 별개의 1회성 실행 — §1 원칙과 일치). 22:00 UTC = 07:00 KST.
- `postgres` 볼륨만 영속화하면 되므로 백업 대상이 단순함. 2026-09-14부터 호스트 cron `0 23 * * *` UTC(=08:00 KST)이 `/opt/tech-radar/scripts-backup-db.sh`를 돌려 `pg_dump | gzip`을 `/opt/tech-radar-backups/`에 7일치 보관한다(저장소가 git clone이라 그 밖에 둔다). 인스턴스가 통째로 사라지면 이 파일도 사라지므로, 진짜 재해 대비는 주기적으로 밖으로 내려받는 것까지다.
- 대시보드 Basic Auth는 2026-09-14부터 켜져 있다(`.env`의 `BASIC_AUTH_USER`/`BASIC_AUTH_PASSWORD`). 값이 비면 통과하는 폴백이라 그동안 `/digests`가 공개 상태였다. `/health`는 모니터링용이라 인증 없이 열어 둔다.

**실패 알림 (2026-09-13 추가)** — 파이프라인이 조용히 죽으면 "오늘 메일이 안 왔네" 말고는 알 방법이 없어서 이틀치를 놓친 뒤 넣었다. 두 겹이다.

| | 무엇을 잡나 | 어떻게 |
|---|---|---|
| `run_pipeline.run()`의 crash 처리 | 파이프라인이 돌다가 죽은 경우 | `pipeline_run`을 `failed`로 닫고 traceback을 메일로 보낸 뒤 예외를 다시 던짐 |
| `run_pipeline.alert_reason()` | 실행은 끝났지만 내용이 망가진 경우 — 수집 외 단계의 실패, 수집기 전멸 | 메일로 알림. 수집기 하나가 한 번 실패하는 건(arXiv 429 등, 최근 14회 중 2회) 알리지 않는다 — 잦은 알림은 안 읽게 된다 |
| `check_digest.py` (cron `30 23 * * *` UTC = 08:30 KST) | 파이프라인이 **아예 안 돈** 경우(cron 정지, 컨테이너 기동 실패, 서버 다운), 메일 발송 실패, **소스 하나가 며칠째 죽어 있는 경우** | 그날 `digest` 행이 없거나 `email_status != 'sent'`면 알림. 같은 소스가 최근 3회 연속 실패해도 알림 — 매일 조용히 빠진 채로 다이제스트가 계속 나가는 걸 막는다 |

알림도 다이제스트와 같은 Gmail SMTP(`deliver.default_sender()`)를 쓴다 — 받는 사람이 한 명이라 이미 매일 보는 편지함이 가장 확실한 도착지다. 알림 발송 자체가 실패하면 로그만 남기고 삼킨다(`alert.send_alert`).

---

## 10. 실행 세부사항 현황 (2026-09-18 갱신)

### 해결됨
- RSS 블로그 URL 실제 값 검증 완료 (OpenAI/HuggingFace/Google AI/PyTorch Korea/TechBlogPosts). Anthropic/Meta AI는 쓸만한 공식 RSS가 없어 목록에서 제외
- `content_hash` 완전 일치 + 제목 유사도(`SequenceMatcher` ratio ≥ 0.85) dedup 구현 (`app/pipeline/dedup.py`)
- 관심사 가중치: High=1.0 / Medium=0.5 / Low=0.1 (`app/pipeline/embed_filter.py`)
- 이메일 발송: Gmail SMTP + 앱 비밀번호 (`app/pipeline/deliver.py`) — 실제 발송 검증 완료
- GitHub Trending 근사치 쿼리 파라미터: `config/sources.yaml`의 `keywords`/`created_within_days`/`min_stars`
- **EC2 배포 + 호스트 cron 자동 실행** (§9). 대시보드도 있고 Basic Auth도 켜져 있다 — "대시보드가 아직 없어 보류"라고 적혀 있던 항목들은 전부 끝났다
- **본문 보강**(§6 5.5단계) — 본문 없이 들어오던 글에 원문을 채운다. LLM 채점이 글의 중요도가 아니라 주어진 정보량을 반영하던 문제를 입력 쪽에서 고친 것
- **실패 알림 2겹 + 야간 DB 백업** (§9)
- **채점 프롬프트에 calibration anchor 추가 — 시도했고 버렸다.** 점수대별 기준을 넣고 "발췌가 짧다고 깎지 마라"라고 못 박았더니, 중요도·새로움의 길이 민감도는 그대로인 채 신뢰도만 −2.36으로 흔들렸고 세 점수가 오히려 더 뭉쳤다(첫 주성분 52%→71%). 프롬프트가 아니라 입력을 고치는 쪽으로 방향을 바꾼 계기

### 아직 미결정/미구현
- **채점 프롬프트 재설계 후보** — 라벨 300건으로 가중치는 ×5로 정했지만(§7), LLM 세 점수가 사람 판단과 거의 무관하다는 건 그대로다. 지금 프롬프트는 관심사를 한 글자도 모른 채 "AI/개발 실무자에게 미치는 파급력"을 묻는다. 관심사를 넣고 "이 사람이 읽을 가치"를 묻는 쪽으로 바꾸면 달라지는지 확인 필요
- **교차 출처 신호 미사용** — 같은 이슈를 여러 소스가 동시에 다루는지를 랭킹이 전혀 보지 않는다. LLM 점수보다 강한 신호일 수 있어 라벨로 검증 예정
- **RAG 질의 레이어** — `GET /ask?q=...` 한 줄 질문/한 번 답변 (청사진은 로컬 전용 문서). 본문 보강 이후에야 코퍼스가 쓸 만해졌다(1,000자 이상 721건). t3.micro에서 질의마다 임베딩 모델을 새로 올리지 않도록 상주 여부를 먼저 확인해야 함
- **`retention_days`(90일) 삭제 로직 미구현** — 설정값만 있고 실제 배치 삭제 코드 없음. DB는 16MB/2,689건이라 급하지 않다
- 정밀분석(top 20) 선정 단계까지는 RRF cap 없음 — 최종 랭킹엔 소스당 최대 4개 cap이 있지만, top 20이 특정 소스로 쏠리면 다른 소스는 애초에 최종 후보가 될 기회가 없음. 필요성 재검토 중
- **백업이 인스턴스 밖으로 안 나간다** — 야간 pg_dump는 같은 EC2 디스크에 쌓인다. 인스턴스가 통째로 사라지면 같이 사라진다
- **본문 보강의 한계** — 자바스크립트로 그리는 페이지는 못 가져오고(하루 3~5건), 기존 RSS 요약에 원문을 이어 붙이는 방식이라 같은 내용이 두 번 들어갈 수 있다
- 대시보드 검색/필터 없음 — 날짜별 목록과 상세 조회만
