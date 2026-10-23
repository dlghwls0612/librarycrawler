"""classify.py — 기록관리 직무 적합성, 서울·경기 지역 판별, 자격요건, D-Day 상태 판정."""
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def get_today(today=None):
    """한국 표준시(KST) 기준 오늘 날짜."""
    return today or datetime.now(KST).date()


def is_result_post(title, settings):
    """합격자/전형결과 공고 여부(수집 목록에서 배제)."""
    kws = settings.get("closure", {}).get("result_keywords", [])
    return any(k in title for k in kws)


def is_records_relevant(title, body="", settings=None):
    """기록관리(기록연구사, 전문요원, 아키비스트 등) 관련 공고인지 판별."""
    settings = settings or {}
    excludes = settings.get("keywords_exclude", [])
    includes = settings.get("keywords_include", [])

    full_text = f"{title} {body}"

    # 1. 제외 키워드가 제목이나 본문에 있으면 탈락
    if any(ex in title for ex in excludes):
        return False

    # 2. 포함 키워드가 제목에 있으면 즉시 합격
    if any(inc in title for inc in includes):
        return True

    # 3. 본문에 포함되어 있더라도 핵심 직무 단어가 들어있는지 확인
    core_kws = ["기록연구사", "기록물관리전문요원", "기록관리", "아키비스트", "아카비스트"]
    if any(ck in full_text for ck in core_kws):
        return True

    return False


def detect_region(title, body="", organization="", settings=None):
    """서울 또는 경기 지역인지 판별 (그 외는 None 반환)."""
    settings = settings or {}
    regions_map = settings.get("regions_include", {})
    full_text = f"{title} {organization} {body}"

    # 명시적 서울 검사
    seoul_kws = regions_map.get("서울", ["서울"])
    for kw in seoul_kws:
        if kw in full_text:
            return "서울"

    # 명시적 경기 검사
    gyeonggi_kws = regions_map.get("경기", ["경기"])
    for kw in gyeonggi_kws:
        if kw in full_text:
            return "경기"

    return None


def tag_jobtype(title, body="", settings=None):
    """고용형태 태깅: 임기제, 정규, 계약, 인턴 등."""
    settings = settings or {}
    rules = settings.get("jobtype_rules", {})
    text = f"{title} {body}"

    # 임기제공무원이 가장 우선적/특수함
    for kw in rules.get("임기제", []):
        if kw in text:
            return "임기제"

    for kw in rules.get("인턴", []):
        if kw in text:
            return "인턴"

    for kw in rules.get("계약", []):
        if kw in text:
            return "계약"

    for kw in rules.get("정규", []):
        if kw in text:
            return "정규"

    return "계약"


def has_specialist_credential(title, body="", settings=None):
    """기록물관리전문요원 자격 필수/우대 여부 판별."""
    settings = settings or {}
    kws = settings.get("credential_rules", {}).get("specialist_required", [])
    text = f"{title} {body}"
    return any(k in text for k in kws)


def calc_status(start_date=None, deadline=None, today=None):
    """접수 상태 및 D-Day 판정
    반환 튜플: (status_text, dday_label, badge_color)
    - status_text: '접수중', '접수예정', '오늘마감', '마감', '상시채용'
    - dday_label: 'D-3', 'D-Day', '접수 D-5', '마감'
    """
    t = get_today(today)

    if not deadline:
        return "진행중", "접수중", "badge-info"

    try:
        d_end = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "진행중", "접수중", "badge-info"

    diff_end = (d_end - t).days

    # 시작일이 있는 경우 접수 예정 판별
    if start_date:
        try:
            d_start = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
            if t < d_start:
                diff_start = (d_start - t).days
                return "접수예정", f"접수 D-{diff_start}", "badge-warning"
        except (ValueError, TypeError):
            pass

    if diff_end < 0:
        return "마감", "마감", "badge-expired"
    elif diff_end == 0:
        return "오늘마감", "D-Day", "badge-danger"
    elif diff_end <= 3:
        return "접수중", f"D-{diff_end}", "badge-danger"
    elif diff_end <= 7:
        return "접수중", f"D-{diff_end}", "badge-primary"
    else:
        return "접수중", f"D-{diff_end}", "badge-success"


def is_expired(deadline, today=None):
    """마감일 기준 만료 여부."""
    if not deadline:
        return False
    t = get_today(today)
    try:
        d = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
        return d < t
    except (ValueError, TypeError):
        return False
