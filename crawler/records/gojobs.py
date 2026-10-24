"""gojobs.py — 인사혁신처 나라일터 기록연구사(국가·지방 공무원) 채용공고 수집기.

목록(apmList.do)은 서버에서 그려지므로 httpx 로 읽힌다. 목록에는 근무지가 없고 기관명만 있어
(국방부·헌법재판소처럼 지역 이름이 없는 중앙기관이 많다) 서울·경기 판정은 상세 화면의
'근무지역' 칸으로 한다. 상세 진입은 아직 마감 안 된 공고만 — 하루 몇 건 수준.
"""
import re
import time
import httpx
from bs4 import BeautifulSoup
from . import classify
from . import parsers

BASE_URL = "https://www.gojobs.go.kr"
LIST_URL = "https://www.gojobs.go.kr/apmList.do"
VIEW_URL = "https://www.gojobs.go.kr/apmView.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.gojobs.go.kr/apmList.do",
}
MENU = {"menuNo": "401", "selMenuNo": "400", "mngrMenuYn": "N"}
# 나라일터는 같은 채용의 진행 공고(서류 합격자·면접 계획·최종 합격자)가 따로 올라온다.
# records.yaml 의 result_keywords 는 띄어쓰기('최종 합격자')만 잡아서 '최종합격자 공고'가 빠져나간다.
_RESULT_WORDS = ("합격자", "면접시험", "면접 계획", "시험장소", "응시자 유의", "전형결과", "결과 공고")


def _cell(soup, label):
    """상세 화면 표에서 th 글자가 label 인 칸의 값."""
    th = soup.find("th", string=lambda s: s and s.strip() == label)
    td = th.find_next_sibling("td") if th else None
    return td.get_text(" ", strip=True) if td else ""


def _list_rows(html):
    """목록 표(apmTbl) → [(empmnsn, 제목, 기관명, 게시일, 마감일)]."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="apmTbl")
    rows = []
    for tr in (table.find("tbody").find_all("tr") if table and table.find("tbody") else []):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"fn_apmView"))
        if len(tds) < 5 or not a:
            continue
        m = re.search(r"fn_apmView\('(\d+)',\s*'(\d+)'\)", str(a.get("href", "")))
        if not m:
            continue
        rows.append((m.group(1), m.group(2), a.get_text(" ", strip=True),
                     tds[2].get_text(strip=True), parsers.normalize_date(tds[3].get_text(strip=True)),
                     parsers.normalize_date(tds[4].get_text(strip=True))))
    return rows


def crawl_gojobs(settings, keywords=None, max_pages=2, delay=1.0):
    """나라일터에서 서울·경기 근무 기록연구사 채용공고 수집(마감분은 처음부터 거른다)."""
    if keywords is None:
        keywords = ["기록연구", "기록물", "아키비스트"]

    jobs = []
    seen_ids = set()

    with httpx.Client(headers=HEADERS, timeout=12.0, follow_redirects=True) as client:
        for kw in keywords:
            for page in range(1, max_pages + 1):
                try:
                    res = client.get(LIST_URL, params={**MENU, "searchKeyword": kw, "pageIndex": page})
                    if res.status_code != 200:
                        break
                except Exception as e:
                    print(f"[나라일터] 검색어 '{kw}' {page}쪽 호출 실패: {e}")
                    break
                rows = _list_rows(res.text)
                time.sleep(delay)
                if not rows:
                    break

                for jobsecode, item_id, title, org, _posted, deadline in rows:
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    if classify.is_expired(deadline):
                        continue
                    if classify.is_result_post(title, settings) or any(w in title for w in _RESULT_WORDS):
                        continue

                    url = f"{VIEW_URL}?menuNo=401&empmnsn={item_id}&searchJobsecode={jobsecode}&selMenuNo=400"
                    try:
                        dres = client.get(url)
                        dsoup = BeautifulSoup(dres.text, "html.parser")
                    except Exception as e:
                        print(f"[나라일터] 상세 {item_id} 호출 실패: {e}")
                        continue
                    time.sleep(delay)
                    area = _cell(dsoup, "근무지역")
                    grade = _cell(dsoup, "채용직급")
                    start = parsers.normalize_date(_cell(dsoup, "접수시작일"))
                    deadline = parsers.normalize_date(_cell(dsoup, "접수마감일")) or deadline

                    # 근무지역 칸을 먼저 본다(제목·기관명은 근무지가 빠진 경우가 많음)
                    region = (classify.detect_region(area, "", "", settings)
                              or (None if area else classify.detect_region(title, "", org, settings)))
                    if not region:
                        continue
                    if not classify.is_records_relevant(title, grade, settings):
                        continue

                    status_text, dday_label, badge_color = classify.calc_status(start, deadline)
                    jobs.append({
                        "id": f"gojobs-{item_id}",
                        "source": "나라일터",
                        "category": "지자체·국가기관",
                        "title": title,
                        "organization": org,
                        "region": region,
                        "location": area,
                        "jobtype": classify.tag_jobtype(title, grade, settings),
                        "is_specialist": classify.has_specialist_credential(title, grade, settings),
                        "start_date": start,
                        "deadline": deadline,
                        "status": status_text,
                        "dday": dday_label,
                        "badge_color": badge_color,
                        "url": url,
                    })

                # 이 쪽이 전부 마감 공고면 다음 쪽은 더 오래된 것뿐
                if all(classify.is_expired(r[5]) for r in rows):
                    break

    return jobs
