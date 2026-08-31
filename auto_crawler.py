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

print(f"[*] Supabase 타깃: {SUPABASE_URL}")
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
        # 브라우저 실행 및 실제 일반 사용자 브라우저 지문 위장
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        # 1. 필름메이커스 사냥
        try:
            print("\n[*] === [1] 필름메이커스 배우 오디션 잠입 ===")
            await page.goto("https://www.filmmakers.co.kr/actorsAudition", wait_until="networkidle", timeout=30000)
            
            # 페이지 내 모든 링크 수집 후 정규식 필터링
            links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            film_links = []
            for l in links:
                if "/actorsAudition/" in l:
                    clean_url = l.split("?")[0].split("#")[0]
                    if re.search(r'/actorsAudition/\d+$', clean_url) and clean_url not in film_links:
                        film_links.append(clean_url)

            print(f"[*] 필름메이커스 실시간 공고 {len(film_links)}개 탐지 완료")

            for target_url in film_links[:8]:
                exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
                if exists.data:
                    print(f"[=] 이미 저장된 공고 스킵: {target_url}")
                    continue

                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
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

                    insert_res = supabase.table("auditions").insert(payload).execute()
                    print(f"[+] [필름메이커스 DB 적재 성공] {payload['title']}")
                except Exception as ex:
                    print(f"[-] 상세 공고 수집 에러 ({target_url}): {ex}")

        except Exception as e:
            print(f"[-] 필름메이커스 목록 접근 실패: {e}")

        # 2. OTR 사냥
        try:
            print("\n[*] === [2] OTR 연극/뮤지컬 오디션 잠입 ===")
            await page.goto("https://otr.co.kr/audition/", wait_until="networkidle", timeout=30000)
            
            otr_raw_links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            otr_links = []
            for l in otr_raw_links:
                if "audition" in l and ("id=" in l or "view" in l or "read" in l):
                    if l not in otr_links and not l.endswith("#"):
                        otr_links.append(l)

            print(f"[*] OTR 실시간 공고 {len(otr_links)}개 탐지 완료")

            for target_url in otr_links[:6]:
                exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
                if exists.data:
                    print(f"[=] 이미 저장된 공고 스킵: {target_url}")
                    continue

                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
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

                    insert_res = supabase.table("auditions").insert(payload).execute()
                    print(f"[+] [OTR DB 적재 성공] {payload['title']}")
                except Exception as ex:
                    print(f"[-] OTR 상세 공고 수집 에러 ({target_url}): {ex}")

        except Exception as e:
            print(f"[-] OTR 목록 접근 실패: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
