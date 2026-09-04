"""fetch.py — 봇우회 페이지 수집.
httpx(가벼움) 기본, engine=playwright면 헤드리스 렌더. playwright 미설치 시 httpx로 폴백.
정부/도서관 사이트는 인증서 문제가 잦아 verify=False(관대) + 요청 간격/재시도.
"""
import ssl
import time
import warnings

import httpx

warnings.filterwarnings("ignore")  # SSL 등 경고 억제


def _legacy_ssl_context():
    """Ubuntu(OpenSSL 3)에서 한국 관공서·재단의 옛 SSL(레거시 재협상, 낮은 보안레벨)에 접속 가능하게.
    이게 없으면 GitHub 러너에서 sslv3 handshake failure 로 성북 등 다수가 안 걸림."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT      # 안전하지 않은 레거시 재협상 허용
    except AttributeError:
        ctx.options |= 0x4
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")            # 오래된 암호/키 허용
    except ssl.SSLError:
        pass
    return ctx


_SSL = _legacy_ssl_context()

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
                              follow_redirects=True, verify=_SSL) as c:
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
        # 도커/저메모리 환경에서 크롬 안정화 (/dev/shm 부족·샌드박스 이슈 회피)
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        _pw = (pw, browser)
    return _pw[1]


def fetch_playwright(url, timeout, ua):
    browser = _get_pw()
    ctx = browser.new_context(user_agent=ua, locale="ko-KR", ignore_https_errors=True)
    page = ctx.new_page()
    try:
        # networkidle 을 무한정 기다리면 광고/폴링 사이트에서 안 끝남 →
        # domcontentloaded 후 networkidle 을 '최대 6초'만 기다리고 넘어감(SPA 렌더 시간은 확보)
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(600)
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
    timeout = min(req.get("timeout_seconds", 20), 12)   # 느린 사이트에 오래 매달리지 않음
    ua = req.get("user_agent", DEFAULT_UA)
    retries = min(req.get("retries", 2), 1)
    if engine == "playwright":
        try:
            return fetch_playwright(url, timeout, ua)
        except Exception:
            # playwright 미설치/실패 → httpx 폴백(SPA는 놓칠 수 있음)
            return fetch_httpx(url, timeout, ua, retries)
    # httpx 우선, 실패(SSL/차단 등)하면 브라우저로 재시도 = 안전망
    try:
        return fetch_httpx(url, timeout, ua, retries)
    except Exception:
        return fetch_playwright(url, timeout, ua)
