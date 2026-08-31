import os
import re
import json
import asyncio
from playwright.async_api import async_playwright
from google import genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

async def parse_with_gemini(raw_text: str) -> dict:
    prompt = f"""
    당신은 영화, 드라마, 연극, 뮤지컬 오디션 전문 캐스팅 디렉터입니다.
    아래 공고문 본문에서 배우들이 지원하기 위해 필요한 핵심 정보만 추출해 JSON 형식으로만 응답하세요.

    반환 형식:
    {{
      "title": "작품명 및 공고 제목",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼 중 택1",
      "production": "제작사 또는 극단명",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령대 (예: 20대 초반, 20대~30대)",
      "role": "배역명 및 캐릭터 요약",
      "deadline": "마감일(YYYY-MM-DD, 없으면 2099-12-31)",
      "requirements": "자격요건 및 촬영/오디션 일정 요약",
      "subject_format": "지정 메일제목 양식 (없으면 '[작품명_지원] 배역_이름_연락처')"
    }}

    [공고 본문]:
    {raw_text[:3500]}
    """
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        clean = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean)
    except Exception as e:
        print(f"[-] AI 분석 에러: {e}")
        return {}

async def run_crawler():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 1. 필름메이커스 오디션 타깃
        try:
            print("[*] 필름메이커스 배우 오디션 스캔 중...")
            await page.goto("https://www.filmmakers.co.kr/actorsAudition", wait_until="domcontentloaded", timeout=25000)
            links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            
            # /actorsAudition/숫자 패턴 추출
            target_links = list(set([l for l in links if re.search(r'/actorsAudition/\d+', l)]))
            print(f"[*] 필름메이커스 공고 {len(target_links)}개 포착")

            for post_url in target_links[:8]:
                exists = supabase.table("auditions").select("id").eq("source_url", post_url).execute()
                if exists.data:
                    print(f"[=] 이미 저장된 공고: {post_url}")
                    continue

                try:
                    await page.goto(post_url, timeout=15000)
                    body = await page.inner_text("body")
                    
                    emails = re.findall(EMAIL_REGEX, body)
                    phones = re.findall(PHONE_REGEX, body)
                    parsed = await parse_with_gemini(body)
                    
                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "제목 미상",
                        "category": parsed.get("category") or "장편/단편영화",
                        "production": parsed.get("production") or "제작팀",
                        "gender": parsed.get("gender") or "무관",
                        "age": parsed.get("age") or "연령 무관",
                        "role": parsed.get("role") or "주조연 모집",
                        "deadline": parsed.get("deadline") or "2099-12-31",
                        "email": emails[0] if emails else "본문 참조",
                        "phone": phones[0] if phones else "비공개/이메일 접수",
                        "subject_format": parsed.get("subject_format") or "[지원] 이름_연락처",
                        "requirements": parsed.get("requirements") or "",
                        "source": "필름메이커스"
                    }

                    supabase.table("auditions").insert(payload).execute()
                    print(f"[+] 저장 성공: {payload['title']}")
                except Exception as ex:
                    print(f"[-] 본문 수집 실패 ({post_url}): {ex}")
        except Exception as e:
            print(f"[-] 필름메이커스 접근 실패: {e}")

        # 2. OTR 연극/뮤지컬 오디션 타깃
        try:
            print("[*] OTR 연극/뮤지컬 오디션 스캔 중...")
            await page.goto("https://otr.co.kr/audition/", wait_until="domcontentloaded", timeout=25000)
            otr_links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            
            target_otr = list(set([l for l in otr_links if "audition" in l and ("id=" in l or "view" in l or "read" in l)]))
            print(f"[*] OTR 공고 {len(target_otr)}개 포착")

            for post_url in target_otr[:6]:
                exists = supabase.table("auditions").select("id").eq("source_url", post_url).execute()
                if exists.data:
                    continue

                try:
                    await page.goto(post_url, timeout=15000)
                    body = await page.inner_text("body")
                    
                    emails = re.findall(EMAIL_REGEX, body)
                    phones = re.findall(PHONE_REGEX, body)
                    parsed = await parse_with_gemini(body)
                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "제목 미상",
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
                    print(f"[+] 저장 성공: {payload['title']}")
                except Exception as ex:
                    print(f"[-] OTR 본문 실패: {ex}")
        except Exception as e:
            print(f"[-] OTR 접근 실패: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
