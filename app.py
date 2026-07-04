from flask import Flask, render_template, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
import os
import re
import ssl
import base64
import shutil
import urllib.request
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote
from datetime import datetime

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "rank_history.json")
SHARED_DRIVER = None
REFRESH_JOBS = {}
REFRESH_LOCK = threading.Lock()

# ==========================================
# 1. 순위/광고/리뷰 고속 조회 (순수 HTTP, 브라우저 불필요)
# ==========================================
# search.naver.com 통합검색 페이지는 map.naver.com의 allSearch API와 달리 봇 차단(ncaptcha) 토큰이
# 걸려있지 않아 requests만으로 SSR HTML에 내장된 GraphQL 캐시(__APOLLO_STATE__)를 그대로 읽을 수 있다.
# 이 캐시에 오가닉 순위(placeList), 광고 목록(adBusinesses), 리뷰 수가 전부 들어있다.
SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

def extract_apollo_state(html, marker):
    idx = html.find(marker)
    if idx < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[idx + len(marker):])
        return obj
    except Exception:
        return None

def get_search_widget_data(keyword, timeout=5):
    """통합검색 플레이스 위젯에서 오가닉 순위(상위 7~9위)와 노출 광고 개수를 가져온다."""
    url = f"https://search.naver.com/search.naver?query={quote(keyword)}"
    try:
        resp = requests.get(url, headers=SEARCH_HEADERS, timeout=timeout)
        resp.encoding = "utf-8"
        obj = extract_apollo_state(resp.text, "naver.search.ext.nmb.salt.__APOLLO_STATE__ = ")
        if not obj:
            return {"organic": [], "ad_count": 0}

        root = obj.get("ROOT_QUERY", {})
        place_key = next((k for k in root if k.startswith("placeList(")), None)
        ad_key = next((k for k in root if k.startswith("adBusinesses(")), None)

        organic = []
        if place_key:
            for ref in root[place_key].get("businesses", {}).get("items", []):
                data = obj.get(ref.get("__ref"))
                if data:
                    organic.append(data)

        ad_count = len(root[ad_key].get("items", [])) if ad_key else 0
        return {"organic": organic, "ad_count": ad_count}
    except Exception as e:
        print(f"검색 위젯 조회 실패: {e}", flush=True)
        return {"organic": [], "ad_count": 0}

def get_place_review_counts_http(place_id, timeout=5):
    """m.place.naver.com 상세 페이지에서 리뷰 수를 순위와 무관하게 즉시 가져온다."""
    url = f"https://m.place.naver.com/place/{place_id}/home"
    try:
        resp = requests.get(url, headers=SEARCH_HEADERS, timeout=timeout)
        resp.encoding = "utf-8"
        visitor_match = re.search(r'"visitorReviewsTotal":(\d+)', resp.text)
        blog_match = re.search(r'"cafeBlogReviewsTotal":(\d+)', resp.text)
        return {
            "visitor_reviews": int(visitor_match.group(1)) if visitor_match else None,
            "blog_reviews": int(blog_match.group(1)) if blog_match else None,
        }
    except Exception as e:
        print(f"리뷰 수 조회 실패: {e}", flush=True)
        return {"visitor_reviews": None, "blog_reviews": None}

# ==========================================
# 2. 기존 데이터 관리 및 유틸리티 함수
# ==========================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            try: return json.load(f)
            except: return {}
    return {}

def save_to_history(user_id, keyword, target_place_url, total_rank, pure_rank, visitor_reviews=None, blog_reviews=None, place_name=None, place_alias=None, place_id=None, preserve_missing_reviews=True):
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    
    if user_id not in history: history[user_id] = {}
    if "$meta" not in history[user_id]: history[user_id]["$meta"] = {}
    if target_place_url not in history[user_id]["$meta"]:
        history[user_id]["$meta"][target_place_url] = {}
    if place_name:
        history[user_id]["$meta"][target_place_url]["place_name"] = place_name
    if place_alias:
        history[user_id]["$meta"][target_place_url]["place_alias"] = place_alias
    if place_id:
        history[user_id]["$meta"][target_place_url]["place_id"] = str(place_id)
    if target_place_url:
        history[user_id]["$meta"][target_place_url]["target_place_url"] = target_place_url

    key = f"{keyword}_{target_place_url}"
    if key not in history[user_id]: history[user_id][key] = {}

    if preserve_missing_reviews and blog_reviews is None:
        blog_reviews = latest_known_review_count(history[user_id], key, "blog_reviews", today)
        if blog_reviews is None:
            blog_reviews = latest_known_place_review_count(history[user_id], target_place_url, "blog_reviews", today)
    if preserve_missing_reviews and visitor_reviews is None:
        visitor_reviews = latest_known_review_count(history[user_id], key, "visitor_reviews", today)
        if visitor_reviews is None:
            visitor_reviews = latest_known_place_review_count(history[user_id], target_place_url, "visitor_reviews", today)
    
    history[user_id][key][today] = {
        "total_rank": total_rank,
        "pure_rank": pure_rank,
        "visitor_reviews": visitor_reviews,
        "blog_reviews": blog_reviews,
        "time": datetime.now().strftime("%H:%M:%S")
    }

    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

def extract_place_id(value):
    if not value:
        return None
    decoded = unquote(value)
    patterns = [
        r"/place/(\d+)",
        r"/entry/place/(\d+)",
        r"placeId=(\d+)",
        r'"placeId"\s*:\s*"?(\d+)"?',
    ]
    for pattern in patterns:
        matched = re.search(pattern, decoded)
        if matched:
            return matched.group(1)
    return None

def resolve_place_info(target_place_url, driver=None):
    place_id = extract_place_id(target_place_url)
    resolved_url = target_place_url

    if not place_id and target_place_url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(
                target_place_url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=5, context=context) as response:
                resolved_url = response.geturl()
                place_id = extract_place_id(resolved_url)
        except Exception as e:
            print(f"플레이스 URL 빠른 해석 실패: {e}", flush=True)

    if not place_id and target_place_url.startswith(("http://", "https://")):
        if driver is None:
            return { "place_id": None, "place_url": resolved_url }
        driver.get(target_place_url)
        time.sleep(2)
        resolved_url = driver.current_url
        place_id = extract_place_id(resolved_url) or extract_place_id(driver.page_source)

    return {
        "place_id": place_id,
        "place_url": canonical_place_home_url(place_id) if place_id else resolved_url
    }

def get_target_place(target_place_url, target_meta=None, driver=None, allow_browser_resolve=True):
    cached_place_id = (target_meta or {}).get("place_id")
    if cached_place_id:
        return {
            "place_id": str(cached_place_id),
            "place_url": canonical_place_home_url(str(cached_place_id))
        }
    return resolve_place_info(target_place_url, driver if allow_browser_resolve else None)

def canonical_place_home_url(place_id):
    return f"https://map.naver.com/p/entry/place/{place_id}?placePath=/home"

def parse_count(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    matched = re.search(r"[0-9,]+", str(value))
    if not matched: return None
    return int(matched.group(0).replace(",", ""))

def first_count(*values):
    for value in values:
        parsed = parse_count(value)
        if parsed is not None:
            return parsed
    return None

def latest_known_review_count(history, key, field, today):
    date_entries = history.get(key, {})
    for date in sorted(date_entries.keys(), reverse=True):
        if date == today: continue
        value = date_entries[date].get(field)
        if value is not None: return value
    return None

def latest_known_place_review_count(history, target_place_url, field, today):
    candidates = []
    suffix = f"_{target_place_url}"
    for key, date_entries in history.items():
        if key == "$meta" or not key.endswith(suffix) or not isinstance(date_entries, dict): continue
        for date, info in date_entries.items():
            if date == today or not isinstance(info, dict): continue
            value = info.get(field)
            if value is not None: candidates.append((date, value))
    if not candidates: return None
    return sorted(candidates, reverse=True)[0][1]

# ==========================================
# 3. 브라우저/Selenium 관리
# ==========================================
def create_driver(enable_performance_logs=False):
    options = Options()
    options.set_capability("pageLoadStrategy", "eager")
    if os.environ.get("NAVER_HEADLESS", "0") == "1":
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if enable_performance_logs:
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    chrome_bin = ( os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chromium-browser") )
    if chrome_bin:
        options.binary_location = chrome_bin
        
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(driver_path) if driver_path and os.path.exists(driver_path) else Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(35)
    driver.set_script_timeout(20)
    return driver

def get_shared_driver():
    global SHARED_DRIVER
    try:
        if SHARED_DRIVER:
            _ = SHARED_DRIVER.current_url
            return SHARED_DRIVER
    except Exception:
        try: SHARED_DRIVER.quit()
        except: pass
        SHARED_DRIVER = None
        
    SHARED_DRIVER = create_driver(enable_performance_logs=True)
    return SHARED_DRIVER

def get_fast_search_results(driver, keyword, limit=20):
    # (기존 빠름 검색 로직 유지)
    def collect_from_logs():
        collected = []
        for log in driver.get_log("performance"):
            try:
                message = json.loads(log["message"])["message"]
                if message.get("method") != "Network.responseReceived": continue
                params = message.get("params", {})
                response_url = params.get("response", {}).get("url", "")
                if "/p/api/search/allSearch" not in response_url: continue
                body_data = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": params["requestId"]})
                body = body_data.get("body", "")
                if body_data.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", errors="ignore")
                payload = json.loads(body)
                places = payload.get("result", {}).get("place", {}).get("list", [])
                collected.extend(places)
            except Exception: pass
        return collected

    def add_places(source, target, seen_ids):
        for place in source:
            place_id = str(place.get("id") or "")
            if not place_id or place_id in seen_ids: continue
            seen_ids.add(place_id)
            target.append(place)

    try: driver.get_log("performance")
    except: pass

    try: driver.get(f"https://map.naver.com/p/search/{quote(keyword)}")
    except Exception as e: return []
    time.sleep(4)

    results = []
    seen_ids = set()
    add_places(collect_from_logs(), results, seen_ids)

    for _ in range(6):
        if len(results) >= limit: break
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, 4).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "searchIframe")))
            scroll_box = driver.find_element(By.CSS_SELECTOR, "#_pcmap_list_scroll_container")
            driver.execute_script("arguments[0].scrollBy(0, 2600);", scroll_box)
            time.sleep(1.1)
            add_places(collect_from_logs(), results, seen_ids)
        except Exception: break
    return results[:limit]

# ==========================================
# 4. [핵심] 랭킹 탐색 (API 하이브리드 적용)
# ==========================================
def get_ranking(keyword, target_place_url, target_meta=None, fast_only=True, include_review_detail=True, fast_limit=20, scan_start=None, scan_end=None, driver=None):
    # 1. 대상 플레이스 ID 먼저 확보
    target_place_id = (target_meta or {}).get("place_id") or extract_place_id(target_place_url)

    # 2. [초고속] 통합검색 위젯에서 순수 HTTP로 순위/광고 확인 (브라우저 불필요)
    widget_data = get_search_widget_data(keyword)
    ad_count = widget_data["ad_count"]

    if target_place_id:
        for idx, item in enumerate(widget_data["organic"], start=1):
            if str(item.get("id")) == str(target_place_id):
                review_counts = get_place_review_counts_http(target_place_id) if include_review_detail else {}
                return {
                    "status": "success",
                    "total_rank": idx + ad_count,
                    "pure_rank": idx,
                    "name": item.get("name"),
                    "place_url": target_place_url,
                    "place_id": target_place_id,
                    "visitor_reviews": review_counts.get("visitor_reviews"),
                    "blog_reviews": review_counts.get("blog_reviews"),
                    "address": item.get("roadAddress") or item.get("address"),
                    "category": item.get("category")
                }

    # 3. 위젯에 없는 경우(통합검색 미리보기 노출권 밖) -> 브라우저(Selenium)로 정밀 스캔
    external_driver = driver is not None
    use_shared_driver = os.environ.get("NAVER_REUSE_DRIVER", "1") == "1"
    try:
        if driver is None:
            driver = get_shared_driver() if use_shared_driver else create_driver(enable_performance_logs=True)
        target_place = get_target_place(target_place_url, target_meta, driver, allow_browser_resolve=not fast_only)

        if not target_place.get("place_id"):
            return {"status": "error", "message": "플레이스 URL에서 장소 ID를 찾지 못했습니다."}

        fast_results = get_fast_search_results(driver, keyword, fast_limit)
        if fast_results:
            for idx, result in enumerate(fast_results, start=1):
                if str(result.get("id")) == str(target_place["place_id"]):
                    response = {
                        "status": "success",
                        "total_rank": idx + ad_count,
                        "pure_rank": idx,
                        "name": result.get("name"),
                        "place_url": target_place["place_url"],
                        "place_id": target_place["place_id"],
                        "address": result.get("roadAddress") or result.get("address"),
                        "category": " > ".join(result.get("category", [])) if result.get("category") else None
                    }
                    if include_review_detail:
                        review_counts = get_place_review_counts_http(target_place["place_id"])
                        response["visitor_reviews"] = review_counts.get("visitor_reviews")
                        response["blog_reviews"] = review_counts.get("blog_reviews")
                    return response

        return {"status": "success", "total_rank": 0, "pure_rank": 0, "message": "20위 권 밖이거나 검색 결과에 없습니다."}
    except Exception as e:
        return {"status": "error", "message": f"검색 중 오류 발생: {e}"}
    finally:
        if not external_driver and not use_shared_driver and driver:
            try: driver.quit()
            except: pass

# ==========================================
# 5. 백그라운드 갱신 작업 스레드
# ==========================================
REFRESH_CONCURRENCY = int(os.environ.get("NAVER_REFRESH_CONCURRENCY", "4"))
HISTORY_LOCK = threading.Lock()

def process_keyword(user_id, kw, url, meta, driver):
    try:
        res = get_ranking(kw, url, target_meta=meta, driver=driver)
        with HISTORY_LOCK:
            save_to_history(
                user_id, kw, url,
                res.get('total_rank'),
                res.get('pure_rank'),
                res.get('visitor_reviews'),
                res.get('blog_reviews'),
                res.get('name'),
                meta.get("place_alias"),
                res.get('place_id'),
                preserve_missing_reviews=True # 리뷰가 없으면 과거 기록 유지
            )
        with REFRESH_LOCK:
            if res.get('status') == 'success':
                REFRESH_JOBS[user_id]["success_count"] += 1
    except Exception as e:
        print(f"전체 최신화 실패({kw}): {e}", flush=True)
    finally:
        with REFRESH_LOCK:
            REFRESH_JOBS[user_id]["done"] += 1
            REFRESH_JOBS[user_id]["current_keyword"] = kw

def process_keyword_chunk(user_id, chunk, history):
    driver = create_driver(enable_performance_logs=True)
    try:
        for kw, url in chunk:
            meta = history.get(user_id, {}).get("$meta", {}).get(url, {})
            process_keyword(user_id, kw, url, meta, driver)
    finally:
        try: driver.quit()
        except: pass

def run_refresh_job(user_id, keywords_to_update):
    try:
        history = load_history()
        worker_count = max(1, min(REFRESH_CONCURRENCY, len(keywords_to_update)))
        chunks = [keywords_to_update[i::worker_count] for i in range(worker_count)]
        chunks = [c for c in chunks if c]

        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            futures = [executor.submit(process_keyword_chunk, user_id, chunk, history) for chunk in chunks]
            for future in as_completed(futures):
                future.result()

        with REFRESH_LOCK:
            job = REFRESH_JOBS[user_id]
            job["status"] = "success"
            job["current_keyword"] = ""
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["message"] = f"총 {job['total']}개 중 {job['success_count']}개 갱신 완료!"
    except Exception as e:
        with REFRESH_LOCK:
            if user_id in REFRESH_JOBS:
                REFRESH_JOBS[user_id]["status"] = "error"
                REFRESH_JOBS[user_id]["message"] = f"업데이트 중 오류가 발생했습니다: {e}"
                REFRESH_JOBS[user_id]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ==========================================
# 6. Flask 라우트 (API 엔드포인트)
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/history', methods=['GET'])
def api_history():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({})
    history = load_history()
    return jsonify(history.get(user_id, {}))

@app.route('/api/refresh_place', methods=['POST'])
def refresh_place():
    data = request.json
    user_id = data.get('user_id')
    target_place_url = data.get('target_place_url')
    
    if not user_id:
        return jsonify({"status": "error", "message": "사용자 ID가 필요합니다."})
        
    with REFRESH_LOCK:
        if user_id in REFRESH_JOBS and REFRESH_JOBS[user_id]["status"] == "running":
            return jsonify({"status": "running", "message": "이미 업데이트가 진행 중입니다."})
        
        history = load_history()
        user_data = history.get(user_id, {})
        keywords_to_update = []
        
        if target_place_url:
            for key in user_data:
                if key == "$meta": continue
                if key.endswith(f"_{target_place_url}"):
                    kw = key.split(f"_{target_place_url}")[0]
                    keywords_to_update.append((kw, target_place_url))
        else:
            for key in user_data:
                if key == "$meta": continue
                parts = key.rsplit("_", 1)
                if len(parts) == 2:
                    keywords_to_update.append((parts[0], parts[1]))
        
        REFRESH_JOBS[user_id] = {
            "status": "running",
            "total": len(keywords_to_update),
            "done": 0,
            "success_count": 0,
            "current_keyword": "",
            "message": ""
        }
        
    thread = threading.Thread(target=run_refresh_job, args=(user_id, keywords_to_update))
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "running", "total": len(keywords_to_update)})

@app.route('/api/refresh_status', methods=['GET'])
def refresh_status():
    user_id = request.args.get('user_id')
    with REFRESH_LOCK:
        job = REFRESH_JOBS.get(user_id, {})
        return jsonify(job)

if __name__ == '__main__':
    # 외부 접속을 차단하고 맥북(로컬) 내부에서만 구동하도록 127.0.0.1(localhost)로 고정합니다.
    app.run(debug=True, host='127.0.0.1', port=8080)