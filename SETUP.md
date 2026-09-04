# GitHub 셋업 가이드 (매일 자동 수집 + 무료 웹사이트)

비개발자 기준, 마우스 위주로 따라할 수 있게 정리했어요. 약 10분.

> 핵심 결과: `https://<내아이디>.github.io/<레포이름>/` 주소로 사이트가 뜨고,
> **매일 새벽 5시(KST) 자동으로 공고가 갱신**됩니다. 비용 0원.

---

## 1) 레포(저장소) 만들기 — **반드시 Public**
1. github.com 로그인 → 우상단 **+** → **New repository**
2. Repository name: 예) `library-jobs`
3. **Public 선택** (중요! Public이어야 Actions 무제한 + Pages 무료)
4. **Create repository**

> Private로 하면 자동수집 시간이 제한되고 Pages가 유료라 꼭 Public.

## 2) 파일 올리기
`library-crawler` 폴더 안의 **모든 파일/폴더**를 올립니다.
- 방법 A (쉬움): 레포 첫 화면 → **uploading an existing file** → `library-crawler` 안의 내용물을 통째로 드래그 → **Commit changes**
  - 폴더 구조가 유지되도록 **폴더째** 끌어다 놓으세요. 특히 숨은 폴더 `.github/workflows/crawl.yml` 가 꼭 포함돼야 합니다.
- 방법 B (git 익숙하면): `git init && git add . && git commit -m init && git remote add origin ... && git push`

올린 뒤 레포에 이런 게 보이면 정상: `crawler/`, `docs/`, `sources.yaml`, `requirements.txt`, `.github/workflows/crawl.yml`

## 3) Actions 쓰기 권한 켜기 (커밋 허용)
1. 레포 **Settings** → 좌측 **Actions** → **General**
2. 맨 아래 **Workflow permissions** → **Read and write permissions** 선택 → **Save**

> 이게 있어야 자동수집 결과(jobs.json)를 레포에 저장할 수 있어요.

## 4) 웹사이트(Pages) 켜기
1. **Settings** → 좌측 **Pages**
2. **Source**: *Deploy from a branch*
3. **Branch**: `main` / 폴더 **`/docs`** 선택 → **Save**
4. 1~2분 뒤 상단에 사이트 주소가 뜹니다: `https://<내아이디>.github.io/<레포이름>/`
   - 지금은 올려둔 스냅샷(38건)이 보여요. 다음 단계에서 최신으로 갱신됩니다.

## 5) 첫 자동수집 실행 (수동 버튼)
1. 상단 **Actions** 탭 → (처음이면) *I understand my workflows, enable them* 클릭
2. 왼쪽 **도서관 채용 크롤 (매일)** → 오른쪽 **Run workflow** → **Run workflow**
3. 10~20분 기다리기 (Playwright 설치 + 99개 소스 수집). 초록 체크 뜨면 성공
4. `docs/data/jobs.json` 이 갱신되고, 잠시 뒤 사이트에 **최신 공고**가 반영됩니다
   - 이때 경기·사서교사 등 Playwright 필요 소스까지 채워져요

## 6) 끝! 이후는 자동
- 매일 **새벽 5시(KST)** 자동 실행 → 사이트 자동 갱신
- 언제든 Actions 탭에서 **Run workflow**로 즉시 갱신 가능

---

## 문제가 생기면
- **Actions 빨간 X**: 로그 열어 어느 단계인지 확인. 대개 `pip install` 또는 `playwright install` 네트워크 일시 오류 → 재실행(Run workflow)로 해결
- **사이트가 404**: Pages 설정에서 폴더가 `/docs` 인지 확인, 1~2분 대기
- **jobs.json이 안 바뀜**: 3) 권한(Read and write) 확인
- **특정 도서관이 안 뜸**: 그 사이트가 개편됐을 수 있음 → 나에게 "○○ 크롤러 고쳐줘" 하면 sources.yaml 한 줄 수정으로 해결

## 로컬에서 미리 돌려보기(선택)
```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m crawler.main --crawl        # docs/data/jobs.json 생성
# docs/index.html 을 브라우저로 열면 확인 (혹은 python -m http.server 로 docs 서빙)
```
