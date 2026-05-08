import os
import re
import sys
import time
import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://m.booking.naver.com/booking/13/bizes/606892/items/5755658?area=ple&lang=ko&startDate=2026-05-08&tab=book&theme=place"

# GitHub Actions Variables 또는 Secrets로 설정 가능
DATES = os.getenv("TARGET_DATES", "2026-05-08,2026-05-09").split(",")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# 예약 가능하다고 판단할 시간 패턴
TIME_PATTERN = re.compile(r"(?:오전|오후)?\s*\b([01]?\d|2[0-3])(?::([0-5]\d))?\b")

AVAILABLE_KEYWORDS = [
    "예약",
    "선택",
    "가능",
    "오전",
    "오후",
]

UNAVAILABLE_KEYWORDS = [
    "예약 가능한 시간이 없습니다",
    "예약할 수 없습니다",
    "예약이 마감",
    "휴무",
    "선택 가능한 시간이 없습니다",
]


def build_url(date: str) -> str:
    parsed = urlparse(BASE_URL)
    query = parse_qs(parsed.query)
    query["startDate"] = [date]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def notify(message: str) -> None:
    print(message)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"[WARN] Telegram notification failed: {e}", file=sys.stderr)



def normalize_time_text(text: str) -> list[str]:
    normalized = []

    for match in TIME_PATTERN.finditer(text):
        raw = match.group(0).strip()
        hour = int(match.group(1))
        minute = match.group(2) or "00"

        if "오후" in raw and hour < 12:
            hour += 12
        elif "오전" in raw and hour == 12:
            hour = 0

        # 너무 넓은 정규식이 날짜/수량을 시간으로 오인하지 않도록
        # 오전/오후가 없고 ':'도 없는 단독 숫자는 제외합니다.
        if "오전" not in raw and "오후" not in raw and ":" not in raw:
            continue

        normalized.append(f"{hour:02d}:{minute}")

    return normalized


def extract_available_times(page) -> list[str]:
    """
    네이버 예약 페이지는 구조가 바뀔 수 있으므로,
    버튼/role/전체 텍스트를 모두 확인합니다.

    시간 표기가 `14:00`일 수도 있고 `오후 2시`, `오후 3:00`처럼
    표시될 수도 있어서 normalize_time_text()로 통일합니다.
    """

    candidates = set()

    selectors = [
        "button:not([disabled])",
        "a",
        "[role='button']",
        "li",
        "div",
    ]

    for selector in selectors:
        elements = page.locator(selector)

        try:
            count = min(elements.count(), 300)
        except Exception:
            continue

        for i in range(count):
            element = elements.nth(i)

            try:
                text = element.inner_text(timeout=500).strip()
            except Exception:
                continue

            if not text:
                continue

            if not any(keyword in text for keyword in AVAILABLE_KEYWORDS):
                continue

            for time_text in normalize_time_text(text):
                candidates.add(time_text)

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        for time_text in normalize_time_text(body_text):
            candidates.add(time_text)
    except Exception:
        pass

    return sorted(candidates)


def check_date(page, date: str) -> list[str]:
    url = build_url(date)
    print(f"[INFO] Checking {date}: {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # 동적 렌더링 대기
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    # “날짜와 시간을 선택해 주세요” 영역 렌더링 여유
    time.sleep(5)

    # 가끔 모바일 페이지가 viewport에 따라 다르게 렌더링되므로 body 확인
    body_text = page.locator("body").inner_text(timeout=5000)
    print(f"[DEBUG] Body text preview for {date}: {body_text[:1000]}")

    times = extract_available_times(page)
    if times:
        return times

    # 명시적인 불가 문구가 있고, 시간 후보도 없을 때만 불가로 처리
    if any(keyword in body_text for keyword in UNAVAILABLE_KEYWORDS):
        return []

    return []


def main() -> int:
    available_results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )

        page = context.new_page()

        for raw_date in DATES:
            date = raw_date.strip()
            if not date:
                continue

            try:
                times = check_date(page, date)
                if times:
                    available_results[date] = times
            except Exception as e:
                print(f"[ERROR] Failed to check {date}: {e}", file=sys.stderr)

        browser.close()

    if available_results:
        lines = ["💇 네이버 예약 가능 시간이 발견됐습니다!"]
        for date, times in available_results.items():
            lines.append(f"- {date}: {', '.join(times)}")
            lines.append(f"  {build_url(date)}")

        notify("\n".join(lines))
        return 0

    print("[INFO] No available times found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())