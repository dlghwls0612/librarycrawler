"""parsers.py — 공고 본문 및 테이블에서 접수기간 및 마감일 추출 엔진."""
import re
from datetime import datetime


def normalize_date(text):
    """'2026. 8. 12' or '2026-08-12' or '26.11.02' -> 'YYYY-MM-DD'."""
    if not text:
        return None
    text = text.strip()

    # 1. YYYY-MM-DD or YYYY.MM.DD
    m1 = re.search(r'(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if m1:
        y, m, d = m1.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"

    # 2. YY.MM.DD (e.g., 26.11.02)
    m2 = re.search(r'\b(2\d)[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if m2:
        y, m, d = m2.groups()
        return f"20{y}-{int(m):02d}-{int(d):02d}"

    return None


def extract_dates(text):
    """텍스트에서 접수 시작일과 마감일을 추출.
    반환: (start_date, deadline)
    예:
      '2026. 8. 12.(수) ~ 8. 14.(금)' -> ('2026-08-12', '2026-08-14')
      '2026-10-21 ~ 2026-11-05'      -> ('2026-10-21', '2026-11-05')
      '~ 2026. 11. 15'               -> (None, '2026-11-15')
    """
    if not text:
        return None, None

    # 접수기간 / 접수일자 패턴 찾기
    range_match = re.search(
        r'(20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})(?:\([^)]*\))?\s*~'
        r'\s*(?:(20\d{2})[.\-/])?\s*(\d{1,2})[.\-/]\s*(\d{1,2})',
        text
    )
    if range_match:
        s_raw, e_year, e_month, e_day = range_match.groups()
        start = normalize_date(s_raw)
        if start:
            year = e_year or start[:4]
            deadline = f"{year}-{int(e_month):02d}-{int(e_day):02d}"
            return start, deadline

    # 단독 마감일 패턴 (예: ~ 2026.11.05 or 마감: 2026-11-05)
    single_match = re.search(
        r'(?:마감|접수마감|기한|~)\s*:?\s*(20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})',
        text
    )
    if single_match:
        deadline = normalize_date(single_match.group(1))
        return None, deadline

    # 날짜가 2개 이상 연속 등장하는 경우 (첫 번째=시작, 마지막=마감)
    all_dates = re.findall(r'20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}', text)
    if len(all_dates) >= 2:
        return normalize_date(all_dates[0]), normalize_date(all_dates[-1])
    elif len(all_dates) == 1:
        return None, normalize_date(all_dates[0])

    return None, None
