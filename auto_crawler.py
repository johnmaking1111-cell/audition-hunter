import os
import re
import json
import asyncio
import urllib.parse
from playwright.async_api import async_playwright
import google.generativeai as genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 망 연결: {SUPABASE_URL}")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

async def parse_with_gemini(raw_text: str) -> dict:
    prompt = f"""
    당신은 영화, 방송, 연극, 뮤지컬, 광고 오디션 전문 캐스팅 디렉터 AI입니다.
    아래 수집된 공고문 텍스트에서 배우들이 꼭 알아야 할 핵심 정보를 추출해 순수 JSON으로만 응답하세요.

    JSON 포맷:
    {{
      "title": "작품명 및 공고 제목",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼, 에이전시/공채 중 택1",
      "production": "제작사, 극단, 또는 기획사명",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령대 (예: 20대, 20대~30대, 전 연령)",
      "role": "배역명 및 모집 요강 요약",
      "deadline": "마감일(YYYY-MM-DD 포맷, 상시/미기재 시 2099-12-31)",
      "requirements": "자격요건, 촬영/오디션 일정 요약",
      "subject_format": "지정 메일제목 양식 (없으면 '[작품명_지원] 배역_이름_연락처')"
    }}

    [공고 원문]:
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
        print(f"[-] AI 분석 오류: {e}")
        return {}

async def save_to_supabase(payload: dict):
    try:
        exists = supabase.table("auditions").select("id").eq("source_url", payload["source_url"]).execute()
        if exists.data:
            print(f"[=] 이미 존재하는 공고: {payload['title']}")
            return False
        
        res = supabase.table("auditions").insert(payload).execute()
        print(f"[🔥 DB 적재 성공] {payload['category']} | {payload['title']} | 출처: {payload['source']}")
        return True
    except Exception as e:
        print(f"[-] DB 저장 예외: {e}")
        return False

async def run_crawler():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ko-KR"
        )
        page = await context.new_page()

        # ==========================================
        # [1] 캐스팅114 & 종합 캐스팅 네트워크 사냥
        # ==========================================
        print("\n[*] === [1] 캐스팅114 / 캐스팅 네트워크 탐색 ===")
        try:
            target_url = "http://www.casting114.net"
            await page.goto(target_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            audition_links = list(set([l for l in links if any(k in l.lower() for k in ["audition", "cast", "view", "board", "read"])]))
            print(f"[*] 캐스팅114 타깃 링크 {len(audition_links)}건 포착")

            for post_url in audition_links[:6]:
                try:
                    await page.goto(post_url, timeout=12000)
                    body_text = await page.inner_text("body")
                    if len(body_text.strip()) < 100:
                        continue

                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)
                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "캐스팅 공고",
                        "category": parsed.get("category") or "OTT/드라마",
                        "production": parsed.get("production") or "제작팀",
                        "gender": parsed.get("gender") or "무관",
                        "age": parsed.get("age") or "전 연령",
                        "role": parsed.get("role") or "배역 모집",
                        "deadline": parsed.get("deadline") or "2099-12-31",
                        "email": emails[0] if emails else "본문 참조",
                        "phone": phones[0] if phones else "비공개/이메일 접수",
                        "subject_format": parsed.get("subject_format") or "[지원] 배역_이름_연락처",
                        "requirements": parsed.get("requirements") or "",
                        "source": "캐스팅114"
                    }
                    await save_to_supabase(payload)
                except Exception:
                    continue
        except Exception as e:
            print(f"[-] 캐스팅114 탐색 예외: {e}")

        # ==========================================
        # [2] 공연예술/뮤지컬 전문 포털 사냥 (더스테이지 / 스테이지톡 계열)
        # ==========================================
        print("\n[*] === [2] 전국 공연예술·뮤지컬 공고망 탐색 ===")
        try:
            await page.goto("http://www.themusical.co.kr/Audition", timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            musical_links = list(set([l for l in links if "Audition/Detail" in l or "audition" in l.lower()]))
            print(f"[*] 뮤지컬/연극 타깃 링크 {len(musical_links)}건 포착")

            for post_url in musical_links[:6]:
                try:
                    await page.goto(post_url, timeout=12000)
                    body_text = await page.inner_text("body")
                    emails = re.findall(EMAIL_REGEX, body_text)
                    phones = re.findall(PHONE_REGEX, body_text)
                    parsed = await parse_with_gemini(body_text)
                    if not parsed:
                        continue

                    payload = {
                        "source_url": post_url,
                        "title": parsed.get("title") or "뮤지컬/연극 오디션",
                        "category": "뮤지컬",
                        "production": parsed.get("production") or "제작사",
                        "gender": parsed.get("gender") or "무관",
                        "age": parsed.get("age") or "전 연령",
                        "role": parsed.get("role") or "배역 모집",
                        "deadline": parsed.get("deadline") or "2099-12-31",
                        "email": emails[0] if emails else "본문 참조",
                        "phone": phones[0] if phones else "비공개/이메일 접수",
                        "subject_format": parsed.get("subject_format") or "[뮤지컬지원] 배역_이름",
                        "requirements": parsed.get("requirements") or "",
                        "source": "더뮤지컬"
                    }
                    await save_to_supabase(payload)
                except Exception:
                    continue
        except Exception as e:
            print(f"[-] 뮤지컬망 탐색 예외: {e}")

        # ==========================================
        # [3] 전국 실시간 오디션 웹 레이더 (다중 검색 인덱스 사냥)
        # ==========================================
        print("\n[*] === [3] 전국 실시간 오디션 웹 레이더 전면 가동 ===")
        search_queries = ["배우 오디션 공고 접수", "드라마 단편영화 배우 모집 공고", "신인배우 오디션 모집 프로필 접수"]
        
        for q in search_queries:
            try:
                encoded_q = urllib.parse.quote(q)
                target_search_url = f"https://html.duckduckgo.com/html/?q={encoded_q}"
                await page.goto(target_search_url, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)

                result_links = await page.eval_on_selector_all(".result__url", "elements => elements.map(e => e.innerText.trim())")
                direct_links = []
                for l in result_links:
                    url = l if l.startswith("http") else f"https://{l}"
                    if not any(ban in url for ban in ["youtube.com", "namu.wiki", "wikipedia"]):
                        direct_links.append(url)

                print(f"[*] 키워드 [{q}] 사냥 결과: {len(direct_links)}건 발견")

                for target_url in direct_links[:5]:
                    try:
                        await page.goto(target_url, timeout=12000, wait_until="domcontentloaded")
                        body_text = await page.inner_text("body")
                        if len(body_text.strip()) < 150 or not any(k in body_text for k in ["오디션", "배우", "모집", "접수", "캐스팅"]):
                            continue

                        emails = re.findall(EMAIL_REGEX, body_text)
                        phones = re.findall(PHONE_REGEX, body_text)
                        parsed = await parse_with_gemini(body_text)
                        if not parsed or not parsed.get("title"):
                            continue

                        payload = {
                            "source_url": target_url,
                            "title": parsed.get("title"),
                            "category": parsed.get("category") or "기타",
                            "production": parsed.get("production") or "제작팀",
                            "gender": parsed.get("gender") or "무관",
                            "age": parsed.get("age") or "전 연령",
                            "role": parsed.get("role") or "배역 모집",
                            "deadline": parsed.get("deadline") or "2099-12-31",
                            "email": emails[0] if emails else "본문 참조",
                            "phone": phones[0] if phones else "비공개/이메일 접수",
                            "subject_format": parsed.get("subject_format") or "[지원] 배역_이름_연락처",
                            "requirements": parsed.get("requirements") or "",
                            "source": "전국 웹 레이더"
                        }
                        await save_to_supabase(payload)
                    except Exception:
                        continue
            except Exception as e:
                print(f"[-] 광역 레이더 탐색 중 예외: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
