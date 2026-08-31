import os
import re
import json
import urllib.request
import xml.etree.ElementTree as ET
from google import genai
from supabase import create_client, Client

raw_url = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_URL = raw_url.split("/rest")[0].rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 타깃 주소: {SUPABASE_URL}")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[!] 에러: Supabase 환경변수 누락")
    exit(1)

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"[!] Supabase 클라이언트 생성 실패: {e}")
    exit(1)

ai_client = genai.Client(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

def parse_with_gemini(title: str, text: str) -> dict:
    prompt = f"""
    당신은 대한민국 배우들을 돕는 오디션 전문 캐스팅 디렉터 AI입니다.
    아래 공고문 원문에서 핵심 정보만 추출해 순수 JSON으로만 응답하세요.

    JSON 포맷:
    {{
      "title": "공고 또는 작품 명칭",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼, 에이전시/기획사 중 택1",
      "production": "제작사, 극단 또는 주최측",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령 (예: 20대, 25~35세, 전 연령)",
      "role": "배역명 또는 모집 요강 요약",
      "deadline": "마감일(YYYY-MM-DD 포맷, 없으면 2099-12-31)",
      "requirements": "자격 요건 및 지원 방법 요약",
      "subject_format": "메일 제목 양식 (없으면 '[오디션 지원] 배역_이름_연락처')"
    }}

    [공고 제목]: {title}
    [공고 본문]: {text[:2500]}
    """
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt
        )
        clean = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean)
    except Exception as e:
        print(f"[-] AI 분석 오류: {e}")
        return {}

def fetch_rss_feed(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    items = []
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            tree = ET.fromstring(response.read())
            for item in tree.findall(".//item"):
                t = item.findtext("title") or ""
                l = item.findtext("link") or ""
                d = item.findtext("description") or ""
                if t and l:
                    items.append({"title": t, "link": l, "desc": d})
    except Exception as e:
        print(f"[-] RSS 수신 실패 ({url}): {e}")
    return items

def run_audition_hunter():
    print("\n==========================================")
    print("🔥 [오디션 사냥꾼] 전국 실시간 오디션 레이더 풀가동")
    print("==========================================")

    channels = [
        ("문화예술 오디션/배우 모집 피드", "https://news.google.com/rss/search?q=%EC%98%A4%EB%94%94%EC%85%98+%EB%B0%B0%EC%9A%B0+%EB%AA%A8%EC%A7%91&hl=ko&gl=KR&ceid=KR:ko"),
        ("뮤지컬/연극 캐스팅 공고망", "https://news.google.com/rss/search?q=%EB%AE%A4%EC%A7%80%EC%BB%AC+%EC%97%B0%EA%B7%B9+%EC%BA%90%EC%8A%A4%ED%8C%85+%EA%B3%B5%EA%B3%A0&hl=ko&gl=KR&ceid=KR:ko"),
        ("독립/단편영화 배우 모집망", "https://news.google.com/rss/search?q=%EB%8F%85%EB%A6%BD%EC%98%81%ED%99%94+%EB%8B%A8%ED%8E%B8%EC%98%81%ED%99%94+%EB%B0%B0%EC%9A%B0+%EB%AA%A8%EC%A7%91&hl=ko&gl=KR&ceid=KR:ko")
    ]

    total_inserted = 0

    for source_name, feed_url in channels:
        print(f"\n[*] 레이더 탐색 가동: [{source_name}]")
        posts = fetch_rss_feed(feed_url)
        print(f"[*] 유효 공고 {len(posts)}건 포착")

        for post in posts[:5]:
            source_url = post["link"]
            title = post["title"]
            desc = re.sub(r'<[^>]+>', '', post["desc"])

            try:
                exists = supabase.table("auditions").select("id").eq("source_url", source_url).execute()
                if exists.data:
                    print(f"[=] 기존 저장 공고 스킵: {title[:20]}...")
                    continue
            except Exception as ex:
                print(f"[!] Supabase 통신 에러: {ex}")
                continue

            parsed = parse_with_gemini(title, desc)
            if not parsed:
                continue

            emails = re.findall(EMAIL_REGEX, desc)
            phones = re.findall(PHONE_REGEX, desc)

            payload = {
                "source_url": source_url,
                "title": parsed.get("title") or title,
                "category": parsed.get("category") or "오디션",
                "production": parsed.get("production") or "제작/주최사",
                "gender": parsed.get("gender") or "무관",
                "age": parsed.get("age") or "전 연령",
                "role": parsed.get("role") or "배역 모집",
                "deadline": parsed.get("deadline") or "2099-12-31",
                "email": emails[0] if emails else "상세 링크 참조",
                "phone": phones[0] if phones else "비공개/접수처 참조",
                "subject_format": parsed.get("subject_format") or "[지원] 배역_이름_연락처",
                "requirements": parsed.get("requirements") or desc[:300],
                "source": source_name
            }

            try:
                res = supabase.table("auditions").insert(payload).execute()
                print(f"[🔥 DB 적재 성공] {payload['category']} | {payload['title']}")
                total_inserted += 1
            except Exception as ex:
                print(f"[-] DB Insert 에러: {ex}")

    print("\n==========================================")
    print(f"🎯 사냥 완료: 총 {total_inserted}건의 오디션 공고가 DB에 적재되었습니다.")
    print("==========================================")

if __name__ == "__main__":
    run_audition_hunter()
