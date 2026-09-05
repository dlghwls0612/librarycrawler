"""parsers.py — 범용 목록/상세 추출.
사이트마다 다른 HTML에 강하도록 취약한 CSS 선택자 대신
'링크 + 날짜 정규식' 휴리스틱으로 후보 공고를 뽑는다(2단계 1차).
이후 특정 사이트가 안 되면 sources.yaml에 선택자 override를 추가해 정밀화.
"""
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

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
                 "모임", "축제", "체험", "강연", "봉사자 모집",
                 # 도서관/사서 업무가 아닌 시설·지원 직군(사서 구직자 대상 아님)
                 "미화원", "미화", "환경미화", "특수운영직", "청소원", "청소", "경비원", "경비",
                 "방호원", "방호", "당직", "시설관리원", "시설관리", "조리", "급식", "방역",
                 "소독", "운전원", "주차")
DATE_RE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
# 제목에 박힌 마감일: "~8/28까지", "~8.17", "(9.1~9.11)", "~2026.11.24" 등 ('까지' 없어도 인식)
TITLE_DL_RE = re.compile(r"~\s*(?:(20\d{2})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\s*(?:까지)?")
TITLE_DL_RE2 = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*까지")


def _to_date(m):
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _looks_detail(url):
    u = url.lower()
    return any(h in u for h in DETAIL_HINTS)


# 도서관 통합홈 목록 행의 상세 이동 함수 호출에서 글번호 추출: fnDetail('209697') 등
ONCLICK_ID_RE = re.compile(
    r"(?:fnDetail|fnView|fnSelectDetail|goView|goDetail|fn_view|fn_detail)\s*\(\s*['\"]?(\d+)['\"]?",
    re.I)
# 페이지의 fnDetail 함수에서 (글번호 필드명, 상세 endpoint)를 자동 감지
# — 사이트마다 필드명이 다름: snlib=postIdx, eplib=bbsPostIdx 등
_BBSDETAIL_RE = re.compile(
    r"function\s+fnDetail\s*\([^)]*\)\s*\{.*?\.(\w+)\.value\s*=\s*\w+.*?\.action\s*=\s*[\"']([^\"']+Detail\.do)",
    re.S)


def _detect_bbspost_detail(html):
    """목록 페이지의 fnDetail(idx) 정의에서 (글번호 파라미터명, 상세 URL 경로)를 얻는다."""
    m = _BBSDETAIL_RE.search(html or "")
    return (m.group(1), m.group(2)) if m else None


def _egov_detail_url(a, base_url, bbs_ctx=None):
    """전자정부 목록의 상세 링크가 href=""/javascript 라서 글번호가 속성에 담기는 경우
    상세 URL을 복원한다. 두 패턴 지원:
    - sen.go.kr(교육청): keyValue=board_idx, index.do → view.do (menu_idx·manage_idx 유지)
      (keyValue2 속성은 menu_idx=0 이라 상세가 빈 값으로 떠서 쓰지 않음)
    - goe.go.kr(경기교육): data-id=nttSn, selectNttList.do → selectNttInfo.do (mi·bbsId 유지)"""
    pu = urlparse(base_url)
    q = parse_qs(pu.query)
    # sen 패턴
    kv = (a.get("keyvalue") or "").strip()
    if kv and kv.isdigit() and pu.path.endswith("index.do"):
        menu = (q.get("menu_idx") or ["25"])[0]
        manage = (q.get("manage_idx") or ["0"])[0]
        path = pu.path[: -len("index.do")] + "view.do"
        query = urlencode({"menu_idx": menu, "board_idx": kv, "manage_idx": manage})
        return f"{pu.scheme}://{pu.netloc}{path}?{query}"
    # goe 패턴
    did = (a.get("data-id") or "").strip()
    if did and did.isdigit() and pu.path.endswith("selectNttList.do"):
        keep = {k: v[0] for k, v in q.items() if k in ("mi", "bbsId")}
        keep["nttSn"] = did
        path = pu.path[: -len("selectNttList.do")] + "selectNttInfo.do"
        return f"{pu.scheme}://{pu.netloc}{path}?{urlencode(keep)}"
    # 도서관 통합홈(bbsPostList.do) 패턴: 행 앵커가 href="#javascript" onclick="fnDetail('209697')"
    # → 상세 endpoint + 글번호파라미터로 실제 원문 딥링크 복원(파라미터명은 페이지에서 감지)
    m = ONCLICK_ID_RE.search(a.get("onclick") or "")
    if m and (bbs_ctx or pu.path.endswith("bbsPostList.do")):
        field, action = bbs_ctx or ("postIdx", pu.path[: -len("bbsPostList.do")] + "bbsPostDetail.do")
        keep = {field: m.group(1), "manageCd": (q.get("manageCd") or ["ALL"])[0]}
        if "menuNo" in q:
            keep["menuNo"] = q["menuNo"][0]
        detail = urljoin(base_url, action)
        sep = "&" if "?" in detail else "?"
        return detail + sep + urlencode(keep)
    return None


# 개별 글이 아닌 게시판 목록/정적안내 페이지로 끝나는 URL(=메뉴·목록 링크 노이즈)
LIST_ENDPOINTS = ("bbspostlist.do", "selectbbsnttlist.do", "selectnttlist.do",
                  "contents.do", "bbslist.do", "list.do", "boardlist.do")


def _is_list_or_menu(url):
    """개별 공고가 아니라 게시판 목록/메뉴/정적 페이지 URL인지(=노이즈).
    단, 특정 글로 진입한 흔적(#javascript 폴백, 상세 힌트 토큰)이 있으면 목록으로 안 봄."""
    u = url.lower()
    if "#javascript" in u:            # 파서가 목록의 특정 행을 클릭한 폴백(실제 공고)
        return False
    if any(h in u for h in DETAIL_HINTS):
        return False
    path = urlparse(u).path
    return any(path.endswith(e) for e in LIST_ENDPOINTS)


def _clean_title(t):
    """목록 행 전체가 앵커로 묶여 제목에 글번호·등록일·조회수가 섞여 들어온 경우 정제.
    예: '144712 채용공고 [다산성곽도서관] … 채용 공고 등록일 2026.08.22 조회수 552'
        → '채용공고 [다산성곽도서관] … 채용 공고'"""
    t = re.sub(r"^\s*\d{5,}\s+", "", t)   # 앞머리 글번호(5자리+, 연도4자리는 보존)
    # 뒤쪽 메타데이터부터 잘라냄
    t = re.split(r"\s*(?:등록일|작성일|게시일|수정일|조회수|조회|첨부파일|작성자|담당부서)\b", t)[0]
    return t.strip()


def extract_listings(html, base_url):
    """목록 페이지 → [{title, url, posted}] 후보. 과다수집 후 classify에서 필터."""
    soup = BeautifulSoup(html, "lxml")
    bbs_ctx = _detect_bbspost_detail(html)   # 도서관통합홈 상세 파라미터명·endpoint(있으면)
    items, seen = [], set()
    for a in soup.find_all("a"):
        title = _clean_title(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        if not title or len(title) < 5:
            continue
        if href.lower().startswith("javascript") or href == "" or href.startswith("#"):
            # href가 js/빈값/#프래그먼트인 전자정부·도서관통합홈: 속성·onclick으로 상세 URL 복원
            url = _egov_detail_url(a, base_url, bbs_ctx)
            if not url:
                # 복원 실패 시: '#xxx' 프래그먼트는 목록 링크로라도 남김(공고 유실 방지)
                if href.startswith("#") and href != "#":
                    url = urljoin(base_url, href)
                else:
                    continue
        else:
            url = urljoin(base_url, href)
        if url in seen:
            continue
        # 게시판 목록/메뉴/정적 페이지 링크(개별 공고 아님)는 제외
        if _is_list_or_menu(url):
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
    for ctx in ("접수기간", "접수 기간", "접수마감", "접수 마감", "접수기한", "접수 기한",
                "마감일", "제출기한", "제출기간", "제출 기간", "원서접수", "원서 접수",
                "서류접수", "서류 접수", "신청기간", "신청 기간", "지원마감", "지원 마감",
                "모집기간", "모집 기간", "모집마감", "지원기간", "지원 기간", "접수일시", "접수 일시"):
        idx = text.find(ctx)
        if idx != -1:
            window = text[idx: idx + 90]
            dates = [d for d in (_to_date(m) for m in DATE_RE.finditer(window)) if d]
            if dates:
                # 범위 끝이 연도 없이 '~ 9. 11' 형태면 시작일 연도를 물려받아 보정(하루 일찍 만료 방지)
                tail = re.search(r"~\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})\s*일?", window)
                if tail:
                    cand = _compose_md(int(tail.group(1)), int(tail.group(2)), int(dates[-1][:4]), None)
                    if cand and cand > dates[-1]:
                        return cand
                return dates[-1]   # 범위면 뒤(마감), 단일이면 그 날짜
    return None


def extract_apply_start(html):
    """상세 페이지 접수기간의 '시작일'을 추출(첫 날짜). 시작일이 미래면 '접수예정'으로 표시하기 위함."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    for ctx in ("접수기간", "접수 기간", "신청기간", "신청 기간", "모집기간", "지원기간", "원서접수", "접수일시"):
        idx = text.find(ctx)
        if idx != -1:
            window = text[idx: idx + 90]
            dates = [d for d in (_to_date(m) for m in DATE_RE.finditer(window)) if d]
            if dates:
                return dates[0]   # 첫 날짜 = 접수 시작일
    return None


def extract_posted(html):
    """상세 페이지에서 게시일(작성일/등록일)을 추출 — 목록에서 게시일을 못 얻었을 때 보조.
    게시일이 있으면 안전만료(N일)가 동작해 오래된 공고가 자동으로 사라진다."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    for ctx in ("게시일", "작성일자", "작성일", "등록일자", "등록일", "게시일자", "공고일"):
        idx = text.find(ctx)
        if idx != -1:
            m = DATE_RE.search(text[idx: idx + 40])
            if m:
                d = _to_date(m)
                if d:
                    return d
    return None


def _compose_md(mo, d, year, posted):
    """월/일(+연도)로 YYYY-MM-DD 구성. 연도 불명이면 게시일 연도로 추정
    (게시월보다 마감월이 작으면 이듬해=연말게시→연초마감). 게시일도 없으면 None."""
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    if year is None:
        if not posted or len(posted) < 7:
            return None
        year = int(posted[:4])
        if mo < int(posted[5:7]):
            year += 1
    return f"{year:04d}-{mo:02d}-{d:02d}"


def deadline_from_title(title, posted=None, fallback_year=None):
    """제목에 명시된 마감일('~8/28까지', '(~8.17)', '9월 5일까지' 등)을 추출.
    상세페이지에서 마감일을 못 읽었을 때 보조로 사용(마감 오검출 방지).
    연도가 제목·게시일에 다 없으면 fallback_year(보통 수집 연도)로 보정."""
    if not title:
        return None

    def _mk(mo, d, y):
        got = _compose_md(mo, d, y, posted)
        if got is None and y is None and fallback_year:
            got = _compose_md(mo, d, fallback_year, None)
        return got

    last = None
    for m in TITLE_DL_RE.finditer(title):
        y = int(m.group(1)) if m.group(1) else None
        got = _mk(int(m.group(2)), int(m.group(3)), y)
        if got:
            last = got
    if last:
        return last
    m = TITLE_DL_RE2.search(title)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), None)
    return None


# 제목의 임용일: "(26.08.18.임용)", "26.8.7 임용" 등 — 이 날짜가 지나면 접수는 이미 종료됨
APPOINT_RE = re.compile(r"(\d{2})\s*[.\-]\s*(\d{1,2})\s*[.\-]\s*(\d{1,2})\s*\.?\s*임용")


def appointment_date_from_title(title):
    """제목에 명시된 '임용일'을 YYYY-MM-DD로. 접수 마감일은 아니지만, 임용일이 지났으면
    접수는 확실히 끝난 것이라 만료 판정의 상한선으로 쓴다(표시 마감일로는 쓰지 않음)."""
    if not title:
        return None
    m = APPOINT_RE.search(title)
    if not m:
        return None
    return _compose_md(int(m.group(2)), int(m.group(3)), 2000 + int(m.group(1)), None)


_SARAMIN_DDAY = re.compile(r"D-(\d+)")
# 지역명 → (region, 서울/경기 여부). 사이트 탭이 서울/경기뿐이라 그 외 지역은 버림.
_SARAMIN_REGION = {"서울": "서울", "경기": "경기"}


def extract_saramin(html, base_url, today):
    """사람인 목록 전용 파서: 행마다 제목·상세URL·지역·마감일(D-day)을 정확히 추출.
    - 지역: .work_place('서울 영등포구 외') → region/district. 서울·경기 외는 제외(사이트 범위).
    - 마감일: .support_detail .date 의 'D-N' → today+N일(절대날짜), '오늘마감'→today, 상시/수시→None.
    today = date 객체(만료·D-day 환산 기준)."""
    from datetime import date, timedelta
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("div.list_item, .item_recruit"):
        a = row.select_one(".job_tit a")
        if not a:
            continue
        title = _clean_title(a.get_text(" ", strip=True))
        if not title or len(title) < 5:
            continue
        if any(w in title for w in EXCLUDE_WORDS):
            continue
        # 지역: 서울/경기만
        wp = row.select_one(".work_place")
        loc = wp.get_text(" ", strip=True) if wp else ""
        region = _SARAMIN_REGION.get(loc.split()[0], None) if loc else None
        if region is None:
            continue   # 인천/강원/전국 등 서울·경기 밖은 제외
        parts = loc.split()
        district = next((p for p in parts[1:] if p.endswith(("구", "시", "군"))), "전역·통합")
        # 상세 URL
        href = (a.get("href") or "").strip()
        rid = ""
        rm = re.search(r"rec[-_](?:link_)?(\d+)", (row.get("id") or "") + " " + (a.get("id") or ""))
        if rm:
            rid = rm.group(1)
        if href and "rec_idx" in href:
            url = urljoin(base_url, href)
        elif rid:
            url = f"https://www.saramin.co.kr/zf_user/jobs/relay/view?view_type=list&rec_idx={rid}"
        else:
            url = urljoin(base_url, href) if href else None
        if not url:
            continue
        # 마감일(D-day)
        deadline = None
        dd = row.select_one(".support_detail .date") or row.select_one(".date")
        if dd:
            dt = dd.get_text(" ", strip=True)
            m = _SARAMIN_DDAY.search(dt)
            m2 = re.search(r"~\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", dt)  # ~09.13(토) 형식
            if m:
                deadline = (today + timedelta(days=int(m.group(1)))).isoformat()
            elif "오늘마감" in dt or "오늘 마감" in dt:
                deadline = today.isoformat()
            elif m2:
                mo, d = int(m2.group(1)), int(m2.group(2))
                try:
                    cand = date(today.year, mo, d)
                    if cand < today:                        # 월/일이 지났으면 이듬해
                        cand = date(today.year + 1, mo, d)
                    deadline = cand.isoformat()
                except ValueError:
                    pass
            # 상시채용/수시채용/채용시/표기없음 → None(만료 안 함)
        out.append({"title": title, "url": url, "posted": None, "deadline": deadline,
                    "region": region, "district": district})
    return out
