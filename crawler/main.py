"""
서울·경기 도서관 채용 크롤러 — 진입점

--validate : sources.yaml 검증 + 요약 (+ jobs.json 뼈대)
--crawl    : 실제 수집 → data/jobs.json
  옵션: --limit N (앞 N개 소스만) · --only <문자열> (id/region/district/이름 부분일치)
        --no-details (상세페이지 진입 생략=마감일 미추출, 빠름)
        ※ --limit/--only 부분수집은 jobs.partial.json 에만 쓴다(사이트용 jobs.json 보호).
          정말 덮어쓰려면 --write 를 함께.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import yaml
except ImportError:
    sys.exit("pyyaml 필요: pip install -r requirements.txt")

from . import fetch as fetchmod
from . import parsers
from . import classify

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.yaml"
OUT = ROOT / "docs" / "data" / "jobs.json"   # GitHub Pages(/docs)에서 바로 서빙
# --limit/--only 로 일부만 돌린 결과가 가는 곳. 사이트가 읽는 jobs.json 을 반쪽짜리로
# 덮어써서 나머지 도서관 공고가 통째로 사라지는 사고를 막는다(docs/ 밖 = 배포 안 됨).
PARTIAL_OUT = ROOT / "jobs.partial.json"

REQUIRED = ("id", "region", "district", "name", "parser", "engine", "url")
VALID_REGIONS = {"서울", "경기", "사서교사"}
KST = timezone(timedelta(hours=9))
MAX_CANDS = 30          # 소스당 후보 상한
DETAIL_CAP = 15         # 소스당 상세페이지(마감일) 진입 상한 — 속도 보호.
                        # 8이던 시절 12개 소스가 상한에 걸려 마감일 미확인 공고가
                        # 34건 중 10건이었다(안전만료로 조용히 사라지는 주범).
MAX_PAGES = 3           # 소스당 목록 페이지 상한(1쪽만 보면 2쪽으로 밀린 모집중 공고가 사라짐)
PAGE_LOOKBACK_DAYS = 60 # 이 쪽의 가장 오래된 글이 이보다 오래됐으면 다음 쪽은 볼 필요 없음
# 전체 수집 시간 상한(분). 사이트 다수가 동시에 먹통이면 소스당 최대 1분(httpx 2회+playwright)까지
# 늘어질 수 있어 안전장치를 둔다. 초과하면 남은 소스는 '수집 실패'로 처리 →
# 이전 목록을 그대로 재사용하고 사이트에 배너가 뜨므로 공고가 사라지지는 않는다.
MAX_RUNTIME_MIN = 45
# 도서관 외 업무도 뽑는 '모기관' 게시판(→ 도서관/사서 키워드 필수)
PARENT_HINTS = ("문화재단", "시설", "공단", "문화원", "진흥원", "시청", "구청", "군청")


def load():
    with open(SOURCES, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate(cfg):
    errors, seen = [], set()
    parsers_def = set((cfg.get("parsers") or {}).keys())
    for i, s in enumerate(cfg.get("sources") or []):
        where = s.get("id", f"[index {i}]")
        for key in REQUIRED:
            if not s.get(key):
                errors.append(f"{where}: 필수 필드 누락 '{key}'")
        if s.get("id") in seen:
            errors.append(f"{where}: 중복 id")
        seen.add(s.get("id"))
        if s.get("region") not in VALID_REGIONS:
            errors.append(f"{where}: region '{s.get('region')}' 잘못됨")
        if s.get("parser") and s["parser"] not in parsers_def:
            errors.append(f"{where}: parser '{s['parser']}' 미정의")
        if s.get("engine") not in ("httpx", "playwright"):
            errors.append(f"{where}: engine 은 httpx|playwright")
    return errors


def summarize(cfg):
    src = cfg.get("sources") or []
    print(f"\n총 소스: {len(src)}개")
    for label, key in (("지역", "region"), ("파서", "parser"), ("엔진", "engine")):
        c = Counter(s.get(key) for s in src)
        print(f"  {label:4} " + " · ".join(f"{k}:{v}" for k, v in c.most_common()))


def _now():
    return datetime.now(KST)


def _needs_library_kw(s):
    """도서관/사서 키워드 필수 여부. (URL 도메인 기준 = 이름 오탐 방지)
    - 도서관 자체 도메인(host에 lib/library) 또는 교육청 sen, scope:library → 전수 수집(False)
    - 그 외(시청/군청/구청 게시판, 문화재단, 공단, 사람인, 사서교사 포털) → 키워드 필수(True)
    """
    scope = s.get("scope")
    if scope == "library":
        return False
    if scope == "mixed":
        return True
    if s["parser"] == "sen":
        return False
    host = (urlparse(s["url"]).hostname or "").lower()
    if "library" in host or "lib" in host:
        return False
    return True


def _worth_next_page(html):
    """이 목록 쪽의 가장 오래된 글이 아직 '모집 중일 수 있는' 기간 안이면 다음 쪽도 본다.
    이미 몇 달 전 글까지 내려간 쪽이면 다음 쪽엔 유효 공고가 없으므로 그만 본다(요청 절약).
    날짜를 하나도 못 읽으면 판단 불가 → 한 쪽 더 본다(공고 유실 방지 쪽으로)."""
    dates = sorted(parsers.listing_dates(html))
    if not dates:
        return True
    # 맨 위 고정공지(성북 '채용관련 일반 자격기준' 2021년 등)가 섞여 있어 최솟값을 쓰면
    # 매일 갱신되는 게시판도 '몇 년치'로 오인된다 → 오래된 쪽 1/4은 고정공지로 보고 버림
    oldest = dates[len(dates) // 4]
    cutoff = (_now().date() - timedelta(days=PAGE_LOOKBACK_DAYS)).isoformat()
    return oldest >= cutoff


def _mk_id(source_id, url):
    return source_id + "::" + hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def _expand_url(url):
    """소스 URL의 날짜 자리표시자 치환.
    교육청(sen) 검색 게시판은 URL에 검색기간이 들어가는데, 이게 고정 날짜로 박혀 있으면
    그 날짜 이후 올라온 공고가 검색 결과에서 통째로 빠진다(매일 조금씩 눈이 머는 버그).
      {today}    = 오늘(KST)      {year_ago} = 1년 전"""
    if "{" not in url:
        return url
    today = _now().date()
    return (url.replace("{today}", today.isoformat())
               .replace("{year_ago}", (today - timedelta(days=365)).isoformat()))


def crawl(cfg, limit=None, only=None, details=True, out=OUT):
    settings = cfg["settings"]
    sources = cfg["sources"]
    if only:
        sources = [s for s in sources if only in s["id"] or only in s["region"]
                   or only in s["district"] or only in s["name"]]
    if limit:
        sources = sources[:limit]

    # 예의 크롤링 — sources.yaml 의 delay_seconds 를 그대로 지킨다.
    # (집 IP로 도는 크롤러라 과다요청으로 차단되면 크롤러뿐 아니라 가정 접속까지 막힘)
    delay = settings.get("request", {}).get("delay_seconds", 2)
    started = time.monotonic()
    jobs, health, results = [], [], 0

    # 직전 수집분 로드(수집 실패 시 재사용 · isNew 비교용)
    prev_jobs = []
    if OUT.exists():
        try:
            prev_jobs = json.load(open(OUT, encoding="utf-8-sig")).get("jobs", [])
        except Exception:
            pass
    # 비교는 모두 '정규 URL'(목록·검색 문맥 파라미터 제거) 기준 —
    # 게시판이 링크에 붙이는 문맥값이 바뀌어도 같은 공고로 인식된다(광명 bvLib 사례).
    prev_by_sid = {}
    prev_urls = {parsers.canon_url(j.get("url") or "") for j in prev_jobs}
    for j in prev_jobs:
        prev_by_sid.setdefault(j.get("sid"), []).append(j)
    # 게시일을 못 얻은 공고의 안전만료 기준일 = '처음 수집한 날'.
    # (firstSeen 없던 시절 데이터는 scrapedAt으로 보정)
    prev_first = {parsers.canon_url(j.get("url") or ""):
                  (j.get("firstSeen") or (j.get("scrapedAt") or "")[:10])
                  for j in prev_jobs}
    today_str = _now().date().isoformat()

    def _live(j):   # 만료 안 된(아직 유효한) 공고인지
        dl = j.get("deadline")
        if classify.is_expired(dl):
            return False
        if dl is None and classify.is_safety_expired(j.get("posted") or j.get("firstSeen"), settings):
            return False
        return True

    failures = []   # [{"id","name","reason"}] — fetch=접속실패 · empty=구조변경 의심
    # 사각지대 진단: 후보 0건인데 목록엔 진짜 공고 행이 보이는 소스.
    # (기존 '구조변경 의심'은 직전에 유효 공고가 있던 소스만 잡아서, 한 번도 못 잡아본
    #  소스는 영원히 조용히 0건이었다 — 군포·송파·노원·금천·성동이 그 사각지대였다.)
    unreadable = []
    capped = []     # 상세 진입 상한(DETAIL_CAP)에 걸린 소스 — 마감일 미확인 공고 발생 가능
    dropped = []    # 어제는 유효 공고가 있었는데 오늘 0건이 된 소스 — 조용한 유실 조기경보

    for i, s in enumerate(sources):
        # 시간 상한 초과: 남은 소스는 손대지 않고 '수집 실패'로 넘겨 이전 목록을 재사용시킨다
        # (중간에 그냥 끊으면 남은 도서관들의 공고가 그날 통째로 사라짐)
        if time.monotonic() - started > MAX_RUNTIME_MIN * 60:
            for rest in sources[i:]:
                failures.append({"id": rest["id"], "name": rest["name"], "reason": "timeout"})
                health.append((rest["id"], "SKIPPED", f"시간상한 {MAX_RUNTIME_MIN}분 초과 — 이전 목록 재사용"))
            print(f"\n⏱ 시간상한 {MAX_RUNTIME_MIN}분 초과 — 남은 {len(sources) - i}곳은 이전 목록으로 대체")
            break

        tag = f"[{s['region']}/{s['district']}] {s['name']}"
        src_url = _expand_url(s["url"])   # 검색기간 등 날짜 자리표시자를 오늘 기준으로
        try:
            html = fetchmod.fetch(src_url, s["engine"], settings)
        except Exception as e:
            health.append((s["id"], "FETCH_FAIL", str(e)[:70]))
            failures.append({"id": s["id"], "name": s["name"], "reason": "fetch"})
            print(f"  ✗ {tag}: fetch 실패 ({str(e)[:50]})")
            continue

        is_saramin = s["parser"] == "saramin"

        api = s.get("api")   # JSON 목록 API 소스(화면에 링크가 없는 Vue 게시판)

        def _parse(page_html, page_url):
            if is_saramin:   # 사람인 전용: 지역·마감일(D-day)을 목록에서 정확히 추출, 서울·경기만
                return parsers.extract_saramin(page_html, page_url, _now().date())
            if api:
                return parsers.extract_json_listings(page_html, api)
            return parsers.extract_listings(page_html, page_url)

        cands = _parse(html, src_url)
        # httpx가 JS 목록보드(bbsPostList 등)의 행을 못 읽어 0건이면 playwright로 재렌더 후 재시도
        if not cands and not is_saramin and s["engine"] == "httpx" \
                and parsers._detect_bbspost_detail(html):
            try:
                html = fetchmod.fetch(src_url, "playwright", settings)
                cands = _parse(html, src_url)
            except Exception:
                pass

        # 2쪽 이후: 한 쪽 건수가 적은 게시판은 아직 모집 중인 공고가 다음 쪽으로 밀린다.
        # 이 쪽이 이미 오래된 글까지 내려갔거나 새 후보가 안 나오면 즉시 중단(요청 절약).
        seen_cand = {c["url"] for c in cands}
        pages_used, page_urls = 1, {src_url}
        max_pages = s.get("pages", MAX_PAGES)
        while (len(cands) < MAX_CANDS and pages_used < max_pages
               and _worth_next_page(html)):
            nxt = (parsers.api_next_page(src_url, api, pages_used + 1) if api
                   else parsers.next_page_url(html, src_url, pages_used + 1))
            if not nxt or nxt in page_urls:
                break
            page_urls.add(nxt)
            time.sleep(delay)
            try:
                html = fetchmod.fetch(nxt, s["engine"], settings)
            except Exception:
                break
            pages_used += 1
            fresh = [c for c in _parse(html, nxt) if c["url"] not in seen_cand]
            if not fresh:       # 같은 쪽이 다시 왔거나 더 볼 게 없음
                break
            seen_cand.update(c["url"] for c in fresh)
            cands += fresh
        cands = cands[:MAX_CANDS]
        kept = 0
        details_used = 0
        for c in cands:
            title = c["title"]
            if classify.is_result_post(title, settings):
                results += 1
                continue
            if _needs_library_kw(s) and not (
                classify.is_library_relevant(title, settings) or "사서" in title or "도서관" in title):
                continue

            deadline = c.get("deadline")   # 사람인은 D-day에서 이미 확보
            # ── 상세 진입 '전에' 제목만으로 확정되는 만료를 먼저 걸러낸다.
            # 예전엔 상세를 먼저 열어보고 나서 만료 판정을 해서, 이미 끝난 공고가
            # 상세 진입 상한(DETAIL_CAP)을 다 써버리고 정작 살아있는 공고는
            # 마감일을 못 읽은 채 안전만료로 조용히 사라졌다.
            appoint = parsers.appointment_date_from_title(title)
            if appoint and classify.is_expired(appoint):
                continue   # 임용일이 지남 = 접수는 확실히 종료
            title_dl = parsers.deadline_from_title(title, c["posted"], _now().year)
            if deadline is None and classify.is_expired(title_dl):
                continue   # 제목에 박힌 마감일이 이미 지남

            open_start = None
            # API 소스의 상세 화면은 Vue 껍데기라 httpx로 열어도 본문이 없다 → 상세 진입 생략
            if deadline is None and details and not is_saramin and not api \
                    and details_used < DETAIL_CAP:
                try:
                    dhtml = fetchmod.fetch(c["url"], s["engine"], settings)
                    deadline = parsers.extract_deadline(dhtml)
                    open_start = parsers.extract_apply_start(dhtml)
                    if c["posted"] is None:   # 목록에서 게시일 못 얻었으면 상세에서 보조
                        c["posted"] = parsers.extract_posted(dhtml)
                except Exception:
                    pass
                details_used += 1
                time.sleep(delay)

            # 상세에서 마감일을 못 읽었으면 제목에 박힌 마감일('~8/28까지', '(~8.17)')로 보조 판정
            if deadline is None:
                deadline = title_dl

            if classify.is_expired(deadline):
                continue
            # 게시일이 없으면 '처음 수집한 날'을 안전만료 기준으로 —
            # 날짜가 하나도 없는 항목이 목록에 영구히 남는 것 방지(오수집·재게시 잔류 차단)
            canon = parsers.canon_url(c["url"])
            first_seen = prev_first.get(canon) or today_str
            if deadline is None and classify.is_safety_expired(c["posted"] or first_seen, settings):
                continue

            # 접수 시작일이 미래면 '접수예정'
            job_status = "upcoming" if (open_start and open_start > _now().date().isoformat()) else "open"

            jobs.append({
                "id": _mk_id(s["id"], canon),
                "sid": s["id"],
                "region": c.get("region", s["region"]), "district": c.get("district", s["district"]),
                "source": s["name"], "title": title,
                "jobType": classify.tag_jobtype(title, settings),
                "posted": c["posted"], "deadline": deadline, "url": c["url"],
                "firstSeen": first_seen, "status": job_status,
                "scrapedAt": _now().isoformat(timespec="seconds"),
            })
            kept += 1

        status = "OK" if kept else "ZERO"
        # 상세 진입 상한에 걸린 소스 = 마감일을 못 읽은 공고가 생겼을 수 있다.
        # (마감일 None 은 게시 N일 뒤 안전만료로 조용히 사라지므로 유실의 주범)
        if details_used >= DETAIL_CAP:
            capped.append({"id": s["id"], "name": s["name"], "cands": len(cands)})
            health.append((s["id"], "DETAIL_CAP",
                           f"상세 진입 {DETAIL_CAP}건 상한 도달 — 나머지는 마감일 미확인"))
        health.append((s["id"], status, f"cands={len(cands)} kept={kept} pages={pages_used}"))
        print(f"  {'✓' if kept else '·'} {tag}: {kept}건" + (f" (후보 {len(cands)})" if not kept and cands else ""))
        # '공고 없음'인가 '못 읽음'인가 — 목록에 보이는 공고 행 수와 후보 수를 비교한다.
        # 0건만 보면 '절반 넘게 못 읽는' 부분 실패(의정부·안성 등)를 놓치므로 비율로 본다.
        # 도서관 키워드가 필수인 소스(시청 통합 게시판 등)는 도서관 공고 행만 세어 비교한다
        lib_kw = (tuple(settings.get("keywords_include", [])) + ("사서", "도서관")
                  if _needs_library_kw(s) else None)
        rows = parsers.suspect_rows(html, lib_kw)
        if rows and len(cands) * 2 < len(rows):
            unreadable.append({"id": s["id"], "name": s["name"],
                               "cands": len(cands), "rows": len(rows), "samples": rows[:3]})
            health.append((s["id"], "UNREADABLE",
                           f"목록엔 공고 행 {len(rows)}개인데 후보 {len(cands)}개 — 파서 점검 필요"))
            print(f"    ⚠ 목록엔 공고 {len(rows)}행인데 후보 {len(cands)}건 — 파서 점검 필요")
        # 접속은 됐으나 후보 0건인데 직전엔 유효 공고가 있었으면 = 홈페이지 구조 변경 의심
        prev_live = sum(1 for j in prev_by_sid.get(s["id"], []) if _live(j))
        if len(cands) == 0 and prev_live:
            failures.append({"id": s["id"], "name": s["name"], "reason": "empty"})
        # 후보는 나오는데 최종 수집이 0이 된 경우 — 위 '구조변경 의심'이 못 잡는 사각지대다.
        # (제목 형식이 바뀌어 필터에 걸리거나, 마감일 오독으로 전부 만료 처리된 경우)
        elif kept == 0 and prev_live:
            dropped.append({"id": s["id"], "name": s["name"],
                            "prev": prev_live, "cands": len(cands)})
        time.sleep(delay)

    # 수집 실패/구조변경 의심 소스: 직전 수집분(아직 유효한 것)을 임시로 그대로 재사용
    failed_ids = {f["id"] for f in failures}
    reused = 0
    for fid in failed_ids:
        for j in prev_by_sid.get(fid, []):
            if not _live(j):
                continue
            j = dict(j); j["stale"] = True   # '이전 수집분 임시표시' 플래그
            jobs.append(j); reused += 1

    # 중복 제거: ①같은 url ②같은 소스+완전히 동일한 제목(공백무시)의 재게시
    #   ※ 임용일·날짜 등이 달라 제목이 다르면 별개 공고로 유지(영등포 블라인드 채용 등)
    uniq, seen_url, seen_st, seen_cross = [], set(), set(), set()
    for j in sorted(jobs, key=lambda x: (x["posted"] or x["deadline"] or ""), reverse=True):
        cu = parsers.canon_url(j["url"])
        if cu in seen_url:
            continue
        norm = re.sub(r"\s+", "", j["title"])
        st = (j["source"], norm)
        if st in seen_st:
            continue
        # ③ 소스는 다르지만 같은 공고 — 한 공고가 도서관과 시청 양쪽에 실리는 경우
        #    (부천시립도서관 ↔ 부천시청). 제목이 충분히 길고 마감일까지 같을 때만 묶는다:
        #    '기간제근로자 채용 공고' 같은 짧고 흔한 제목이 잘못 합쳐지면 진짜 공고가 사라진다.
        if j["deadline"] and len(norm) >= 20:
            if (norm, j["deadline"]) in seen_cross:
                continue
            seen_cross.add((norm, j["deadline"]))
        seen_url.add(cu); seen_st.add(st); uniq.append(j)

    fetchmod.close()

    # 직전 수집분과 비교해 '당일 신규'(isNew) 표시 — 어제 없던 URL만(재사용분은 자동 False)
    for j in uniq:
        j["isNew"] = bool(prev_urls) and (parsers.canon_url(j["url"]) not in prev_urls)

    fail_names = sorted({f["name"] for f in failures})
    if fail_names:
        print(f"\n⚠ 수집 실패/구조변경 의심 {len(fail_names)}곳(이전 목록 {reused}건 재사용): " + ", ".join(fail_names))
    if unreadable:
        # 이모지(4바이트)는 NAS 콘솔에서 깨져서 로그를 못 읽는다 → 대괄호 표기로
        print(f"\n[파서 점검 필요] {len(unreadable)}곳 — 목록에 보이는 공고를 제대로 못 읽음:")
        for u in unreadable:
            print(f"  · {u['name']} ({u['id']}) — 공고 행 {u['rows']}개 중 후보 {u['cands']}건")
            for smp in u["samples"]:
                print(f"      {smp}")
    if dropped:
        print(f"\n[수집 급감] {len(dropped)}곳 — 어제는 유효 공고가 있었는데 오늘 0건:")
        for d in dropped:
            print(f"  · {d['name']} ({d['id']}) — 어제 {d['prev']}건 · 오늘 후보 {d['cands']}건 수집 0")
    nodl = [j for j in uniq if not j.get("deadline")]
    if nodl or capped:
        print(f"\n[마감일 미확인] {len(nodl)}건 / 전체 {len(uniq)}건"
              + (f" · 상세 진입 상한({DETAIL_CAP}) 도달 소스 {len(capped)}곳" if capped else ""))
        for c in capped:
            print(f"  · {c['name']} (후보 {c['cands']}건)")
    write(cfg, uniq, fail_names, out=out, unreadable=unreadable)
    _report(health, len(uniq), results)


def write(cfg, jobs, failures=None, out=OUT, unreadable=None):
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "project": cfg.get("meta", {}).get("project", ""),
            "collected_at": _now().strftime("%Y-%m-%d %H:%M"),
            "job_count": len(jobs),
            "failures": failures or [],
            # 사이트 배너에는 안 쓴다(‘수집 실패’와 성격이 다름). 점검용 기록.
            "unreadable": [u["name"] for u in (unreadable or [])],
        },
        "jobs": jobs,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n{out.name} 작성: {out} (공고 {len(jobs)}건)")


def _report(health, jobcount, results):
    ok = sum(1 for _, s, _ in health if s == "OK")
    zero = sum(1 for _, s, _ in health if s == "ZERO")
    fail = sum(1 for _, s, _ in health if s == "FETCH_FAIL")
    skipped = sum(1 for _, s, _ in health if s == "SKIPPED")
    # 한 소스가 여러 줄(예: DETAIL_CAP + ZERO)을 남기므로 줄 수가 아니라 소스 id 수로 센다
    print(f"\n=== 건강검진 ===  소스 {len({sid for sid, _, _ in health})}"
          f" · 성공 {ok} · 0건 {zero} · fetch실패 {fail}"
          + (f" · 시간초과 건너뜀 {skipped}" if skipped else "")
          + f" · 결과공고제외 {results}")
    for sid, st, msg in health:
        if st != "OK":
            print(f"  [{st}] {sid} — {msg}")


def write_skeleton(cfg):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"project": cfg.get("meta", {}).get("project", ""),
                        "collected_at": _now().strftime("%Y-%m-%d %H:%M"),
                        "job_count": 0, "status": "skeleton"}, "jobs": []}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\njobs.json 뼈대 생성: {OUT}")


def main():
    ap = argparse.ArgumentParser(description="도서관 채용 크롤러")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--crawl", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", type=str)
    ap.add_argument("--no-details", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="--limit/--only 부분수집 결과로 docs/data/jobs.json 을 덮어쓴다(위험)")
    args = ap.parse_args()

    cfg = load()
    errors = validate(cfg)
    if errors:
        print("검증 실패:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("검증 통과 [OK]")

    if args.crawl:
        summarize(cfg)
        # 일부 소스만 돌린 결과를 jobs.json 에 쓰면 나머지 도서관 공고가 전부 날아간다.
        # (재사용 안전장치는 '수집 실패' 소스만 살리므로, 아예 안 돈 소스는 못 살린다)
        out = OUT
        if (args.limit or args.only) and not args.write:
            out = PARTIAL_OUT
            print(f"\n※ 부분 수집(--limit/--only) — 사이트용 {OUT.name} 은 건드리지 않고"
                  f" {PARTIAL_OUT.name} 에만 씁니다. 정말 덮어쓰려면 --write 를 추가하세요.")
        print("\n=== 수집 시작 ===")
        crawl(cfg, limit=args.limit, only=args.only, details=not args.no_details, out=out)
    else:
        summarize(cfg)
        if not OUT.exists():
            write_skeleton(cfg)   # 실데이터가 있으면 덮어쓰지 않음
        else:
            print("\n(기존 jobs.json 유지 — 수집은 --crawl)")


if __name__ == "__main__":
    main()
