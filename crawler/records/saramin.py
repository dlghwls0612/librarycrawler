"""saramin.py — 사람인 서울·경기 기록관리 채용공고 수집기."""
import re
import urllib.parse
from datetime import datetime, timedelta, date
import httpx
from bs4 import BeautifulSoup
from . import classify
from . import parsers

BASE_URL = "https://www.saramin.co.kr"
SEARCH_URL = "https://www.saramin.co.kr/zf_user/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.saramin.co.kr/",
}

SARAMIN_REGION = {
    "서울": "서울", "경기": "경기",
    "서울전체": "서울", "경기전체": "경기",
}


def crawl_saramin(settings, keywords=None):
    """사람인에서 서울·경기 기록관리(전문요원, 연구사, 아키비스트 등) 채용공고 수집."""
    if keywords is None:
        keywords = ["기록물관리", "기록연구", "아키비스트", "기록관리전문요원"]

    jobs = []
    seen_ids = set()
    today = classify.get_today()

    with httpx.Client(headers=HEADERS, timeout=12.0, follow_redirects=True) as client:
        for kw in keywords:
            params = {
                "searchword": kw,
                "loc_mcd": "101000,102000",  # 서울(101000), 경기(102000)
            }
            try:
                res = client.get(SEARCH_URL, params=params)
                if res.status_code != 200:
                    continue
            except Exception as e:
                print(f"[Saramin] 검색어 '{kw}' 호출 실패: {e}")
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            items = soup.select(".item_recruit")

            for item in items:
                # 공고 ID 추출
                rid = item.get("value") or ""
                if not rid:
                    id_attr = item.get("id") or ""
                    rm = re.search(r'rec_(\d+)', id_attr)
                    if rm:
                        rid = rm.group(1)

                title_el = item.select_one(".job_tit a")
                corp_el = item.select_one(".corp_name a")
                loc_el = item.select_one(".work_place")
                date_el = item.select_one(".job_date .date")
                cond_el = item.select_one(".job_condition")

                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                corp = corp_el.get_text(strip=True) if corp_el else "기관명 미확인"
                loc_text = loc_el.get_text(strip=True) if loc_el else ""
                cond_text = cond_el.get_text(" ", strip=True) if cond_el else ""
                raw_date = date_el.get_text(strip=True) if date_el else ""

                href = title_el.get("href") or ""
                if not rid:
                    idx_match = re.search(r'rec_idx=(\d+)', href)
                    if idx_match:
                        rid = idx_match.group(1)

                item_id = rid or href
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                full_url = urllib.parse.urljoin(BASE_URL, href)

                # 1. 결과 공고 배제
                if classify.is_result_post(title, settings):
                    continue

                # 2. 기록관리 관련성 확인
                full_text = f"{title} {corp} {cond_text}"
                if not classify.is_records_relevant(title, full_text, settings):
                    continue

                # 3. 서울·경기 지역 판별
                region = classify.detect_region(title, f"{loc_text} {corp}", corp, settings)
                # 사람인 loc_mcd 필터로 이미 서울·경기로 검색했으나, 확실히 보정
                if not region:
                    if any(s in loc_text for s in ["서울", "강남", "서초", "송파", "영등포", "종로", "마포", "용산"]):
                        region = "서울"
                    elif any(g in loc_text for g in ["경기", "성남", "수원", "고양", "용인", "안양", "화성"]):
                        region = "경기"
                    else:
                        region = "서울"  # loc_mcd 101000/102000 기본

                # 4. 마감일 판정 (D-day 또는 날짜 파싱)
                deadline = None
                start_date = None

                # 사람인 날짜 표기 파싱: 'D-3', '오늘마감', '~ 11/15(일)', '진행예정', '상시채용'
                dday_match = re.search(r'D-(\d+)', raw_date)
                date_match = re.search(r'~\s*(\d{1,2})[.\-/](\d{1,2})', raw_date)

                if dday_match:
                    diff_days = int(dday_match.group(1))
                    deadline = (today + timedelta(days=diff_days)).strftime("%Y-%m-%d")
                elif "오늘마감" in raw_date:
                    deadline = today.strftime("%Y-%m-%d")
                elif date_match:
                    mo, d = int(date_match.group(1)), int(date_match.group(2))
                    try:
                        cand_year = today.year
                        cand_date = date(cand_year, mo, d)
                        if cand_date < today:
                            cand_date = date(cand_year + 1, mo, d)
                        deadline = cand_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

                # 만료 공고 필터
                if deadline and classify.is_expired(deadline):
                    continue

                # 5. 고용 형태 및 전문요원 자격 태깅
                jobtype = classify.tag_jobtype(title, cond_text, settings)
                is_specialist = classify.has_specialist_credential(title, f"{cond_text} {corp}", settings)

                # 6. 접수 상태 및 D-Day 판정
                if "진행예정" in raw_date:
                    status_text = "접수예정"
                    dday_label = "접수예정"
                    badge_color = "badge-warning"
                else:
                    status_text, dday_label, badge_color = classify.calc_status(start_date, deadline)

                jobs.append({
                    "id": f"saramin-{rid}",
                    "source": "사람인",
                    "category": "문화재단·공공·민간",
                    "title": title,
                    "organization": corp,
                    "region": region,
                    "location": loc_text or region,
                    "jobtype": jobtype,
                    "is_specialist": is_specialist,
                    "start_date": start_date,
                    "deadline": deadline,
                    "status": status_text,
                    "dday": dday_label,
                    "badge_color": badge_color,
                    "url": full_url,
                })

    return jobs
