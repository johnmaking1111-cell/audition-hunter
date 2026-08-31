import os
import re
import json
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

print(f"[*] Supabase 연결 대상: {SUPABASE_URL}")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
}

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'01[016789]-?\d{3,4}-?\d{4}'

def parse_with_gemini(raw_text: str) -> dict:
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
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[-] AI 분석 실패: {e}")
        return {}

def harvest_filmmakers():
    print("\n[*] === 필름메이커스 사냥 시작 ===")
    url = "https://www.filmmakers.co.kr/actorsAudition"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.text, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/actorsAudition/" in href and re.search(r'/actorsAudition/\d+', href):
                full_url = href if href.startswith("http") else f"https://www.filmmakers.co.kr{href}"
                if full_url not in links:
                    links.append(full_url)

        print(f"[*] 필름메이커스 공고 링크 {len(links)}개 확보")
        for target_url in links[:8]:
            exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
            if exists.data:
                print(f"[=] 이미 수집된 공고 스킵: {target_url}")
                continue

            sub_res = requests.get(target_url, headers=HEADERS, timeout=10)
            sub_soup = BeautifulSoup(sub_res.text, "lxml")
            body_text = sub_soup.get_text(separator=" ", strip=True)

            emails = re.findall(EMAIL_REGEX, body_text)
            phones = re.findall(PHONE_REGEX, body_text)
            parsed = parse_with_gemini(body_text)
            if not parsed:
                continue

            payload = {
                "source_url": target_url,
                "title": parsed.get("title") or "오디션 공고",
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
            res_ins = supabase.table("auditions").insert(payload).execute()
            print(f"[+] 필름메이커스 DB 저장 완료: {payload['title']} | ID: {res_ins.data}")
    except Exception as e:
        print(f"[-] 필름메이커스 수집 실패: {e}")

def harvest_otr():
    print("\n[*] === OTR 연극/뮤지컬 사냥 시작 ===")
    url = "https://otr.co.kr/audition/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.text, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "audition" in href and any(k in href for k in ["id=", "view", "read"]):
                full_url = href if href.startswith("http") else f"https://otr.co.kr{href}"
                if full_url not in links:
                    links.append(full_url)

        print(f"[*] OTR 공고 링크 {len(links)}개 확보")
        for target_url in links[:6]:
            exists = supabase.table("auditions").select("id").eq("source_url", target_url).execute()
            if exists.data:
                continue

            sub_res = requests.get(target_url, headers=HEADERS, timeout=10)
            sub_soup = BeautifulSoup(sub_res.text, "lxml")
            body_text = sub_soup.get_text(separator=" ", strip=True)

            emails = re.findall(EMAIL_REGEX, body_text)
            phones = re.findall(PHONE_REGEX, body_text)
            parsed = parse_with_gemini(body_text)
            if not parsed:
                continue

            payload = {
                "source_url": target_url,
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
            res_ins = supabase.table("auditions").insert(payload).execute()
            print(f"[+] OTR DB 저장 완료: {payload['title']} | ID: {res_ins.data}")
    except Exception as e:
        print(f"[-] OTR 수집 실패: {e}")

if __name__ == "__main__":
    harvest_filmmakers()
    harvest_otr()
