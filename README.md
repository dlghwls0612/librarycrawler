# 서울·경기 도서관 채용 크롤러

서울·경기 공공/구립/전문/교육청 공공도서관과 사서교사 채용공고를 매일 자동 수집해
하나의 `jobs.json`으로 합치고, 정적 웹사이트에서 지역별로 보여준다.

## 구조
```
library-crawler/
├─ sources.yaml          # 수집 대상 채널 설정 (파서=코드가 아니라 데이터)
├─ requirements.txt
├─ SETUP.md              # GitHub 셋업 가이드(비개발자용)
├─ crawler/
│  ├─ main.py            # 진입점: sources.yaml 로드 → 수집 → docs/data/jobs.json
│  ├─ fetch.py           # httpx / playwright 봇우회 fetch
│  ├─ parsers.py         # 범용 목록·상세 파서(키워드+날짜 휴리스틱)
│  └─ classify.py        # 키워드 필터 · 고용형태 태깅 · 마감판정
├─ docs/                 # ← GitHub Pages(/docs)가 서빙하는 폴더
│  ├─ index.html         # 최종 사이트(서울/경기/사서교사 3탭)
│  └─ data/jobs.json     # 수집 결과 (사이트가 읽음, Actions가 매일 갱신)
├─ site/index.html       # docs/index.html 원본(개발용 사본)
└─ .github/workflows/
   └─ crawl.yml          # 매일 자동 수집 + 커밋
```

## 설계 원칙
- **파서는 설정(sources.yaml)** — 사이트 개편 시 코드가 아니라 한 항목만 수정
- **list → detail 2단계** — 목록에서 각 게시글에 직접 진입해 본문 마감일 추출 + 딥링크
- **4단계 마감판정** — ①본문 마감일 ②합격자/결과 공고 매칭 ③원문 사라짐 ④안전만료 25일
- **봇 우회** — Playwright 헤드리스 + 실제 브라우저 헤더, 요청 간격, 재시도
- **예의 크롤링** — robots 존중, 요청 간격, 원문 링크로 트래픽 환원

## 단계
- [x] 0단계: 프로젝트 뼈대
- [x] 1단계: sources.yaml (약 100개 채널)
- [ ] 2단계: 크롤러 코어 (fetch/parsers/classify)
- [ ] 3단계: 표시 사이트 연동 (서울/경기/사서교사 3탭)
- [ ] 4단계: GitHub Actions 매일 자동 실행

## 로컬 실행(뼈대 검증)
```bash
pip install -r requirements.txt
python -m crawler.main --validate   # sources.yaml 검증 + 요약 출력
python -m crawler.main --dry-run    # (2단계 이후) 실제 수집
```
