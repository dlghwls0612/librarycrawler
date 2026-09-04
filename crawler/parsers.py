"""parsers.py — 범용 목록/상세 추출.
사이트마다 다른 HTML에 강하도록 취약한 CSS 선택자 대신
'링크 + 날짜 정규식' 휴리스틱으로 후보 공고를 뽑는다(2단계 1차).
이후 특정 사이트가 안 되면 sources.yaml에 선택자 override를 추가해 정밀화.
"""
import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

# 상세 링크로 보이는 힌트(게시글 URL 판별)
DETAIL_HINTS = ("view", "nttid", "board_idx", "wr_id", "articleview", "bbsarticleview",
                "read", "seq=", "idx=", "artclview", "b_idx", "pid=", "post/")
# 고용 관련 토큰 — '사서/도서관/모집/공고'는 프로그램 제목에도 흔해 제외, 고용 신호어만
JOB_WORDS = ("채용", "근로자", "기간제", "임기제", "아르바이트", "알바", "지원인력",
             "보조인력", "구인", "임용", "신규직원", "직원채용", "순회사서", "개관연장", "개관시간 연장",
             "공개채용", "공개경쟁", "채용공고")
# 프로그램·행사·안내성 제목 제외(고용어가 있어도 이게 있으면 제외)
EXCLUDE_WORDS = ("참가자", "참여자", "수강", "회원", "이용자", "강좌", "교실", "프로그램", "행사",
                 "대회", "공모전", "신청", "당첨", "휴관", "반납", "독서", "전시", "특강", "캠프",
                 "모임", "축제", "체험", "강연", "봉사자 모집")
DATE_RE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def _to_date(m):
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _looks_detail(url):
    u = url.lower()
    return any(h in u for h in DETAIL_HINTS)


def extract_listings(html, base_url):
    """목록 페이지 → [{title, url, posted}] 후보. 과다수집 후 classify에서 필터."""
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    for a in soup.find_all("a"):
        title = a.get_text(" ", strip=True)
        href = (a.get("href") or "").strip()
        if not title or len(title) < 5:
            continue
        if href.lower().startswith("javascript") or href in ("#", ""):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        # 고용 토큰이 있고 + 프로그램/행사성 단어가 없어야 후보
        if not any(w in title for w in JOB_WORDS):
            continue
        if any(w in title for w in EXCLUDE_WORDS):
            continue
        posted = None
        row = a.find_parent(["tr", "li", "div", "article"])
        if row:
            m = DATE_RE.search(row.get_text(" ", strip=True))
            if m:
                posted = _to_date(m)
        seen.add(url)
        items.append({"title": title, "url": url, "posted": posted})
    return items


def extract_deadline(html):
    """상세 페이지에서 '접수 마감일' 추출. 접수/마감 문맥 창(window) 안의 마지막 날짜를 마감으로.
    (요일표기 '(수)' 등이 섞여도 견고. 임용일/발표일은 접수 문맥 밖이라 잡지 않음.)
    못 찾으면 None → 안전만료로만 처리(열린 공고 오숨김 방지).
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    for ctx in ("접수기간", "접수 기간", "접수마감", "접수 마감", "마감일", "제출기한", "원서접수"):
        idx = text.find(ctx)
        if idx != -1:
            window = text[idx: idx + 90]
            dates = [d for d in (_to_date(m) for m in DATE_RE.finditer(window)) if d]
            if dates:
                return dates[-1]   # 범위면 뒤(마감), 단일이면 그 날짜
    return None
