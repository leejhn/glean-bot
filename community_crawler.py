#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
community_crawler.py - 커뮤니티(다모앙 + 뽐뿌) 히든 네이버페이 적립 링크 크롤러
1시간 주기로 실행되어 양대 커뮤니티의 최신 유효 리워드 링크를 수집, 중복 제거(Dedup) 후
curated_missions.json 및 naver_pay_extension_html/curated_missions.json에 동기화합니다.
"""

import sys
import os
import json
import re
import subprocess
import time
import base64
import shutil
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT_DIR, "curated_missions.json")
HTML_DATA_FILE = os.path.join(ROOT_DIR, "naver_pay_extension_html", "curated_missions.json")
CURL_BIN = shutil.which("curl.exe") or shutil.which("curl") or "curl"

# 수집 대상 허용 도메인 및 패턴
VALID_PATTERNS = [
    r'https?://campaign2\.naver\.com/npay/v2/click-point/\?[^\s"\'<>]+',
    r'https?://campaign2\.naver\.com/npay/[^\s"\'<>]+',
    r'https?://point\.pay\.naver\.com/mission-detail\?[^\s"\'<>]+',
    r'https?://point\.pay\.naver\.com/bridge/[^\s"\'<>]+',
    r'https?://point\.pay\.naver\.com/main\?category=quickreward[^\s"\'<>]*',
    r'https?://external-token\.pay\.naver\.com/entry/[^\s"\'<>]+',
    r'https?://ofw\.adison\.co/u/naverpay/ads/\d+',
    r'https?://home\.pay\.naver\.com/\?[^\s"\'<>]+'
]

# 제외할 URL 패턴
EXCLUDE_PATTERNS = [
    r'nid\.naver\.com',
    r'member/naver_',
    r'from=main_pay_iconhttps',
    r'ads\.naver\.com/sa',
    r'javascript:',
    r'#none',
    r'promoCode=quiz_s'
]

def fetch_server_dead_links():
    """서버(Cloudflare Worker)에 사용자들이 자동 등록한 만료/무효 링크 목록 조회"""
    url = "https://glean-bot.leejhn.workers.dev/api/dead-links"
    try:
        res = subprocess.run(
            [CURL_BIN, "-s", "--max-time", "5", "-H", "User-Agent: glean-bot-crawler/1.0", "-H", "X-Glean-Auth: internal-crawler", url],
            capture_output=True, text=True, encoding="utf-8"
        )
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            if isinstance(data, list):
                return set(data)
            elif isinstance(data, dict) and "dead_urls" in data:
                return set(data["dead_urls"])
    except Exception:
        pass
    return set()

def load_database():
    """기존 수집 데이터 로드 및 24시간 만료/무효 데이터 정리"""
    if not os.path.exists(DATA_FILE):
        return {"updated_at": "", "total_active": 0, "deleted_urls": [], "missions": []}
    
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ 기존 DB 로드 실패 (초기화 진행): {e}")
        return {"updated_at": "", "total_active": 0, "deleted_urls": [], "missions": []}

    deleted_urls = set(data.get("deleted_urls", []))
    server_dead = fetch_server_dead_links()
    if server_dead:
        print(f"🌐 서버에서 사용자 신고 무효 링크 {len(server_dead)}건 동기화 완료")
        deleted_urls.update(server_dead)

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    today_yymmdd = now.strftime("%y%m%d")

    active_missions = []
    for m in data.get("missions", []):
        url = m.get("url", "")
        if not url or url in deleted_urls:
            continue
        # 무효/실패 알려진 퀴즈 링크 제외
        if "promoCode=quiz_s" in url:
            deleted_urls.add(url)
            continue

        # 과거 날짜 이벤트 제외 (예: 2026-11-10 등 오늘 이전)
        src_title = m.get("source_title", "")
        title_date_match = re.search(r'(\d{4}-\d{2}-\d{2})', src_title)
        if title_date_match and title_date_match.group(1) < today_str:
            deleted_urls.add(url)
            continue

        # eventId에 포함된 과거 날짜 감지 (예: 261110, 261111 등 오늘 이전 날짜)
        date_in_url = re.search(r'2611(\d{2})', url) or re.search(r'202611(\d{2})', url)
        if date_in_url:
            day = int(date_in_url.group(1))
            if day < now.day:
                deleted_urls.add(url)
                continue

        discovered_time_str = m.get("discovered_at")
        if not discovered_time_str:
            continue
        try:
            d_time = datetime.fromisoformat(discovered_time_str)
            if now - d_time < timedelta(hours=24):
                for key in ["category", "source_community", "source_title", "source_url"]:
                    m.pop(key, None)
                active_missions.append(m)
        except Exception:
            pass

    data["deleted_urls"] = sorted(list(deleted_urls))
    data["missions"] = active_missions
    data["total_active"] = len(active_missions)
    return data

def save_database(data):
    """DB 파일 저장 및 GitHub Pages 폴더 동기화"""
    data["updated_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    data["total_active"] = len(data.get("missions", []))

    # 1. 루트 JSON 저장
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 2. GitHub Pages 배포 디렉토리 동기화
    html_dir = os.path.dirname(HTML_DATA_FILE)
    if os.path.exists(html_dir):
        with open(HTML_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_damoang_posts():
    """다모앙 RSS 피드에서 최신 게시글 목록 조회"""
    url = "https://damoang.net/rss/economy"
    headers = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    cmd = [CURL_BIN, "-s", "-H", headers, url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=15)
        if res.returncode != 0 or not res.stdout:
            return []
        root = ET.fromstring(res.stdout)
        items = []
        for item in root.findall("./channel/item"):
            title = item.find("title").text if item.find("title") is not None else ""
            link = item.find("link").text if item.find("link") is not None else ""
            items.append({"source": "다모앙", "title": title.strip(), "link": link.strip()})
        return items
    except Exception as e:
        print(f"❌ 다모앙 RSS 파싱 오류: {e}")
        return []

def fetch_ppomppu_posts():
    """뽐뿌 모바일 쿠폰 게시판에서 최신 게시글 목록 조회"""
    url = "https://m.ppomppu.co.kr/new/bbs_list.php?id=coupon"
    cmd = [
        CURL_BIN, "-s",
        "-H", "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "-H", "Referer: https://m.ppomppu.co.kr/",
        "--tlsv1.2",
        url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if res.returncode != 0 or not res.stdout:
            return []
        html = res.stdout.decode("euc-kr", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for a in soup.find_all("a", href=True):
            if "bbs_view.php?id=coupon" in a["href"]:
                title_span = a.find("span", class_="title")
                title_text = title_span.get_text(strip=True) if title_span else a.get_text(strip=True)
                href = a["href"].lstrip("/")
                if not href.startswith("new/"):
                    href = "new/" + href
                full_link = "https://m.ppomppu.co.kr/" + href
                items.append({"source": "뽐뿌", "title": title_text, "link": full_link})
        return items
    except Exception as e:
        print(f"❌ 뽐뿌 목록 조회 오류: {e}")
        return []

def fetch_damoang_post_html(post_url):
    """다모앙 게시글 본문 가져오기"""
    headers = [
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Referer: https://damoang.net/economy"
    ]
    cmd = [CURL_BIN, "-s"] + headers + [post_url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=15)
        if res.returncode == 0:
            return res.stdout
    except Exception:
        pass
    return ""

def fetch_ppomppu_post_html(post_url):
    """뽐뿌 모바일 게시글 본문 가져오기 (EUC-KR 디코딩)"""
    headers = [
        "-H", "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "-H", "Referer: https://m.ppomppu.co.kr/new/bbs_list.php?id=coupon",
        "--tlsv1.2"
    ]
    cmd = [CURL_BIN, "-s"] + headers + [post_url]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if res.returncode == 0:
            return res.stdout.decode("euc-kr", errors="ignore")
    except Exception:
        pass
    return ""

def unwrap_ppomppu_url(url):
    """뽐뿌의 s.ppomppu.co.kr 리다이렉트 링크에서 base64 대상 URL 추출"""
    if "s.ppomppu.co.kr" in url and "target=" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        target_b64 = params.get("target", [""])[0]
        if target_b64:
            try:
                # 패딩 보정
                missing_padding = len(target_b64) % 4
                if missing_padding:
                    target_b64 += '=' * (4 - missing_padding)
                decoded = base64.b64decode(target_b64).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    return url

def extract_reward_links(html, source, post_title, post_url, deleted_urls=None):
    """HTML 본문에서 유효한 네이버페이 리워드 링크 추출"""
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("div", class_="space-y-6") or soup.find("div", id="bo_v_con") or soup

    raw_links = []
    for a in article.find_all("a", href=True):
        href = a["href"].strip()
        # 뽐뿌 래퍼 링크 풀기
        resolved = unwrap_ppomppu_url(href)
        raw_links.append(resolved)

    text = article.get_text()
    for m in re.finditer(r'https?://[^\s<>"\'\)]+', text):
        raw_links.append(m.group(0).strip())

    valid_missions = []
    seen_urls = set()
    now = datetime.now()

    for url in raw_links:
        # 이스케이프 해제 및 URL 정규화 (# 제거)
        url = unquote(url).replace("&amp;", "&").split("#")[0].strip()
        
        # 영구 삭제된 목록, 말줄임표(...) 링크 및 제외 패턴 검사
        if (deleted_urls and url in deleted_urls) or "..." in url or any(re.search(pat, url) for pat in EXCLUDE_PATTERNS):
            continue

        # 과거 eventId 검사 (오늘 이전 날짜 이벤트는 스킵)
        date_in_url = re.search(r'2611(\d{2})', url) or re.search(r'202611(\d{2})', url)
        if date_in_url and int(date_in_url.group(1)) < now.day:
            continue

        # 유효 패턴 검사
        is_matched = False
        for pat in VALID_PATTERNS:
            if re.search(pat, url):
                is_matched = True
                break
        
        if not is_matched or url in seen_urls:
            continue

        seen_urls.add(url)

        valid_missions.append({
            "url": url,
            "discovered_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        })

    return valid_missions

def run_crawler():
    """다모앙 + 뽐뿌 통합 크롤러 메인 파이프라인"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 커뮤니티(다모앙 + 뽐뿌) 히든 링크 통합 크롤링 시작...")
    db = load_database()
    deleted_urls = set(db.get("deleted_urls", []))
    existing_urls = {m["url"] for m in db.get("missions", [])}

    damoang_items = fetch_damoang_posts()
    ppomppu_items = fetch_ppomppu_posts()
    all_items = damoang_items + ppomppu_items
    print(f"📄 수집 대상 게시글 후보: 총 {len(all_items)}건 (다모앙 {len(damoang_items)}, 뽐뿌 {len(ppomppu_items)})")

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 대상 키워드 필터링
    target_posts = []
    keywords = ["네이버페이", "네페", "npay", "포인트", "적립", "클릭"]
    for item in all_items:
        title = item["title"]
        # 지난 날짜 게시글은 자동 제외 (예: 2026-11-10 등 오늘 이전 날짜)
        m_date = re.search(r'(\d{4}-\d{2}-\d{2})', title)
        if m_date and m_date.group(1) < today_str:
            continue

        if ("네이버페이" in title or "네페" in title) and any(k in title for k in ["원", "포인트", "적립", "클릭", "랜덤", "줍", "뽑기"]):
            # 비클릭형 제외
            if any(k in title for k in ["카드", "보험", "정답"]):
                continue
            target_posts.append(item)

    print(f"🎯 매칭된 핵심 게시글: {len(target_posts)}건")

    new_found_count = 0
    for post in target_posts:
        source = post["source"]
        print(f"  🔍 분석 중 [{source}]: {post['title']}")
        if source == "다모앙":
            html = fetch_damoang_post_html(post["link"])
        else:
            html = fetch_ppomppu_post_html(post["link"])

        if not html:
            continue

        missions = extract_reward_links(html, source, post["title"], post["link"], deleted_urls)
        for m in missions:
            if m["url"] not in existing_urls and m["url"] not in deleted_urls:
                existing_urls.add(m["url"])
                db["missions"].append(m)
                new_found_count += 1
                print(f"    ✨ [{source} 발굴] {m['url'][:75]}...")

        time.sleep(1)

    save_database(db)
    print(f"✅ 크롤링 & 최종 Dedup 완료: 신규 {new_found_count}건 / 현재 활성 링크 총 {db['total_active']}건")
    sync_to_github_pages()
    return db

def sync_to_github_pages():
    """배포용 공개 저장소(leejhn/glean-bot)로 자동 동기화 푸시"""
    try:
        import tempfile
        temp_dir = tempfile.mkdtemp()
        res_clone = subprocess.run(["git", "clone", "--depth", "1", "https://github.com/leejhn/glean-bot.git", temp_dir], capture_output=True, timeout=20)
        if res_clone.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
        dest_file = os.path.join(temp_dir, "curated_missions.json")
        shutil.copy2(DATA_FILE, dest_file)
        subprocess.run(["git", "-C", temp_dir, "add", "curated_missions.json"], capture_output=True, timeout=10)
        res_commit = subprocess.run(["git", "-C", temp_dir, "commit", "-m", "chore: hourly curated missions update"], capture_output=True, timeout=10)
        if res_commit.returncode == 0:
            subprocess.run(["git", "-C", temp_dir, "push", "origin", "main"], capture_output=True, timeout=20)
            print("  🚀 배포용 저장소(leejhn/glean-bot) 자동 푸시 동기화 완료")
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        pass

def print_summary():
    """현재 수집된 리스트 콘솔 출력"""
    db = load_database()
    missions = db.get("missions", [])
    print("\n" + "=" * 75)
    print(f"📋 [glean-bot] 다모앙+뽐뿌 히든 미션 리스트 (총 {len(missions)}개)")
    print(f"🕒 최종 갱신 시각: {db.get('updated_at', '없음')}")
    print("=" * 75)

    if not missions:
        print("현재 활성화된 히든 미션 링크가 없습니다.")
        print("=" * 75)
        return

    for idx, m in enumerate(missions, 1):
        print(f"[{idx:02d}] {m.get('discovered_at', '')} | {m.get('url')}")
        print("-" * 75)

if __name__ == "__main__":
    if "--summary" in sys.argv or "-s" in sys.argv:
        print_summary()
    else:
        run_crawler()
        print_summary()
