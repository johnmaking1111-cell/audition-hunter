import os
import re
import json
import asyncio
from playwright.async_api import async_playwright
import google.generativeai as genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

async def parse_with_gemini(raw_text: str) -> dict:
    """공고문에서 핵심 항목 구조화 추출"""
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={"response_mime_type": "application/json"}
    )
    prompt = f"""
    당신은 영화, 드라마, 연극, 뮤지컬 오디션 전문 캐스팅 디렉터 AI입니다.
    아래 공고문 본문에서 핵심 정보를 추출해 정확한 JSON으로 반환하세요.

    - title: 작품명 및 공고 제목
    - category: 장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼 중 택1
    - production: 제작사 또는 극단명
    - gender: 남, 여, 무관 중 택1
    - age: 모집 연령대 (예: 20대 초반, 25~35세, 전 연령)
    - role: 배역명 및 캐릭터 설명 요약
    - deadline: 마감일 (YYYY-MM-DD 포맷, 기한 없거나 상시면 '2099-12-31')
    - requirements: 자격요건, 준비물, 미팅/촬영 장소 요약
    - subject_format: 지정된 메일 제목 양식 (없으면 '[작품명_지원] 배역명_이름_연락처' 생성)

    [본문]:
    {raw_text[:3500]}
    """
    response = await model.generate_content_async(prompt)
    return json.loads(response.text)

async def run_crawler():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        targets = [
            {"source": "필름메이커스", "url": "https://www.filmmakers.co.kr/actorsAudition"},
            {"source": "OTR", "url": "https://otr.co.kr/audition/"}
        ]

        for target in targets:
            try:
                print(f"[*] {target['source']} 스캔 시작...")
                await page.goto(target["url"], wait_until="domcontentloaded", timeout=20000)
                links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
                
                post_urls = list(set([l for l in links if any(k in l for k in ["article", "view", "read", "board"])]))

                for post_url in post_urls[:15]:
                    # 중복 공고 스킵
                    exists = supabase.table("auditions").select("id").eq("source_url", post_url).execute()
                    if exists.data:
                        continue

                    try:
                        await page.goto(post_url, timeout=12000)
                        body = await page.inner_text("body")
                        
                        emails = re.findall(EMAIL_REGEX, body)
                        phones = re.findall(PHONE_REGEX, body)
                        
                        parsed = await parse_with_gemini(body)
                        
                        payload = {
                            "source_url": post_url,
                            "title": parsed.get("title", "제목 미상"),
                            "category": parsed.get("category", "기타"),
                            "production": parsed.get("production", "제작팀"),
                            "gender": parsed.get("gender", "무관"),
                            "age": parsed.get("age", "연령 무관"),
                            "role": parsed.get("role", "배역 모집"),
                            "deadline": parsed.get("deadline", "2099-12-31"),
                            "email": emails[0] if emails else "본문 참조",
                            "phone": phones[0] if phones else "비공개/이메일 접수",
                            "subject_format": parsed.get("subject_format", "[지원] 이름_연락처"),
                            "requirements": parsed.get("requirements", ""),
                            "source": target["source"]
                        }
                        
                        supabase.table("auditions").insert(payload).execute()
                        print(f"[+] 성공: {payload['title']}")
                    except Exception as e:
                        continue
            except Exception as e:
                print(f"[-] 접근 실패 ({target['source']}): {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
