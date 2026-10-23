"""records — 기록관리(기록연구사·기록물관리전문요원·아키비스트) 채용 수집.

원래 별도 저장소 archivescrawler 가 따로 돌리던 수집기를 그대로 옮겨 왔다
(karma·alio·saramin·classify·parsers 는 그쪽 코드 원본 사본, 설정은 저장소 루트의 records.yaml).
도서관 수집기(main.py)가 같은 실행에서 함께 돌리고, 결과는 사이트 형식으로 바꿔
region="기록관리" 탭에 넣는다. 그래서 도서관 쪽의 안전장치(수집 실패 시 이전 목록 재사용 ·
NEW 표시 · 안전만료 · 건강검진)를 기록관리 공고도 그대로 받는다.
"""
import re
from pathlib import Path

import yaml

from . import karma, alio, saramin
from . import classify as rclassify

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "records.yaml"
REGION = "기록관리"   # 사이트 탭 이름이자 jobs.json 의 region 값

# (sid, 사이트 '수집 실패' 배너에 쓸 이름, 수집 함수)
STEPS = (
    ("records-karma", "기록관리·한국기록전문가협회", lambda st: karma.crawl_karma(st, max_pages=3)),
    ("records-alio", "기록관리·잡알리오", lambda st: alio.crawl_alio(st)),
    ("records-saramin", "기록관리·사람인", lambda st: saramin.crawl_saramin(st)),
)
# 같은 공고가 여러 곳에 올라오면 남길 쪽: 협회 공식 > 공공기관 > 사람인 (archivescrawler 와 동일)
_PRIORITY = {"한국기록전문가협회": 1, "잡알리오": 2, "사람인": 3}


def load_settings():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f).get("settings", {})


def _stable_url(url):
    """사람인 검색결과 링크에는 검색할 때마다 바뀌는 search_uuid 등이 붙는다. 그대로 두면
    같은 공고가 매일 'NEW'로 뜨고 안전만료 기준일(firstSeen)도 매일 초기화된다
    → 도서관 탭의 사람인 링크와 같은 짧은 형태(rec_idx 만)로 고정."""
    m = re.search(r"saramin\.co\.kr/.*[?&]rec_idx=(\d+)", url)
    if m:
        return f"https://www.saramin.co.kr/zf_user/jobs/relay/view?view_type=list&rec_idx={m.group(1)}"
    return url


def to_site(a, sid):
    """archivescrawler 형식 공고 1건 → 도서관 사이트 형식(id·firstSeen 등은 main 이 채움)."""
    return {
        "sid": sid,
        "region": REGION,
        "district": a.get("region") or "지역 미상",     # 탭 안의 칩 = 서울/경기
        "source": a.get("organization") or a.get("source", ""),
        "via": a.get("source", ""),                     # 어디서 찾았나(협회·잡알리오·사람인)
        "title": a.get("title", ""),
        "jobType": a.get("jobtype") or "계약",          # 정규·계약·임기제·인턴
        "spec": bool(a.get("is_specialist")),           # 전문요원 자격 필수
        "posted": a.get("start_date"),
        "deadline": a.get("deadline"),
        "url": _stable_url(a.get("url", "")),
        "status": "upcoming" if a.get("status") == "접수예정" else "open",
    }


def collect(settings):
    """세 곳을 차례로 수집. 반환: [(sid, name, jobs)] — jobs 가 None 이면 예외로 수집 자체가 실패.
    이미 마감된 공고(협회·잡알리오는 최근 마감분도 돌려준다)는 여기서 뺀다 — 사이트는 열린 공고만."""
    out = []
    for sid, name, fn in STEPS:
        try:
            raw = fn(settings)
        except Exception as e:
            print(f"  ✗ [기록관리] {name}: 수집 실패 ({str(e)[:50]})")
            out.append((sid, name, None))
            continue
        live = [a for a in raw
                if a.get("status") != "마감" and not rclassify.is_expired(a.get("deadline"))]
        out.append((sid, name, [to_site(a, sid) for a in live]))
    return out


def deduplicate(jobs):
    """기관명+제목 앞부분이 같으면 1건으로 (archivescrawler 의 규칙 그대로, 출처 우선순위 적용).
    도서관 쪽 dedup 과 섞지 않는 이유: 같은 사람인 공고가 도서관 탭에도 있으면 그쪽에 흡수돼
    기록관리 탭에서 사라진다."""
    seen, out = set(), []
    for j in sorted(jobs, key=lambda x: _PRIORITY.get(x.get("via"), 99)):
        key = ("".join(c for c in j["source"] if c.isalnum())[:10] + ":"
               + "".join(c for c in j["title"] if c.isalnum())[:25])
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out
