"""fetch.py — 봇우회 페이지 수집.
httpx(가벼움) 기본, engine=playwright면 헤드리스 렌더. playwright 미설치 시 httpx로 폴백.
정부/도서관 사이트는 인증서 문제가 잦아 verify=False(관대) + 요청 간격/재시도.
"""
import time
import warnings

import httpx

warnings.filterwarnings("ignore")  # SSL 등 경고 억제

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 (library-jobs-crawler; respectful)")

_pw = None  # playwright 브라우저 재사용


def _headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }


def fetch_httpx(url, timeout, ua, retries):
    last = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(headers=_headers(ua), timeout=timeout,
                              follow_redirects=True, verify=False) as c:
                r = c.get(url)
                r.raise_for_status()
                # 인코딩 자동 보정(euc-kr 사이트 대비)
                if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.text
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _get_pw():
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        _pw = (pw, browser)
    return _pw[1]


def fetch_playwright(url, timeout, ua):
    browser = _get_pw()
    ctx = browser.new_context(user_agent=ua, locale="ko-KR", ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        page.wait_for_timeout(800)  # 지연 렌더 대기
        return page.content()
    finally:
        ctx.close()


def close():
    global _pw
    if _pw:
        try:
            _pw[1].close(); _pw[0].stop()
        except Exception:
            pass
        _pw = None


def fetch(url, engine, settings):
    req = settings.get("request", {})
    timeout = req.get("timeout_seconds", 20)
    ua = req.get("user_agent", DEFAULT_UA)
    retries = req.get("retries", 2)
    if engine == "playwright":
        try:
            return fetch_playwright(url, timeout, ua)
        except Exception:
            # playwright 미설치/실패 → httpx 폴백(SPA는 놓칠 수 있음)
            return fetch_httpx(url, timeout, ua, retries)
    return fetch_httpx(url, timeout, ua, retries)
