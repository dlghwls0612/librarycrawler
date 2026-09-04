"""classify.py — 도서관 관련성 필터 · 고용형태 태깅 · 결과공고 판별 · 마감판정."""
from datetime import date, datetime, timedelta


def is_result_post(title, settings):
    """합격자/전형결과 공고 여부(→ 공고 목록에서 제외하고 종료판정에 사용)."""
    kws = settings.get("closure", {}).get("result_keywords", [])
    return any(k in title for k in kws)


def is_library_relevant(title, settings):
    """도서관/사서 관련 공고인지(혼합 게시판 필터용)."""
    kws = settings.get("keywords_include", [])
    return any(k in title for k in kws)


def tag_jobtype(title, settings):
    rules = settings.get("jobtype_rules", {})
    # 우선순위: 알바 > 계약 > 정규 (구체적인 단시간/기간제부터)
    for jt in ("알바", "계약", "정규"):
        for kw in rules.get(jt, []):
            if kw in title:
                return jt
    return "계약"  # 기본값(대부분 기간제)


def is_expired(deadline, today=None):
    if not deadline:
        return False
    today = today or date.today()
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").date()
        return d < today
    except ValueError:
        return False


def is_safety_expired(posted, settings, today=None):
    """마감일을 못 읽은 공고: 게시 후 N일 지나면 만료(④ 안전만료)."""
    if not posted:
        return False
    days = settings.get("closure", {}).get("safety_expire_days", 25)
    today = today or date.today()
    try:
        p = datetime.strptime(posted, "%Y-%m-%d").date()
        return (today - p) > timedelta(days=days)
    except ValueError:
        return False
