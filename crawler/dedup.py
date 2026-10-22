"""crawler/dedup.py — 지능형 중복 공고 감지 및 마스터-미러 구조화 엔진.

1단계: 동일 자치구(district) + 동일 고용형태(jobType) + 등록일(posted) 윈도우 1차 압축
2단계: 불용어 제외 핵심 토큰 집합 기반 자카드(Jaccard) & 포함율(Containment) 정밀 유사도 검증
3단계: 원천 기관을 마스터(Master)로 지정하고 포털/재단 링크를 aliases(복수 출처)로 병합
"""
import re
from datetime import datetime
from . import parsers

STOPWORDS = {
    "도서관", "채용", "공고", "모집", "직원", "근로자", "선발", "안내", "및",
    "사업소", "문화재단", "시설관리공단", "도시관리공단", "구립도서관", "시립도서관", "통합",
    "공개채용", "공개모집", "채용공고"
}


def normalize_token(w: str) -> str:
    """토큰 정규화: '2026년도' -> '2026', '제1회' -> '1회'"""
    w = re.sub(r"^(20\d{2})년(?:도)?$", r"\1", w)
    w = re.sub(r"^제(\d+)회$", r"\1회", w)
    return w


def extract_tokens(title: str) -> set:
    """제목에서 불용어를 제외한 고유 핵심 형태소 토큰 집합 추출"""
    if not title:
        return set()
    words = re.findall(r"[가-힣a-zA-Z0-9]+", title)
    tokens = set()
    for w in words:
        norm = normalize_token(w)
        if norm not in STOPWORDS and len(norm) > 1:
            tokens.add(norm)
    return tokens


def are_duplicate_jobs(j1: dict, j2: dict) -> bool:
    """두 공고가 동일한 채용 건인지 판정하는 3단계 검증 규칙."""
    # 1. URL이 정규화 기준으로 동일하면 즉시 중복
    if parsers.canon_url(j1["url"]) == parsers.canon_url(j2["url"]):
        return True

    t1_norm = re.sub(r"\s+", "", j1["title"])
    t2_norm = re.sub(r"\s+", "", j2["title"])

    # 2. 동일 소스(같은 사이트)인 경우:
    # 제목이 완전히 동일할 때만 중복 (노원문화재단의 월계/어린이/중계도서관 등 분관별 정상 공고 보호)
    if j1.get("sid") == j2.get("sid") or j1.get("source") == j2.get("source"):
        return t1_norm == t2_norm

    # 3. 교차 소스(Cross-Source, 서로 다른 사이트에서 수집된 경우)의 지능형 판정:
    # (1) 동일 자치구/지역 조건
    d1, d2 = j1.get("district"), j2.get("district")
    r1, r2 = j1.get("region"), j2.get("region")
    same_place = (d1 == d2) or (r1 == r2 and (d1 == "전역·통합" or d2 == "전역·통합"))
    if not same_place:
        return False

    # (2) 고용 형태(jobType)가 명시되어 있고 서로 다르면 별개 공고 (정규 vs 계약 vs 알바)
    if j1.get("jobType") and j2.get("jobType") and j1["jobType"] != j2["jobType"]:
        return False

    # (3) 일정 유사성: 마감일이 둘 다 있으면 반드시 일치해야 함
    if j1.get("deadline") and j2.get("deadline"):
        if j1["deadline"] != j2["deadline"]:
            return False
    elif j1.get("posted") and j2.get("posted"):
        try:
            d1_dt = datetime.strptime(j1["posted"][:10], "%Y-%m-%d")
            d2_dt = datetime.strptime(j2["posted"][:10], "%Y-%m-%d")
            if abs((d1_dt - d2_dt).days) > 5:
                return False
        except Exception:
            pass

    # (4) 핵심 토큰 유사도 검증
    s1 = extract_tokens(j1["title"])
    s2 = extract_tokens(j2["title"])
    if not s1 or not s2:
        return False

    inter = len(s1 & s2)
    min_len = min(len(s1), len(s2))
    containment = inter / min_len if min_len > 0 else 0
    union = len(s1 | s2)
    jaccard = inter / union if union > 0 else 0

    # 공통 토큰 3개 이상이면서 포함율 75% 이상이거나 자카드 65% 이상
    if inter >= 3 and (containment >= 0.75 or jaccard >= 0.65):
        return True

    # 또는 공백 제거 제목 문자열 상호 포함 (15자 이상)
    if len(t1_norm) >= 15 and (t1_norm in t2_norm or t2_norm in t1_norm):
        return True

    return False


def _is_primary_source(j: dict) -> bool:
    """원천 기관(도서관 본청, 지자체 시청/구청) 여부 판정 (사람인 등 2차 포털보다 우선)"""
    sid = j.get("sid", "")
    src = j.get("source", "")
    if "portal" in sid or "saramin" in sid or "incruit" in sid:
        return False
    if "포털" in src or "사람인" in src or "인크루트" in src:
        return False
    return True


def deduplicate_jobs(jobs: list) -> list:
    """수집된 전체 공고 목록에 대해 3단계 지능형 중복 필터링 및 마스터-미러 병합 수행."""
    # 최신 등록순 및 마감일순 정렬
    sorted_jobs = sorted(
        jobs,
        key=lambda x: (x.get("posted") or x.get("deadline") or ""),
        reverse=True
    )

    masters = []
    for cand in sorted_jobs:
        matched_master = None
        for m in masters:
            if are_duplicate_jobs(m, cand):
                matched_master = m
                break

        if matched_master:
            # 원천 소스 우선순위 조정: cand가 원천 기관이고 기존 마스터가 2차 포털이면 교체
            cand_is_pri = _is_primary_source(cand)
            m_is_pri = _is_primary_source(matched_master)

            if cand_is_pri and not m_is_pri:
                # cand를 마스터로 승격하고 기존 마스터를 alias로 흡수
                old_aliases = matched_master.get("aliases", [])
                sub_alias = {"source": matched_master["source"], "url": matched_master["url"]}
                matched_master.update({
                    "id": cand["id"],
                    "sid": cand["sid"],
                    "region": cand["region"],
                    "district": cand["district"],
                    "source": cand["source"],
                    "title": cand["title"],
                    "jobType": cand["jobType"],
                    "url": cand["url"],
                    "posted": cand["posted"] or matched_master.get("posted"),
                    "deadline": cand["deadline"] or matched_master.get("deadline"),
                })
                all_aliases = old_aliases + [sub_alias]
                # url 중복 제거
                seen_a_urls = set()
                uniq_aliases = []
                for a in all_aliases:
                    if a["url"] != matched_master["url"] and a["url"] not in seen_a_urls:
                        seen_a_urls.add(a["url"])
                        uniq_aliases.append(a)
                if uniq_aliases:
                    matched_master["aliases"] = uniq_aliases
            else:
                # cand를 sub_alias로 흡수
                if "aliases" not in matched_master:
                    matched_master["aliases"] = []
                sub_alias = {"source": cand["source"], "url": cand["url"]}
                if cand["url"] != matched_master["url"] and not any(a["url"] == cand["url"] for a in matched_master["aliases"]):
                    matched_master["aliases"].append(sub_alias)
                # 마감일/게시일 결측치 상호 보완
                if not matched_master.get("deadline") and cand.get("deadline"):
                    matched_master["deadline"] = cand["deadline"]
                if not matched_master.get("posted") and cand.get("posted"):
                    matched_master["posted"] = cand["posted"]
        else:
            # 새 마스터 등록
            masters.append(cand)

    return masters
