import os
import re
import json
import asyncio
from urllib.parse import urljoin
from playwright.async_api import async_playwright
import google.generativeai as genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 타깃: {SUPABASE_URL}")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

async def parse_with_gemini(raw_text: str) -> dict:
    prompt = f"""
    당신은 영화, 드라마, 연극, 뮤지컬 오디션 전문 캐스팅 디렉터입니다.
    아래 공고문 본문에서 배우들이 지원하기 위해 필요한 핵심 정보만 추출해 JSON으로 응답하세요.

    JSON 포맷:
    {{
      "title": "작품명 및 공고 제목",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼 중 택1",
      "production": "제작사 또는 극단명",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령대 (예: 20대 초반, 25~35세, 전 연령)",
      "role": "배역명 및 캐릭터 요약",
      "deadline": "마감일(YYYY-MM-DD 포맷, 없으면 2099-12-31)",
      "requirements": "자격요건 및 오디션/촬영 일정 요약",
      "subject_format": "지정 메일제목 양식 (없으면 '[작품명_지원] 배역_이름_연락처')"
    }}

    [공고 본문]:
    {raw_text[:3500]}
    """
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = await model.generate_content_async(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[-] AI 분석 예외: {e}")
        return {}

async def run_crawler():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
        )
        page = await context.new_page()

        # ==========================================
        # [1] 필름메이커스 정밀 타격
        # ==========================================
        print("\n[*] === [1] 필름메이커스 레이더 가동 ===")
        try:
            target_base = "https://www.filmmakers.co.kr/actorsAudition"
            await page.goto(target_base, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            
            current_title = await page.title()
            print(f"[*] 필름메이커스 진입 성공: [{current_title}]")

            # 모든 a 태그의 href와 text를 긁어 XE 게시글 패턴 전수 분석
            links = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('a')).map(a => a.getAttribute('href') || '');
            }''')

            film_targets = set()
            for l in links:
                if not l or l.startswith("#") or "javascript" in l:
                    continue
                # XE 패턴: document_srl 숫자 또는 actorsAudition/숫자 또는 숫자만 있는 링크
                if re.search(r'(document_srl=\d+|actorsAudition/\d+|/\d{7,})', l):
                    full_link = urljoin("https://www.filmmakers.co.kr/actorsAudition", l)
                    film_targets.add(full_link)

            print(f"[+] 필름메이커스 정밀 타깃 {len(film_targets)}건 포착!")

            for post_url in list(film_targets)[:10]:
                exists = supabase.table("auditions").select("id").eq("source_url", post_url).execute()
                if exists.data:
                    print(f"[=] 중복 스킵: {post_url}")
                    continue

                try:
                    await page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1000)
                    body_text = await page.inner_text("body")

                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)

                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "필름메이커스 공고",
                        "category": parsed.get("category") or "영화",
                        "production": parsed.get("production") or "제작팀",
                        "gender": parsed.get("gender") or "무관",
                        "age": parsed.get("age") or "연령 무관",
                        "role": parsed.get("role") or "배역 모집",
                        "deadline": parsed.get("deadline") or "2099-12-31",
                        "email": emails[0] if emails else "본문 참조",
                        "phone": phones[0] if phones else "비공개/이메일 접수",
                        "subject_format": parsed.get("subject_format") or "[지원] 이름_연락처",
                        "requirements": parsed.get("requirements") or "",
                        "source": "필름메이커스"
                    }

                    supabase.table("auditions").insert(payload).execute()
                    print(f"[🔥 DB 적재 성공] {payload['title']}")
                except Exception as ex:
                    print(f"[-] 공고 본문 파싱 실패 ({post_url}): {ex}")

        except Exception as e:
            print(f"[-] 필름메이커스 진입 실패: {e}")

        # ==========================================
        # [2] OTR 연극/뮤지컬 정밀 타격
        # ==========================================
        print("\n[*] === [2] OTR 연극/뮤지컬 레이더 가동 ===")
        try:
            await page.goto("https://otr.co.kr/audition/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            otr_title = await page.title()
            print(f"[*] OTR 진입 성공: [{otr_title}]")

            otr_links = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('a')).map(a => a.getAttribute('href') || '');
            }''')

            otr_targets = set()
            for l in otr_links:
                if not l or l.startswith("#") or "javascript" in l:
                    continue
                if any(k in l for k in ["board_no=", "id=", "/audition/", "view", "read"]):
                    full_link = urljoin("https://otr.co.kr/audition/", l)
                    if full_link != "https://otr.co.kr/audition/":
                        otr_targets.add(full_link)

            print(f"[+] OTR 정밀 타깃 {len(otr_targets)}건 포착!")

            for post_url in list(otr_targets)[:8]:
                exists = supabase.table("auditions").select("id").eq("source_url", post_url).execute()
                if exists.data:
                    continue

                try:
                    await page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1000)
                    body_text = await page.inner_text("body")

                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)

                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "연극/뮤지컬 공고",
                        "category": parsed.get("category") or "연극",
                        "production": parsed.get("production") or "극단",
                        "gender": parsed.get("gender") or "무관",
                        "age": parsed.get("age") or "전 연령",
                        "role": parsed.get("role") or "배역 모집",
                        "deadline": parsed.get("deadline") or "2099-12-31",
                        "email": emails[0] if emails else "본문 참조",
                        "phone": phones[0] if phones else "비공개/이메일 접수",
                        "subject_format": parsed.get("subject_format") or "[지원] 이름_연락처",
                        "requirements": parsed.get("requirements") or "",
                        "source": "OTR"
                    }

                    supabase.table("auditions").insert(payload).execute()
                    print(f"[🔥 DB 적재 성공] {payload['title']}")
                except Exception as ex:
                    print(f"[-] OTR 공고 수집 실패 ({post_url}): {ex}")

        except Exception as e:
            print(f"[-] OTR 진입 실패: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
