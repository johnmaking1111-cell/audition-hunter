import os
import re
import json
import asyncio
from playwright.async_api import async_playwright
import google.generativeai as genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 타깃 연결: {SUPABASE_URL}")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

async def parse_with_gemini(raw_text: str) -> dict:
    prompt = f"""
    당신은 영화, 드라마, 연극, 뮤지컬 오디션 전문 캐스팅 디렉터 AI입니다.
    아래 공고문 본문에서 배우들이 지원하기 위해 필요한 핵심 정보만 추출해 JSON으로 응답하세요.

    JSON 포맷:
    {{
      "title": "작품명 및 공고 제목",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼 중 택1",
      "production": "제작사 또는 극단명",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령대 (예: 20대 초반, 25~35세, 전 연령)",
      "role": "배역명 및 캐릭터 요약",
      "deadline": "마감일(YYYY-MM-DD 포맷, 미기재시 2099-12-31)",
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
        print(f"[-] Gemini 정제 실패: {e}")
        return {}

async def run_crawler():
    async with async_playwright() as p:
        # 봇 탐지 우회 브라우저 파라미터 적용
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ko-KR",
            timezone_id="Asia/Seoul"
        )
        page = await context.new_page()

        # ==========================================
        # [1] 필름메이커스 레이더 (영화/단편/OTT)
        # ==========================================
        print("\n[*] === [1] 필름메이커스 공고 레이더 침투 ===")
        try:
            await page.goto("https://www.filmmakers.co.kr/actorsAudition", wait_until="load", timeout=40000)
            await page.wait_for_timeout(3000)

            # 모든 a 태그 링크 href 수집
            hrefs = await page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => a.href).filter(h => h && h.includes('actorsAudition'));
            }''')

            target_urls = set()
            for h in hrefs:
                match = re.search(r'/actorsAudition/(\d+)', h)
                if match:
                    target_urls.add(f"https://www.filmmakers.co.kr/actorsAudition/{match.group(1)}")

            print(f"[*] 필름메이커스 유효 공고 {len(target_urls)}건 포착 완료")

            for target_url in list(target_urls)[:8]:
                exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
                if exists.data:
                    print(f"[=] 기존 수집 공고 스킵: {target_url}")
                    continue

                try:
                    await page.goto(target_url, wait_until="load", timeout=20000)
                    await page.wait_for_timeout(1500)
                    body_text = await page.inner_text("body")

                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)

                    if not parsed:
                        continue

                    payload = {
                        "source_url": target_url,
                        "title": parsed.get("title") or "필름메이커스 오디션",
                        "category": parsed.get("category") or "장편/단편영화",
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
                    print(f"[+] [필름메이커스 DB 저장 완료] {payload['title']}")
                except Exception as ex:
                    print(f"[-] 상세글 파싱 실패 ({target_url}): {ex}")

        except Exception as e:
            print(f"[-] 필름메이커스 진입 에러: {e}")

        # ==========================================
        # [2] OTR 레이더 (연극/뮤지컬/공연)
        # ==========================================
        print("\n[*] === [2] OTR 연극/뮤지컬 레이더 침투 ===")
        try:
            await page.goto("https://otr.co.kr/audition/", wait_until="load", timeout=40000)
            await page.wait_for_timeout(3000)

            otr_hrefs = await page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => a.href).filter(h => h && h.includes('audition'));
            }''')

            otr_targets = set()
            for h in otr_hrefs:
                if any(q in h for q in ["board_no=", "id=", "/view/", "no="]) and not h.endswith("#"):
                    otr_targets.add(h)

            print(f"[*] OTR 유효 공고 {len(otr_targets)}건 포착 완료")

            for target_url in list(otr_targets)[:6]:
                exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
                if exists.data:
                    continue

                try:
                    await page.goto(target_url, wait_until="load", timeout=20000)
                    await page.wait_for_timeout(1500)
                    body_text = await page.inner_text("body")

                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)

                    if not parsed:
                        continue

                    payload = {
                        "source_url": target_url,
                        "title": parsed.get("title") or "연극/뮤지컬 오디션",
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
                    print(f"[+] [OTR DB 저장 완료] {payload['title']}")
                except Exception as ex:
                    print(f"[-] OTR 상세글 파싱 실패 ({target_url}): {ex}")

        except Exception as e:
            print(f"[-] OTR 진입 에러: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
