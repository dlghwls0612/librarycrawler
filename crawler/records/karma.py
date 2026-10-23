"""karma.py — 사단법인 한국기록전문가협회(KARMA) 채용공고 수집기 (개선 버전)."""
import re
import urllib.parse
from datetime import datetime, timedelta
import httpx
from bs4 import BeautifulSoup
from . import classify
from . import parsers

BASE_URL = "https://www.archivists.or.kr"
LIST_URL = "https://www.archivists.or.kr/category/NOTICE/%EC%B1%84%EC%9A%A9%EA%B3%B5%EA%B3%A0"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.archivists.or.kr/",
}


def crawl_karma(settings, max_pages=3, include_recent_closed=True):
    """KARMA 채용공고 게시판에서 서울·경기 공고를 수집.
    include_recent_closed: True면 최근 90일 이내 마감된 공고도 [마감] 상태로 수집."""
    jobs = []
    seen_ids = set()
    today = classify.get_today()

    with httpx.Client(headers=HEADERS, timeout=12.0, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            page_url = f"{LIST_URL}?page={page}"
            try:
                res = client.get(page_url)
                if res.status_code != 200:
                    break
            except Exception as e:
                print(f"[KARMA] 페이지 {page} 호출 실패: {e}")
                break

            soup = BeautifulSoup(res.text, "html.parser")

            entries = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/") and href[1:].isdigit():
                    pid = href[1:]
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        entries.append((pid, href, a))

            if not entries:
                break

            for pid, href, a_tag in entries:
                full_link = urllib.parse.urljoin(BASE_URL, href)
                container = a_tag.find_parent(["li", "div", "article"]) or a_tag
                text_block = container.get_text(" ", strip=True)

                # 제목 추출
                raw_title = ""
                # a 태그 내부의 strong이나 span 태그 찾기
                strong_el = a_tag.find(["strong", "h2", "h3", "h4", "span"])
                if strong_el and strong_el.get_text(strip=True):
                    raw_title = strong_el.get_text(strip=True)
                else:
                    lines = [ln.strip() for ln in a_tag.get_text("\n", strip=True).split("\n") if ln.strip()]
                    raw_title = lines[0] if lines else "기록관리 채용공고"

                # 태그 정리
                title = re.sub(r'^(?:\[공지\]|\[채용공고\]|\[안내\])\s*', '', raw_title).strip()
                if not title:
                    title = raw_title

                if classify.is_result_post(title, settings):
                    continue

                if not classify.is_records_relevant(title, text_block, settings):
                    continue

                org = "한국기록전문가협회 공고"
                org_match = re.search(r'(?:채용기관|기관명|채용처)\s*[:：]\s*([^\n\r,📍]+)', text_block)
                if org_match:
                    org = org_match.group(1).strip()
                else:
                    bracket = re.match(r'^\[([^\]]+)\]', title)
                    if bracket:
                        org = bracket.group(1).strip()

                location = ""
                loc_match = re.search(r'(?:근무장소|근무지|소재지)\s*[:：]\s*([^\n\r📍]+)', text_block)
                if loc_match:
                    location = loc_match.group(1).strip()

                region = classify.detect_region(title, f"{location} {text_block}", org, settings)
                if not region:
                    continue

                start_date, deadline = parsers.extract_dates(text_block)

                # 만료 공고 필터링 (너무 오래된 공고는 배제)
                is_closed = classify.is_expired(deadline) if deadline else False
                if is_closed and not include_recent_closed:
                    continue

                # 90일 이상 지난 너무 오래된 공고는 제외
                if deadline:
                    try:
                        d_obj = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
                        if (today - d_obj).days > 90:
                            continue
                    except ValueError:
                        pass

                jobtype = classify.tag_jobtype(title, text_block, settings)
                is_specialist = classify.has_specialist_credential(title, text_block, settings)

                status_text, dday_label, badge_color = classify.calc_status(start_date, deadline)

                jobs.append({
                    "id": f"karma-{pid}",
                    "source": "한국기록전문가협회",
                    "category": "협회·전문커뮤니티",
                    "title": title,
                    "organization": org,
                    "region": region,
                    "location": location or region,
                    "jobtype": jobtype,
                    "is_specialist": is_specialist,
                    "start_date": start_date,
                    "deadline": deadline,
                    "status": status_text,
                    "dday": dday_label,
                    "badge_color": badge_color,
                    "url": full_link,
                })

    return jobs
