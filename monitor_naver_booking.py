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
TIME_PATTERN = re.compile(r"^(?:오전|오후)?\s*([01]?\d|2[0-3])(?::([0-5]\d))?\s*시?$")

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



def normalize_time_text(text: str, period: str | None = None) -> str | None:
    text = text.strip()
    match = TIME_PATTERN.match(text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = match.group(2) or "00"
    effective_period = period

    if "오전" in text:
        effective_period = "오전"
    elif "오후" in text:
        effective_period = "오후"

    if effective_period == "오후" and hour < 12:
        hour += 12
    elif effective_period == "오전" and hour == 12:
        hour = 0

    return f"{hour:02d}:{minute}"


def get_period_from_y(y: float, period_markers: list[tuple[str, float]]) -> str | None:
    current_period = None

    for period, marker_y in sorted(period_markers, key=lambda item: item[1]):
        if y >= marker_y:
            current_period = period

    return current_period


def is_visually_available_time_element(element) -> bool:
    try:
        return bool(
            element.evaluate(
                """
                (el) => {
                  const text = (el.innerText || el.textContent || '').trim();
                  const ariaDisabled = el.getAttribute('aria-disabled') === 'true';
                  const disabled = el.disabled === true || el.hasAttribute('disabled');
                  const className = String(el.className || '').toLowerCase();
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();

                  if (!text) return false;
                  if (disabled || ariaDisabled) return false;
                  if (className.includes('disabled') || className.includes('unavailable')) return false;
                  if (style.display === 'none' || style.visibility === 'hidden') return false;
                  if (style.pointerEvents === 'none') return false;
                  if (Number(style.opacity) < 0.5) return false;
                  if (rect.width <= 0 || rect.height <= 0) return false;

                  const colorMatch = style.color.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                  if (colorMatch) {
                    const r = Number(colorMatch[1]);
                    const g = Number(colorMatch[2]);
                    const b = Number(colorMatch[3]);

                    // 예약 불가 시간은 보통 연한 회색 텍스트로 렌더링됩니다.
                    if (r >= 170 && g >= 170 && b >= 170) return false;
                  }

                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def extract_available_times(page) -> list[str]:
    """
    네이버 예약 페이지의 body 전체 텍스트에는 예약 불가 시간도 함께 들어옵니다.
    그래서 전체 텍스트에서 시간을 긁지 않고, 실제 시간 버튼처럼 보이는 요소만 검사합니다.
    """

    candidates = set()
    debug_rows = []
    period_markers = []

    for period in ["오전", "오후"]:
        labels = page.get_by_text(period, exact=True)
        try:
            count = labels.count()
        except Exception:
            continue

        for i in range(count):
            try:
                box = labels.nth(i).bounding_box(timeout=500)
            except Exception:
                box = None

            if box:
                period_markers.append((period, box["y"]))

    selectors = [
        "button",
        "[role='button']",
        "a",
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

            # `예약하기` 같은 문구나 큰 컨테이너가 아니라, 시간 하나만 적힌 요소만 봅니다.
            if not TIME_PATTERN.match(text):
                continue

            # 달력 날짜 버튼은 `1`, `14`, `23`처럼 단독 숫자로 되어 있어서
            # 시간 후보에서 제외합니다. 실제 시간 버튼은 `10:00`, `2:00`처럼 ':'가 있습니다.
            if ":" not in text and "오전" not in text and "오후" not in text:
                continue

            try:
                box = element.bounding_box(timeout=500)
            except Exception:
                box = None

            period = get_period_from_y(box["y"], period_markers) if box else None
            normalized = normalize_time_text(text, period)
            available = is_visually_available_time_element(element)

            debug_rows.append(f"{text} -> {normalized}, period={period}, available={available}")

            if normalized and available:
                candidates.add(normalized)

    print(f"[DEBUG] Time candidates: {debug_rows}")
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