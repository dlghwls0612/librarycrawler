"""
서울·경기 도서관 채용 크롤러 — 진입점

--validate : sources.yaml 검증 + 요약 (+ jobs.json 뼈대)
--crawl    : 실제 수집 → data/jobs.json
  옵션: --limit N (앞 N개 소스만) · --only <문자열> (id/region/district/이름 부분일치)
        --no-details (상세페이지 진입 생략=마감일 미추출, 빠름)
"""
import argparse
import hashlib
import json
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

REQUIRED = ("id", "region", "district", "name", "parser", "engine", "url")
VALID_REGIONS = {"서울", "경기", "사서교사"}
KST = timezone(timedelta(hours=9))
MAX_CANDS = 30          # 소스당 후보 상한
DETAIL_CAP = 8          # 소스당 상세페이지(마감일) 진입 상한 — 속도 보호
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


def _mk_id(source_id, url):
    return source_id + "::" + hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def crawl(cfg, limit=None, only=None, details=True):
    settings = cfg["settings"]
    sources = cfg["sources"]
    if only:
        sources = [s for s in sources if only in s["id"] or only in s["region"]
                   or only in s["district"] or only in s["name"]]
    if limit:
        sources = sources[:limit]

    delay = min(settings.get("request", {}).get("delay_seconds", 2), 1)
    jobs, health, results = [], [], 0

    for s in sources:
        tag = f"[{s['region']}/{s['district']}] {s['name']}"
        try:
            html = fetchmod.fetch(s["url"], s["engine"], settings)
        except Exception as e:
            health.append((s["id"], "FETCH_FAIL", str(e)[:70]))
            print(f"  ✗ {tag}: fetch 실패 ({str(e)[:50]})")
            continue

        cands = parsers.extract_listings(html, s["url"])[:MAX_CANDS]
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

            deadline = None
            if details and details_used < DETAIL_CAP:
                try:
                    dhtml = fetchmod.fetch(c["url"], s["engine"], settings)
                    deadline = parsers.extract_deadline(dhtml)
                except Exception:
                    pass
                details_used += 1
                time.sleep(delay)

            # 상세에서 마감일을 못 읽었으면 제목에 박힌 마감일('~8/28까지')로 보조 판정
            if deadline is None:
                deadline = parsers.deadline_from_title(title, c["posted"])

            if classify.is_expired(deadline):
                continue
            if deadline is None and classify.is_safety_expired(c["posted"], settings):
                continue

            jobs.append({
                "id": _mk_id(s["id"], c["url"]),
                "region": s["region"], "district": s["district"], "source": s["name"],
                "title": title, "jobType": classify.tag_jobtype(title, settings),
                "posted": c["posted"], "deadline": deadline, "url": c["url"],
                "scrapedAt": _now().isoformat(timespec="seconds"),
            })
            kept += 1

        status = "OK" if kept else "ZERO"
        health.append((s["id"], status, f"{kept} jobs"))
        print(f"  {'✓' if kept else '·'} {tag}: {kept}건")
        time.sleep(delay)

    # 중복 제거(같은 url)
    uniq, seen = [], set()
    for j in sorted(jobs, key=lambda x: (x["posted"] or ""), reverse=True):
        if j["url"] in seen:
            continue
        seen.add(j["url"]); uniq.append(j)

    fetchmod.close()

    # 직전 수집분과 비교해 '당일 신규'(isNew) 표시 — 어제 없던 URL만
    prev_urls = set()
    if OUT.exists():
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            prev_urls = {j.get("url") for j in old.get("jobs", [])}
        except Exception:
            pass
    for j in uniq:
        j["isNew"] = bool(prev_urls) and (j["url"] not in prev_urls)

    write(cfg, uniq)
    _report(health, len(uniq), results)


def write(cfg, jobs):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "project": cfg.get("meta", {}).get("project", ""),
            "collected_at": _now().strftime("%Y-%m-%d %H:%M"),
            "job_count": len(jobs),
        },
        "jobs": jobs,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\njobs.json 작성: {OUT} (공고 {len(jobs)}건)")


def _report(health, jobcount, results):
    ok = sum(1 for _, s, _ in health if s == "OK")
    zero = sum(1 for _, s, _ in health if s == "ZERO")
    fail = sum(1 for _, s, _ in health if s == "FETCH_FAIL")
    print(f"\n=== 건강검진 ===  소스 {len(health)} · 성공 {ok} · 0건 {zero} · fetch실패 {fail} · 결과공고제외 {results}")
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
        print("\n=== 수집 시작 ===")
        crawl(cfg, limit=args.limit, only=args.only, details=not args.no_details)
    else:
        summarize(cfg)
        if not OUT.exists():
            write_skeleton(cfg)   # 실데이터가 있으면 덮어쓰지 않음
        else:
            print("\n(기존 jobs.json 유지 — 수집은 --crawl)")


if __name__ == "__main__":
    main()
