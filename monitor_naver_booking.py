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
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# 예약 가능하다고 판단할 시간 패턴
TIME_PATTERN = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


def build_url(date: str) -> str:
    parsed = urlparse(BASE_URL)
    query = parse_qs(parsed.query)
    query["startDate"] = [date]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def notify(message: str) -> None:
    print(message)

    if not DISCORD_WEBHOOK_URL:
        return

    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"[WARN] Discord notification failed: {e}", file=sys.stderr)


def extract_available_times(page) -> list[str]:
    """
    네이버 예약 페이지는 구조가 바뀔 수 있으므로,
    1차로 버튼/role 기반 탐색
    2차로 전체 텍스트에서 시간 패턴 추출
    을 함께 사용합니다.
    """

    candidates = set()

    # 버튼 중 disabled가 아닌 것에서 시간처럼 보이는 텍스트 추출
    buttons = page.locator("button")
    count = buttons.count()

    for i in range(count):
        button = buttons.nth(i)

        try:
            text = button.inner_text(timeout=1000).strip()
            disabled = button.is_disabled(timeout=1000)
        except Exception:
            continue

        if disabled:
            continue

        for match in TIME_PATTERN.finditer(text):
            candidates.add(match.group(0))

    # fallback: 페이지 전체 텍스트에서 시간 추출
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        for match in TIME_PATTERN.finditer(body_text):
            candidates.add(match.group(0))
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
    time.sleep(3)

    # 가끔 모바일 페이지가 viewport에 따라 다르게 렌더링되므로 body 확인
    body_text = page.locator("body").inner_text(timeout=5000)

    # 명시적인 불가 문구가 있으면 우선 불가로 처리
    unavailable_keywords = [
        "예약 가능한 시간이 없습니다",
        "예약할 수 없습니다",
        "예약이 마감",
        "휴무",
        "선택 가능한 시간이 없습니다",
    ]

    if any(keyword in body_text for keyword in unavailable_keywords):
        return []

    return extract_available_times(page)


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