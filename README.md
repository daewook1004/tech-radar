# Tech Radar

매일 아침, AI·LLM·개발 분야에서 **내가 오늘 읽어야 할 10개**를 골라서 메일로 보내주는 개인용 기술 정보 시스템.

RSS 리더가 아니라 **"무엇을 읽을지"라는 판단까지 자동화**하는 것이 목표다. 하루 300건 가까이 모아서, 관심사와의 관련도·중요도·새로움·신뢰도를 평가하고, 상위 10개만 남긴다.

현재 AWS EC2에서 매일 아침 7시(KST)에 자동으로 돌고 있다.

---

## 왜 만들었나

GeekNews, Hacker News, arXiv, GitHub 트렌딩, 회사 기술 블로그를 매일 각각 돌아다니는 게 번거로웠다. 그렇다고 RSS 리더에 다 모아두면 결국 읽지 않은 수백 개가 쌓일 뿐이었다.

필요한 건 "모아주는 것"이 아니라 **"걸러주는 것"** 이었다.

## 어떻게 동작하나

```
5개 소스 수집(~300건)
   → URL 정규화 + 제목 유사도 중복 제거
   → 임베딩 관련도 필터 (관심사 카테고리 대비 코사인 유사도, 상위 50건)
   → LLM 1차 채점 (importance / novelty / credibility)
   → Weighted RRF로 정밀분석 후보 20건 선정
   → LLM 정밀분석 (요약 + "왜 중요한가")
   → Weighted RRF 최종 랭킹 + 소스별 상한 + 다양성 보장
   → 이메일 발송 + 웹 대시보드 게시
```

**수집 소스**: GeekNews · Hacker News · GitHub(Watch 저장소 + 트렌딩 근사) · arXiv · RSS 블로그 6종

**LLM은 두 단계로 나눠서 쓴다.** 임베딩 필터를 통과한 50건은 저렴한 모델로 1차 채점하고, 그중 상위 20건만 플래그십 모델로 정밀분석한다. 300건 전부에 비싼 모델을 쓰면 비용이 감당이 안 되고, 전부 싼 모델을 쓰면 요약 품질이 떨어진다.

## 랭킹: 왜 Weighted RRF인가

네 가지 신호(relevance / importance / novelty / credibility)를 하나의 순위로 합쳐야 하는데, relevance는 0~1 임베딩 유사도이고 나머지는 LLM이 매긴 1~10점이다. **스케일이 다른 값을 손으로 정한 가중치로 더하면 그 숫자가 실제로 무엇을 의미하는지 알 수 없다.**

그래서 원점수 대신 신호별 순위만 사용하는 RRF(Reciprocal Rank Fusion)를 도입했다.

```python
RRF(d) = Σ w_s / (k + rank_s(d))      # k = 60
```

**다만 동일 가중치로 합치면 오히려 더 나빠진다.** 실측에서 관심사와 전혀 무관한 기후 뉴스(relevance 134개 중 125위)가 동일가중치 RRF에서 14위까지 올라왔다. importance 1위·credibility 상위권이었기 때문인데, RRF는 원점수를 보지 않으므로 relevance가 "네 표 중 한 표"로만 취급된 결과였다.

relevance에 3배 가중치를 줘서 해결했다. 다만 이 값은 **하나의 실패 사례를 기준으로 정한 운영값**이고, 전체 랭킹 품질에서 최적이라는 근거는 아직 없다. Precision@10 / nDCG@10 기반 정량 평가는 다음 과제다.

## 현재 운영 상태

| 항목 | 상태 |
|---|---|
| 배포 | AWS EC2 t3.micro (프리티어), Docker Compose |
| 스케줄 | 호스트 cron, 매일 07:00 KST |
| 전달 | Gmail SMTP + 웹 대시보드 (Caddy + Let's Encrypt TLS) |
| 비용 안전장치 | 일 $2 / 월 $30 상한, 초과 시 파이프라인 자동 중단 |

## 로컬에서 실행하기

```bash
cp .env.example .env        # OPENAI_API_KEY 등 입력
docker compose up -d postgres backend
docker compose run --rm backend alembic upgrade head

# 파이프라인 1회 실행
docker compose run --rm backend python -m app.pipeline.run_pipeline

# 대시보드
open http://localhost:8000/digests
```

Gmail 설정(`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `EMAIL_TO`)이 없으면 발송 대신 콘솔 출력 + `output/{날짜}.txt` 저장으로 대체된다.

## 개인화 설정

코드를 고치지 않고 YAML만 편집한다.

| 파일 | 역할 |
|---|---|
| `config/interests.yaml` | 관심사 카테고리 (high/medium/low). 임베딩 관련도 계산의 기준이자 랭킹 다양성 보장의 기준 |
| `config/sources.yaml` | 수집 소스 (RSS 피드, GitHub Watch 저장소, arXiv 카테고리 등) |
| `config/settings.yaml` | 다이제스트 크기, 소스별 상한, 비용 상한 |

관련도는 `max(카테고리별 코사인 유사도 × tier 가중치)`로 계산한다(high=1.0 / medium=0.5 / low=0.1). low tier는 억제 장치로, "AI 홍보성 뉴스" 같은 카테고리를 여기 두면 그 성격의 글이 상위로 올라오지 못한다.

## 기술 스택

Python 3.11 · FastAPI · SQLAlchemy 2.0 + psycopg3 · PostgreSQL · Alembic · fastembed(ONNX, PyTorch 없이 다국어 임베딩) · OpenAI API · Docker Compose · Caddy

대시보드는 Next.js 대신 FastAPI + Jinja2 서버 사이드 렌더링을 쓴다. EC2 t3.micro의 RAM이 1GB뿐이라 Node 런타임을 얹을 여유가 없었다.

## 문서

- [docs/PRD.md](docs/PRD.md) — 제품 요구사항. 딥 인터뷰 8라운드로 범위를 확정한 기록
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 디렉터리 구조, DB 스키마, 파이프라인 단계, 랭킹 방법론

## 아직 안 한 것

- **랭킹 품질의 정량 평가** — 라벨링된 평가셋 없이 사례 기반으로만 튜닝된 상태
- **RAG 질의 레이어** — 과거 다이제스트에 자연어로 질문하기 (설계만 있음)
- **90일 보존 정책** — 설정값만 있고 실제 삭제 배치는 없음
- **대시보드 검색/필터** — 현재는 날짜별 목록 + 상세 조회만
- **Basic Auth** — 코드에는 있으나 현재 미설정 (자격증명이 비어있으면 통과)
