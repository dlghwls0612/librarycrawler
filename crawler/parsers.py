"""parsers.py — 범용 목록/상세 추출.
사이트마다 다른 HTML에 강하도록 취약한 CSS 선택자 대신
'링크 + 날짜 정규식' 휴리스틱으로 후보 공고를 뽑는다(2단계 1차).
이후 특정 사이트가 안 되면 sources.yaml에 선택자 override를 추가해 정밀화.
"""
import json
import re
from urllib.parse import urljoin, urlparse, parse_qs, parse_qsl, urlencode

from bs4 import BeautifulSoup

# 상세 링크로 보이는 힌트(게시글 URL 판별)
DETAIL_HINTS = ("view", "nttid", "board_idx", "wr_id", "articleview", "bbsarticleview",
                "read", "seq=", "idx=", "artclview", "b_idx", "pid=", "post/")
# 고용 관련 토큰 — '사서/도서관/모집/공고'는 프로그램 제목에도 흔해 제외, 고용 신호어만
JOB_WORDS = ("채용", "근로자", "기간제", "임기제", "아르바이트", "알바", "지원인력",
             "보조인력", "구인", "임용", "신규직원", "직원채용", "순회사서", "개관연장", "개관시간 연장",
             # '모집' 단독은 프로그램 모집이 압도적이라 못 쓴다. '공개모집'만 좁혀서 허용
             # (국립장애인도서관장 경력개방형 공개모집 같은 공고를 놓치고 있었다).
             "공개채용", "공개경쟁", "채용공고", "공개모집")
# 프로그램·행사·안내성 제목 제외(고용어가 있어도 이게 있으면 제외)
EXCLUDE_WORDS = ("참가자", "참여자", "수강", "회원", "이용자", "강좌", "교실", "프로그램", "행사",
                 "대회", "공모전", "신청", "당첨", "휴관", "반납", "전시", "특강", "캠프",
                 # '독서' 단독은 너무 넓다 — '독서진흥사업 기간제근로자 채용'(양주 등
                 # 실제 도서관 채용)까지 통째로 버렸다. 프로그램성 표현만 좁혀서 제외.
                 "독서회", "독서교실", "독서동아리",
                 "모임", "축제", "체험", "강연", "봉사자 모집",
                 # 사서 구직자 대상이 아닌 위촉·초빙 성격('공개모집' 허용과 짝을 이룸)
                 "강사", "평가위원", "심사위원", "입주작가", "위촉",
                 # 도서관/사서 업무가 아닌 시설·지원 직군(사서 구직자 대상 아님)
                 "미화원", "미화", "환경미화", "특수운영직", "청소원", "청소", "경비원", "경비",
                 "방호원", "방호", "당직", "시설관리원", "시설관리", "조리", "급식", "방역",
                 "소독", "운전원", "주차")
# 제외어 판정 전에 지워야 하는 말 — '청소'가 '청소년'에 걸려 국립어린이청소년도서관·
# 서초청소년도서관 같은 곳의 사서 채용 공고가 통째로 버려지고 있었다.
_EXCL_SAFE_RE = re.compile(r"청소년")


def has_exclude_word(title):
    """프로그램·비사서직 제외어에 걸리는가. '청소년'은 '청소'로 오인하지 않는다."""
    t = _EXCL_SAFE_RE.sub("", title or "")
    return any(w in t for w in EXCLUDE_WORDS)


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


# ---------- JS 폼전송형 상세링크 복원 (금천문화재단 goBoardView, 성동문화재단 fnView 등) ----------
# 행을 클릭하면 JS가 숨은 폼에 글번호를 넣고 GET 전송하는 목록. 앵커에 href가 없어
# 그냥 두면 공고가 통째로 안 잡힌다. 함수 정의에서 (action, 인자→폼필드) 를 읽어 URL을 복원.
_JS_FUNC_RE = re.compile(r"function\s+(\w+)\s*\(([^)]*)\)\s*\{", re.I)
_JS_ACTION_RE = re.compile(r"""attr\(\s*['"]action['"]\s*,\s*['"]([^'"]+)['"]"""
                           r"""|\.action\s*=\s*['"]([^'"]+)['"]""")
_JS_SETVAL_RE = re.compile(r"""\$\(\s*['"]#(\w+)['"]\s*\)\.val\(\s*(\w+)\s*\)"""
                           r"""|\.(\w+)\.value\s*=\s*(\w+)\b""")
# onclick 의 함수 호출과 인자들: goBoardView('9268') · fnView('2026-46','1')
_JS_CALL_RE = re.compile(r"(\w+)\s*\(\s*([^)]*)\)")


def _js_body(html, start):
    """`{` 위치(start)부터 짝이 맞는 `}` 까지의 함수 본문. 너무 길면 잘라 안전하게."""
    depth, i, end = 0, start, min(len(html), start + 1500)
    while i < end:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i]
        i += 1
    return html[start:end]


def _detect_form_detail(html):
    """페이지의 JS에서 '폼에 값 넣고 전송하는 상세이동 함수'들을 찾는다.
    → {함수명: (action경로, [인자순서대로의 폼필드명])}"""
    out = {}
    for m in _JS_FUNC_RE.finditer(html or ""):
        name, args = m.group(1), m.group(2)
        body = _js_body(html, m.end() - 1)
        am = _JS_ACTION_RE.search(body)
        if not am:
            continue
        action = am.group(1) or am.group(2)
        setval = {}
        for sm in _JS_SETVAL_RE.finditer(body):
            field, arg = (sm.group(1), sm.group(2)) if sm.group(1) else (sm.group(3), sm.group(4))
            setval[arg] = field
        argnames = [a.strip() for a in args.split(",") if a.strip()]
        fields = [setval.get(a) for a in argnames]
        if action and any(fields):
            out[name] = (action, fields)
    return out


# `location.href = "?" + 쿼리조작` 형태의 상세이동(국립중앙도서관 fn_goViewBoard 등).
# 폼이 없어 위 방식으로는 안 잡히고, 복원에 실패하면 모든 행이 href="#none" 하나로
# 합쳐져 목록 전체가 1건으로 쪼그라든다(9행 → 후보 1건이었다).
_JS_LOCHREF_RE = re.compile(r"""location\.href\s*=\s*['"]\?['"]""")
_JS_QREPL_RE = re.compile(
    r"""fn_replaceQueryString\s*\([^,]+,\s*['"](\w+)['"]\s*,\s*['"]?(\w+)['"]?\s*\)""")


def _detect_query_detail(html):
    """→ {함수명: (인자이름목록, [(쿼리키, 인자이름 또는 리터럴), ...])}"""
    out = {}
    for m in _JS_FUNC_RE.finditer(html or ""):
        body = _js_body(html, m.end() - 1)
        if not _JS_LOCHREF_RE.search(body):
            continue
        pairs = _JS_QREPL_RE.findall(body)
        if pairs:
            out[m.group(1)] = ([a.strip() for a in m.group(2).split(",") if a.strip()], pairs)
    return out


def _query_detail_url(onclick, base_url, qctx):
    """onclick 의 호출 인자를 쿼리에 얹어 상세 URL 복원."""
    for m in _JS_CALL_RE.finditer(onclick or ""):
        ctx = qctx.get(m.group(1))
        if not ctx:
            continue
        argnames, pairs = ctx
        vals = [v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()]
        amap = dict(zip(argnames, vals))
        pu = urlparse(base_url)
        q = {k: v[0] for k, v in parse_qs(pu.query).items()}
        hit = False
        for key, token in pairs:
            v = amap.get(token, token)      # 인자면 실제값, 아니면 리터럴
            if v:
                q[key] = v
                hit = hit or token in amap  # 인자값이 하나라도 들어가야 진짜 상세링크
        if hit:
            return f"{pu.scheme}://{pu.netloc}{pu.path}?{urlencode(q)}"
    return None


def _form_detail_url(onclick, base_url, form_ctx):
    """onclick 의 함수 호출 + 함수 정의 → 상세 URL(GET) 복원."""
    for m in _JS_CALL_RE.finditer(onclick or ""):
        ctx = form_ctx.get(m.group(1))
        if not ctx:
            continue
        action, fields = ctx
        vals = [v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()]
        q = {f: v for f, v in zip(fields, vals) if f and v}
        if not q:
            continue
        detail = urljoin(base_url, action.split(";jsessionid=")[0])
        return detail + ("&" if "?" in detail else "?") + urlencode(q)
    return None


# BD_ 계열 목록 → 상세 endpoint·글번호 파라미터. 글번호는 onclick 인자 중 12자리 이상 숫자
# (fnView('1082','20260827174546009',…) · jsView('1022','20260908141606268',…))
_BD_ID_RE = re.compile(r"['\"](\d{12,})['\"]")
_BD_LIST_MAP = (("BD_selectBbsList.do", "BD_selectBbs.do", "q_bbscttSn"),
                ("BD_board.list.do", "BD_board.view.do", "seq"))


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
    # 지자체 표준 게시판(BD_ 계열) — 글번호가 onclick 인자에 있고 이동 JS는 외부 파일이라
    # 페이지에서 감지가 안 된다. 목록 endpoint 로 상세 endpoint·글번호 파라미터를 판별.
    #   고양·교육청: BD_selectBbsList.do → BD_selectBbs.do  (q_bbscttSn)
    #   파주·수원  : BD_board.list.do    → BD_board.view.do (seq)
    bd = _BD_ID_RE.search(a.get("onclick") or "")
    if bd:
        for lst, view, idp in _BD_LIST_MAP:
            if pu.path.endswith(lst):
                keep = {k: v[0] for k, v in q.items()}
                keep[idp] = bd.group(1)
                path = pu.path[: -len(lst)] + view
                return f"{pu.scheme}://{pu.netloc}{path}?{urlencode(keep)}"
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
# 사이트 첫 화면(로고·홈으로 돌아가는 링크). 사이트 이름 자체에 고용어가 들어간 경우
# ('국회채용시스템' 등) 홈 링크가 공고로 오수집되므로 별도로 차단.
HOME_ENDPOINTS = ("mainpage.do", "main.do", "main.jsp", "mainpage.jsp", "main.php")


def _is_home(url):
    pu = urlparse(url.lower())
    # 해시 라우트 SPA(군포시립도서관 '/#/bbs/notice/30910')는 프래그먼트가 곧 경로다.
    # 이걸 안 보면 path='/' · query='' 라서 개별 공고 링크가 전부 '첫 화면'으로 걸러진다.
    if pu.fragment.startswith("/") and len(pu.fragment) > 1:
        return False
    if pu.path.endswith(HOME_ENDPOINTS):
        return True
    return pu.path in ("", "/") and not pu.query


def _is_list_or_menu(url):
    """개별 공고가 아니라 게시판 목록/메뉴/정적 페이지 URL인지(=노이즈).
    단, 특정 글로 진입한 흔적(#javascript 폴백, 상세 힌트 토큰)이 있으면 목록으로 안 봄."""
    if _is_home(url):     # 첫 화면은 어떤 경우에도 개별 공고가 아님
        return True
    u = url.lower()
    if "#javascript" in u:            # 파서가 목록의 특정 행을 클릭한 폴백(실제 공고)
        return False
    if any(h in u for h in DETAIL_HINTS):
        return False
    path = urlparse(u).path
    return any(path.endswith(e) for e in LIST_ENDPOINTS)


# 제목이 앵커 '밖'에 있는 목록(인크루트 등): 링크 글자는 '자세히 보기' 뿐이고
# 진짜 제목은 같은 행의 <span class="title"> 에 있다. 앵커 글자만 보면 전부 버려진다.
BUTTON_TEXTS = {"자세히보기", "자세히", "상세보기", "상세", "더보기", "바로가기", "보기",
                "신청하기", "지원하기", "공고보기", "view", "more", "detail", "readmore"}
TITLE_SEL = "[class*=title], [class*=subject], [class*=tit], [class*=subj]"


def _norm(t):
    return re.sub(r"\s+", "", t or "").lower()


def _row_title(a):
    """앵커 글자가 '자세히 보기' 같은 버튼 문구일 때 같은 행에서 진짜 제목을 찾는다.
    클래스에 title/subject 가 박힌 요소만 보므로 다른 사이트에 오작용하지 않는다."""
    row = a.find_parent(["li", "tr", "article", "div"])
    for _ in range(3):          # 행 컨테이너가 몇 겹 위일 수 있어 조금 거슬러 올라감
        if row is None:
            return None
        for el in row.select(TITLE_SEL):
            t = el.get_text(" ", strip=True)
            if len(t) >= 5 and _norm(t) not in BUTTON_TEXTS:
                return t
        row = row.find_parent(["li", "tr", "article", "div"])
    return None


# 목록 행에 접수기간이 통째로 적힌 게시판(인크루트 '2026.09.08 00:00~2026.09.14 14:00' 등).
# 여기서 마감일을 바로 얻으면 상세 페이지를 안 열어도 되고, 상세에서 못 읽어 마감일이
# 비는 것도 막는다(마감일이 비면 게시 N일 뒤 안전만료로 조용히 사라진다).
_ROW_RANGE_RE = re.compile(
    r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})[^~]{0,15}~[^0-9]{0,15}(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})")


def _row_deadline(row):
    """행에 적힌 접수기간의 끝날짜. '접수'·'모집중' 표시가 함께 있을 때만 인정한다
    (게재기간·행사기간을 접수 마감으로 오인하지 않도록)."""
    if row is None:
        return None
    txt = row.get_text(" ", strip=True)
    if not ("접수" in txt or "모집중" in txt or row.select_one('[title*="접수"]')):
        return None
    m = _ROW_RANGE_RE.search(txt)
    if not m:
        return None
    d = DATE_RE.search(m.group(2))
    return _to_date(d) if d else None


def _clean_title(t):
    """목록 행 전체가 앵커로 묶여 제목에 글번호·등록일·조회수가 섞여 들어온 경우 정제.
    예: '144712 채용공고 [다산성곽도서관] … 채용 공고 등록일 2026.08.22 조회수 552'
        → '채용공고 [다산성곽도서관] … 채용 공고'"""
    t = re.sub(r"^\s*\d{5,}\s+", "", t)   # 앞머리 글번호(5자리+, 연도4자리는 보존)
    # 뒤쪽 메타데이터부터 잘라냄
    t = re.split(r"\s*(?:등록일|작성일|게시일|수정일|조회수|조회|첨부파일|작성자|담당부서)\b", t)[0]
    return t.strip()


# ※ '채용 전용 게시판은 고용어 조건 면제' 를 시도했다가 폐기(2026-09-08 실측).
#   이 추출기는 목록 행이 아니라 페이지의 모든 <a>를 훑기 때문에, 고용어 조건이
#   네비게이션 링크('마이라이브러리'·'신착자료검색'·각 도서관 이름)를 걸러주는
#   핵심 방어선이다. 면제하니 53개 소스에서 노이즈 후보가 2,063건 쏟아졌다.
def extract_listings(html, base_url):
    """목록 페이지 → [{title, url, posted}] 후보. 과다수집 후 classify에서 필터."""
    soup = BeautifulSoup(html, "lxml")
    bbs_ctx = _detect_bbspost_detail(html)   # 도서관통합홈 상세 파라미터명·endpoint(있으면)
    form_ctx = _detect_form_detail(html)     # JS 폼전송형 상세이동 함수들(있으면)
    q_ctx = _detect_query_detail(html)       # JS 쿼리조작형 상세이동 함수들(있으면)
    items, seen = [], set()
    for a in soup.find_all("a"):
        title = _clean_title(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        if _norm(title) in BUTTON_TEXTS:      # '자세히 보기' 링크 → 행에서 제목 회수
            title = _row_title(a) or title
        if not title or len(title) < 5:
            continue
        if href.lower().startswith("javascript") or href == "" or href.startswith("#"):
            # href가 js/빈값/#프래그먼트인 전자정부·도서관통합홈: 속성·onclick으로 상세 URL 복원
            url = _egov_detail_url(a, base_url, bbs_ctx)
            if not url and (form_ctx or q_ctx):
                # JS 폼전송형·쿼리조작형: onclick 이 행(tr/li)에 달린 경우도 있어 조상까지 훑는다
                node, hops = a, 0
                while node is not None and hops < 3 and not url:
                    oc = node.get("onclick") or ""
                    url = (_form_detail_url(oc, base_url, form_ctx) if form_ctx else None) \
                        or (_query_detail_url(oc, base_url, q_ctx) if q_ctx else None)
                    node, hops = node.parent, hops + 1
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
        # 목록 페이지 자기 자신으로 돌아오는 링크(현재 페이지·탭)는 공고가 아님
        # (단 '…#javascript' 폴백은 특정 행을 가리키므로 제외 대상 아님 → 완전 일치만 걸러냄)
        if url == base_url:
            continue
        # 게시판 목록/메뉴/첫화면/정적 페이지 링크(개별 공고 아님)는 제외
        if _is_list_or_menu(url):
            continue
        # 고용 토큰이 있고 + 프로그램/행사성 단어가 없어야 후보
        if not any(w in title for w in JOB_WORDS):
            continue
        if has_exclude_word(title):
            continue
        posted = None
        row = a.find_parent(["tr", "li", "div", "article"])
        if row:
            m = DATE_RE.search(row.get_text(" ", strip=True))
            if m:
                posted = _to_date(m)
        seen.add(url)
        item = {"title": title, "url": url, "posted": posted}
        dl = _row_deadline(row)
        if dl:                       # 목록에 접수기간이 있으면 상세를 안 열어도 됨
            item["deadline"] = dl
        items.append(item)
    return items


# ---------- 페이지 넘김(2쪽 이후) ----------
# 게시판 URL/폼에서 쓰이는 '쪽 번호' 파라미터 이름들(사이트마다 제각각)
PAGE_PARAMS = ("pageIndex", "currentPageNo", "currentPage", "pageNo", "pageNum",
               "pageNumber", "page_no", "pageIdx", "curPage", "nowPage", "cpage",
               "nPage", "startPage", "page", "cp", "pg",
               # 2026-09-08 실측 — 2쪽이 있는데 못 가던 소스들의 실제 파라미터명
               "q_currPage",   # 지자체 표준 BD_ 게시판(수원·파주·고양)
               "currPage",     # 경기도교육청 goPaging()
               "viewPage",     # 서울시교육청 도서관(sen) 계열
               "v_page",       # 양천문화재단
               "currRow",      # 금천문화재단(이름은 행번호지만 실제로는 쪽번호)
               "pgno",         # 강남문화재단
               "p")            # 영등포문화재단

# 쪽번호가 아니라 '몇 번째부터 몇 개'로 넘기는 목록(군포시도서관 offset=20&max=20).
# offset = (쪽-1) × 한쪽건수 로 계산해야 해서 위 파라미터들과 다루는 법이 다르다.
_SIZE_PARAMS = ("max", "limit", "rows", "pagesize", "rowperpage", "recordcountperpage")
_PAGE_PARAMS_LC = {p.lower() for p in PAGE_PARAMS}

# 공고를 식별하지 '않는' 목록·검색 문맥 파라미터. 같은 공고인데 이 값만 바뀌어
# 매번 새 공고로 잡히는 것을 막는다(광명시립도서관 bvLib=85/88/91… 사례).
CONTEXT_PARAMS = _PAGE_PARAMS_LC | {
    "bvlib", "wd", "where", "what", "field", "sfl", "stx", "sst", "sod", "sop",
    "searchcnd", "searchwrd", "searchgubun", "searchkeyword", "keyword", "srchtxt",
    "listtype", "sortdirection", "view_type", "offset", "max", "limit", "rows",
}


def canon_url(url):
    """공고 '식별용' 정규 URL — 목록·검색 문맥 파라미터를 떼어낸 형태.
    사이트에 저장·표시하는 링크(job.url)는 원본 그대로 두고, 중복제거·id·firstSeen·
    isNew 판정에만 이걸 쓴다. 안 그러면 게시판이 링크에 붙이는 문맥값이 바뀔 때마다
    같은 공고가 '오늘 새 공고'로 다시 뜨고 안전만료 기준일도 초기화된다.
    프래그먼트(#…)는 상세 URL 복원 실패 시의 행 구분자라 반드시 보존하되,
    해시 라우트('#/bbs/notice/30910?offset=0&max=20')는 그 안의 목록 문맥도 떼어낸다."""
    pu = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(pu.query, keep_blank_values=True)
         if k.lower() not in CONTEXT_PARAMS]
    frag = pu.fragment
    if frag.startswith("/") and "?" in frag:
        route, _, fq = frag.partition("?")
        keep = [(k, v) for k, v in parse_qsl(fq, keep_blank_values=True)
                if k.lower() not in CONTEXT_PARAMS]
        frag = route + (f"?{urlencode(keep)}" if keep else "")
    return (f"{pu.scheme}://{pu.netloc.lower()}{pu.path}"
            + (f"?{urlencode(q)}" if q else "")
            + (f"#{frag}" if frag else ""))


def _js_page_param(html):
    """`location.href="?"+fn_replaceQueryString(q,"page",pageNo)` 형태로 쪽을 넘기는
    게시판(국립중앙도서관 fn_egov_link_page 등)의 쪽번호 파라미터명.
    폼도 쿼리스트링도 없어 다른 방법으로는 알 수 없다."""
    for argnames, pairs in _detect_query_detail(html).values():
        for key, token in pairs:
            if token in argnames and key.lower() in _PAGE_PARAMS_LC:
                return key
    return None


def _page_param(soup, base_url):
    """이 게시판이 쓰는 쪽번호 파라미터명(실제 근거가 있을 때만). URL 쿼리에 이미 있으면
    그 이름(대소문자 보존), 없으면 목록 폼의 hidden input(전자정부 표준: pageIndex 등).
    근거가 없으면 None — 아무 이름이나 찍어 보내면 헛요청만 늘기 때문."""
    for k in parse_qs(urlparse(base_url).query):
        if k.lower() in _PAGE_PARAMS_LC:
            return k
    for inp in soup.find_all("input"):
        name = (inp.get("name") or "").strip()
        if name.lower() in _PAGE_PARAMS_LC:
            return name
    return None


def _set_param(url, key, value):
    """URL 쿼리의 한 파라미터만 바꾼다. 빈 값 파라미터(searchText= 등)도 보존해야 한다 —
    parse_qs 기본값은 빈 값을 버리는데, 그러면 서버가 다른 화면을 돌려주는 게시판이 있다."""
    pu = urlparse(url)
    q = dict(parse_qsl(pu.query, keep_blank_values=True))
    q[key] = str(value)
    return f"{pu.scheme}://{pu.netloc}{pu.path}?{urlencode(q)}"


def _has_page_value(url, page):
    """URL의 쪽번호 파라미터가 실제로 해당 쪽을 가리키는지(=진짜 페이지 링크인지) 확인."""
    for k, v in parse_qs(urlparse(url).query).items():
        if k.lower() in _PAGE_PARAMS_LC and v and v[0] == str(page):
            return True
    return False


def _offset_page_url(base_url, page):
    """offset/max 방식 목록의 다음 쪽 URL. 해시 라우트(#/bbs/notice?offset=0&max=20)도 지원."""
    pu = urlparse(base_url)
    route, _, frag_q = pu.fragment.partition("?")
    for in_frag, raw in ((False, pu.query), (True, frag_q)):
        if not raw:
            continue
        q = dict(parse_qsl(raw, keep_blank_values=True))
        low = {k.lower(): k for k in q}
        if "offset" not in low:
            continue
        size = next((low[s] for s in _SIZE_PARAMS if s in low), None)
        if not size or not q[size].isdigit():
            continue
        q[low["offset"]] = str((page - 1) * int(q[size]))
        if in_frag:
            return (f"{pu.scheme}://{pu.netloc}{pu.path}"
                    + (f"?{pu.query}" if pu.query else "") + f"#{route}?{urlencode(q)}")
        return (f"{pu.scheme}://{pu.netloc}{pu.path}?{urlencode(q)}"
                + (f"#{pu.fragment}" if pu.fragment else ""))
    return None


def next_page_url(html, base_url, page):
    """목록 페이지의 페이지네이션에서 `page`쪽 URL을 만든다. 못 만들면 None.
    한 쪽에 실리는 건수가 적은 게시판(성북문화재단 등)은 아직 모집 중인 공고가
    2쪽으로 밀려나는데, 1쪽만 읽으면 그 공고가 사이트에서 통째로 사라진다.
    세 형태 지원:
      ① 진짜 링크  <a href="...?pageIndex=2">2</a>
      ② JS 폼전송  <a href="#" onclick="fnSubmitForm(2)">2</a> → 폼의 쪽번호 파라미터로 복원
         (method=get 인 전자정부 목록폼이라 쿼리스트링으로 그대로 접근 가능)
      ③ 페이지네이션 자체가 JS로 그려져 앵커가 아예 없는 목록(은평구 bbsPostList 등)
         → 폼에 쪽번호 hidden input이 있을 때만 그 이름으로 복원(근거 없으면 시도 안 함)"""
    soup = BeautifulSoup(html, "lxml")
    param = _page_param(soup, base_url) or _js_page_param(html)
    call_re = re.compile(r"\(\s*['\"]?%d['\"]?\s*[,)]" % page)
    for a in soup.find_all("a"):
        if a.get_text(strip=True) != str(page):
            continue
        href = (a.get("href") or "").strip()
        if href and not href.lower().startswith(("javascript", "#")):
            url = urljoin(base_url, href)
            if _has_page_value(url, page):
                return url
            continue
        if param and call_re.search(href + " " + (a.get("onclick") or "")):
            return _set_param(base_url, param, page)
    if param:
        return _set_param(base_url, param, page)
    return _offset_page_url(base_url, page)   # offset/max 방식(군포 등)


def listing_dates(html):
    """목록 행들의 날짜(등록일) 모음 — '다음 쪽까지 볼 가치가 있나' 판단용.
    링크가 있는 tr/li 만 목록 행으로 본다(푸터·저작권 연도 오인 방지)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("tr, li"):
        if not row.find("a"):
            continue
        m = DATE_RE.search(row.get_text(" ", strip=True))
        if m:
            d = _to_date(m)
            if d:
                out.append(d)
    return out


def _dig(obj, path):
    """'data.noticeList' 같은 점 경로로 중첩 JSON에서 값 꺼내기."""
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def extract_json_listings(raw, spec):
    """JSON 목록 API → [{title, url, posted}].

    화면이 Vue로만 그려져 목록 링크에 href 가 아예 없는 도서관 통합홈(강서·안산)용.
    HTML을 긁는 대신 그 화면이 실제로 호출하는 API를 직접 부른다 —
    클릭을 흉내내는 것보다 빠르고, 화면 구조가 바뀌어도 잘 견딘다.
    spec 은 sources.yaml 의 api 블록: rows·title·id·date·detail."""
    try:
        rows = _dig(json.loads(raw), spec["rows"]) or []
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = _clean_title(str(r.get(spec["title"]) or ""))
        rid = r.get(spec["id"])
        if not title or len(title) < 5 or rid in (None, "") or rid in seen:
            continue
        if not any(w in title for w in JOB_WORDS) or has_exclude_word(title):
            continue
        posted = None
        m = DATE_RE.search(str(r.get(spec.get("date")) or ""))
        if m:
            posted = _to_date(m)
        seen.add(rid)
        out.append({"title": title, "url": spec["detail"].format(id=rid), "posted": posted})
    return out


def api_next_page(base_url, spec, page):
    """API 목록의 다음 쪽 URL. 쪽번호 파라미터명과 시작번호가 사이트마다 달라
    sources.yaml 의 page_param(기본 pageIndex)·page_base(기본 1)를 따른다.
    (중랑문화재단은 page=0 이 1쪽이라, 이걸 안 맞추면 1쪽을 통째로 건너뛴다.)"""
    return _set_param(base_url, spec.get("page_param", "pageIndex"),
                      page - 1 + int(spec.get("page_base", 1)))


def suspect_rows(html, lib_keywords=None):
    """'후보가 적다'가 진짜 공고가 없어서인지, 못 읽어서인지 가리는 진단.
    목록 행 중 ①날짜가 있고 ②고용 신호어가 있고 ③제외어(미화·소독 등)가 없는 것을 센다.
    후보 수가 이것보다 크게 적으면 = 공고는 있는데 우리가 못 읽고 있다는 뜻.
    (날짜 조건이 메뉴의 '채용공고' 글자를, 제외어 조건이 도서관 무관 직군을 걸러낸다.)

    lib_keywords 를 주면 그 키워드가 있는 행만 센다. 시청 통합 게시판처럼 도서관
    키워드가 필수인 소스에서, 어차피 우리가 안 가져가는 비도서관 공고까지 세어
    '못 읽는다'고 오경보하는 것을 막는다."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("li, tr"):
        t = row.get_text(" ", strip=True)
        if not (15 < len(t) < 200) or not DATE_RE.search(t):
            continue
        if not any(w in t for w in JOB_WORDS) or has_exclude_word(t):
            continue
        if lib_keywords and not any(k in t for k in lib_keywords):
            continue
        out.append(t[:100])
    return out


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
        if has_exclude_word(title):
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
