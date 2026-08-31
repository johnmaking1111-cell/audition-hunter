import os
import re
import json
import urllib.request
import xml.etree.ElementTree as ET
import google.generativeai as genai
from supabase import create_client, Client

# /rest/v1 또는 슬래시가 붙어있으면 자동으로 순수 도메인만 추출
RAW_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_URL = RAW_URL.split("/rest")[0].rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 접속 주소 (정제 완료): {SUPABASE_URL}")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"[!] Supabase 클라이언트 생성 실패: {e}")
    exit(1)

genai.configure(api_key=GEMINI_KEY)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

def parse_with_gemini(title: str, body: str) -> dict:
    prompt = f"""
    당신은 대한민국 모든 배우들의 기회를 찾아주는 오디션 전문 캐스팅 디렉터 AI입니다.
    아래 수집된 공고문에서 핵심 정보를 추출해 JSON으로만 응답하세요.

    JSON 포맷:
    {{
      "title": "공고 또는 작품 명칭",
      "category": "장편/단편영화, OTT/드라마, 연극, 뮤지컬, 광고/숏폼, 에이전시/기획사 중 택1",
      "production": "제작사, 극단 또는 주최측",
      "gender": "남, 여, 무관 중 택1",
      "age": "모집 연령 (예: 20대, 25~35세, 전 연령)",
      "role": "배역명 또는 모집 요강 요약",
      "deadline": "마감일(YYYY-MM-DD 포맷, 기한 미기재 시 2099-12-31)",
      "requirements": "자격 요건 및 지원 방법 요약",
      "subject_format": "메일 제목 양식 (없으면 '[지원] 배역_이름_연락처')"
    }}

    [공고 제목]: {title}
    [공고 내용]: {body[:2500]}
    """
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        res = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(res.text)
    except Exception as e:
        print(f"[-] AI 분석 오류: {e}")
        return {}

def fetch_rss(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    items = []
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            tree = ET.fromstring(res.read())
            for item in tree.findall(".//item"):
                t = item.findtext("title") or ""
                l = item.findtext("link") or ""
                d = item.findtext("description") or ""
                if t and l:
                    items.append({"title": t, "link": l, "desc": d})
    except Exception as e:
        print(f"[-] RSS 수신 실패 ({url}): {e}")
    return items

def run():
    print("\n==========================================")
    print("🔥 [오디션 사냥꾼] 전국 실시간 오디션 레이더 가동")
    print("==========================================")

    feeds = [
        ("문화예술 오디션/공모 피드", "https://news.google.com/rss/search?q=%EC%98%A4%EB%94%94%EC%85%98+%EB%B0%B0%EC%9A%B0+%EB%AA%A8%EC%A7%91&hl=ko&gl=KR&ceid=KR:ko"),
        ("연극/뮤지컬 캐스팅 피드", "https://news.google.com/rss/search?q=%EB%AE%A4%EC%A7%80%EC%BB%AC+%EC%97%B0%EA%B7%B9+%EC%BA%90%EC%8A%A4%ED%8C%85+%EA%B3%B5%EA%B3%A0&hl=ko&gl=KR&ceid=KR:ko"),
        ("독립영화/단편영화 배우 모집", "https://news.google.com/rss/search?q=%EB%8F%85%EB%A6%BD%EC%98%81%ED%99%94+%EB%8B%A8%ED%8E%B8%EC%98%81%ED%99%94+%EB%B0%B0%EC%9A%B0+%EB%AA%A8%EC%A7%91&hl=ko&gl=KR&ceid=KR:ko")
    ]

    total_inserted = 0

    for source_name, feed_url in feeds:
        print(f"\n[*] 레이더 탐색: [{source_name}]")
        posts = fetch_rss(feed_url)
        print(f"[*] 유효 공고 {len(posts)}건 감지")

        for post in posts[:4]:
            source_url = post["link"]
            title = post["title"]
            desc = re.sub(r'<[^>]+>', '', post["desc"])

            try:
                exists = supabase.table("auditions").select("id").eq("source_url", source_url).execute()
                if exists.data:
                    print(f"[=] 중복 스킵: {title[:20]}...")
                    continue
            except Exception as e:
                print(f"[!] DB 조회 에러: {e}")
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
                "production": parsed.get("production") or "제작/기획사",
                "gender": parsed.get("gender") or "무관",
                "age": parsed.get("age") or "전 연령",
                "role": parsed.get("role") or "배역 모집",
                "deadline": parsed.get("deadline") or "2099-12-31",
                "email": emails[0] if emails else "본문 링크 참조",
                "phone": phones[0] if phones else "비공개/접수처 참조",
                "subject_format": parsed.get("subject_format") or "[지원] 배역_이름_연락처",
                "requirements": parsed.get("requirements") or desc[:300],
                "source": source_name
            }

            try:
                res = supabase.table("auditions").insert(payload).execute()
                print(f"[🔥 DB 적재 성공] {payload['category']} | {payload['title']}")
                total_inserted += 1
            except Exception as e:
                print(f"[-] DB Insert 에러: {e}")

    print(f"\n==========================================")
    print(f"🎯 사냥 완료: 총 {total_inserted}개의 새로운 공고가 DB에 적재되었습니다.")
    print("==========================================")

if __name__ == "__main__":
    run()
