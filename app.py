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
from html import unescape as html_unescape
from urllib.parse import quote, unquote
from datetime import datetime

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "rank_history.json")
SHARED_DRIVER = None
REFRESH_JOBS = {}
REFRESH_LOCK = threading.Lock()

# [데이터 저장 및 불러오기]
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
            request = urllib.request.Request(
                target_place_url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=5, context=context) as response:
                resolved_url = response.geturl()
                place_id = extract_place_id(resolved_url)
        except Exception as e:
            print(f"플레이스 URL 빠른 해석 실패: {e}", flush=True)

    if not place_id and target_place_url.startswith(("http://", "https://")):
        if driver is None:
            return {
                "place_id": None,
                "place_url": resolved_url
            }
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

def item_matches_place(item, target_place):
    place_id = target_place.get("place_id")
    if not place_id:
        return False

    html = item.get_attribute("innerHTML") or ""
    if place_id in unquote(html):
        return True

    for link in item.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = link.get_attribute("href") or ""
        if extract_place_id(href) == place_id:
            return True
    return False

def click_item_and_get_place_id(driver, item):
    try:
        before_url = driver.current_url
        button = item.find_element(By.CSS_SELECTOR, "a.place_thumb, a.U70Fj, a[role='button']")
        driver.execute_script("arguments[0].click();", button)
        WebDriverWait(driver, 4).until(
            lambda d: d.current_url != before_url and extract_place_id(d.current_url) is not None
        )
        return extract_place_id(driver.current_url)
    except Exception:
        return None

def parse_count(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    matched = re.search(r"[0-9,]+", str(value))
    if not matched:
        return None
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
        if date == today:
            continue
        value = date_entries[date].get(field)
        if value is not None:
            return value
    return None

def latest_known_place_review_count(history, target_place_url, field, today):
    candidates = []
    suffix = f"_{target_place_url}"
    for key, date_entries in history.items():
        if key == "$meta" or not key.endswith(suffix) or not isinstance(date_entries, dict):
            continue
        for date, info in date_entries.items():
            if date == today or not isinstance(info, dict):
                continue
            value = info.get(field)
            if value is not None:
                candidates.append((date, value))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]

def extract_review_counts_from_text(text):
    text = html_unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text)
    visitor_reviews = None
    blog_reviews = None

    visitor_match = re.search(r"방문자\s*리뷰\s*([0-9,]+)", text)
    blog_match = re.search(r"블로그\s*리뷰\s*([0-9,]+)", text)

    if visitor_match:
        visitor_reviews = parse_count(visitor_match.group(1))
    if blog_match:
        blog_reviews = parse_count(blog_match.group(1))

    return {
        "visitor_reviews": visitor_reviews,
        "blog_reviews": blog_reviews
    }

def get_review_counts(driver, target_place=None):
    try:
        driver.switch_to.default_content()
        if target_place and target_place.get("place_id"):
            driver.get(canonical_place_home_url(target_place["place_id"]))

        WebDriverWait(driver, 8).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "entryIframe")))
        for _ in range(12):
            try:
                text = driver.find_element(By.TAG_NAME, "body").text
                review_counts = extract_review_counts_from_text(text)
                if review_counts["visitor_reviews"] is not None or review_counts["blog_reviews"] is not None:
                    return review_counts
            except Exception:
                pass
            time.sleep(0.5)

        review_counts = extract_review_counts_from_text(driver.page_source)
        if review_counts["visitor_reviews"] is not None or review_counts["blog_reviews"] is not None:
            return review_counts
    except Exception as e:
        print(f"리뷰 수 추출 실패: {e}", flush=True)
    return {
        "visitor_reviews": None,
        "blog_reviews": None
    }

def get_fast_search_results(driver, keyword, limit=20):
    def collect_from_logs():
        collected = []
        for log in driver.get_log("performance"):
            try:
                message = json.loads(log["message"])["message"]
                if message.get("method") != "Network.responseReceived":
                    continue

                params = message.get("params", {})
                response_url = params.get("response", {}).get("url", "")
                if "/p/api/search/allSearch" not in response_url:
                    continue

                body_data = driver.execute_cdp_cmd(
                    "Network.getResponseBody",
                    {"requestId": params["requestId"]}
                )
                body = body_data.get("body", "")
                if body_data.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", errors="ignore")

                payload = json.loads(body)
                places = payload.get("result", {}).get("place", {}).get("list", [])
                collected.extend(places)
            except Exception as e:
                print(f"빠른 검색 결과 파싱 실패: {e}", flush=True)
        return collected

    def add_places(source, target, seen_ids):
        for place in source:
            place_id = str(place.get("id") or "")
            if not place_id or place_id in seen_ids:
                continue
            seen_ids.add(place_id)
            target.append(place)

    try:
        driver.get_log("performance")
    except Exception:
        pass

    try:
        driver.get(f"https://map.naver.com/p/search/{quote(keyword)}")
    except Exception as e:
        print(f"빠른 검색 페이지 로딩 실패: {keyword} / {e}", flush=True)
        return []

    time.sleep(4)

    results = []
    seen_ids = set()
    add_places(collect_from_logs(), results, seen_ids)

    for _ in range(6):
        if len(results) >= limit:
            break
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, 4).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "searchIframe")))
            scroll_box = driver.find_element(By.CSS_SELECTOR, "#_pcmap_list_scroll_container")
            driver.execute_script("arguments[0].scrollBy(0, 2600);", scroll_box)
            time.sleep(1.1)
            add_places(collect_from_logs(), results, seen_ids)
        except Exception as e:
            print(f"빠른 검색 추가 수집 실패: {e}", flush=True)
            break

    return results[:limit]

def result_to_rank_response(result, target_place, total_rank, pure_rank):
    visitor_reviews = first_count(
        result.get("placeReviewCount"),
        result.get("visitorReviewCount"),
        result.get("visitorReviewCnt")
    )
    blog_reviews = first_count(
        result.get("blogReviewCount"),
        result.get("blogReviewCnt"),
        result.get("blogCafeReviewCount"),
        result.get("blogCafeReviewCnt"),
        result.get("blogCafeReview")
    )

    return {
        "status": "success",
        "total_rank": total_rank,
        "pure_rank": pure_rank,
        "name": result.get("name"),
        "place_url": target_place["place_url"],
        "place_id": target_place["place_id"],
        "visitor_reviews": visitor_reviews,
        "blog_reviews": blog_reviews,
        "address": result.get("roadAddress") or result.get("address"),
        "category": " > ".join(result.get("category", [])) if result.get("category") else None
    }

def merge_review_counts(response, review_counts):
    if review_counts.get("visitor_reviews") is not None:
        response["visitor_reviews"] = review_counts["visitor_reviews"]
    if review_counts.get("blog_reviews") is not None:
        response["blog_reviews"] = review_counts["blog_reviews"]
    return response

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
    chrome_bin = (
        os.environ.get("CHROME_BIN")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
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
        try:
            SHARED_DRIVER.quit()
        except Exception:
            pass
        SHARED_DRIVER = None

    SHARED_DRIVER = create_driver(enable_performance_logs=True)
    return SHARED_DRIVER

# [초고속 촘촘 검색 엔진]
def get_ranking(keyword, target_place_url, target_meta=None, fast_only=True, include_review_detail=True, fast_limit=20, scan_start=None, scan_end=None):
    driver = None
    use_shared_driver = os.environ.get("NAVER_REUSE_DRIVER", "1") == "1"
    try:
        driver = get_shared_driver() if use_shared_driver else create_driver(enable_performance_logs=True)
        target_place = get_target_place(
            target_place_url,
            target_meta,
            driver,
            allow_browser_resolve=not fast_only
        )
        if not target_place["place_id"]:
            print(f"플레이스 ID를 찾지 못했습니다: {target_place_url}", flush=True)
            return {"status": "error", "message": "플레이스 URL에서 장소 ID를 찾지 못했습니다."}

        if scan_start is None:
            fast_results = get_fast_search_results(driver, keyword, fast_limit)
            if fast_results:
                for idx, result in enumerate(fast_results, start=1):
                    if str(result.get("id")) == str(target_place["place_id"]):
                        response = result_to_rank_response(result, target_place, idx, idx)
                        if include_review_detail:
                            review_counts = get_review_counts(driver, target_place)
                            return merge_review_counts(response, review_counts)
                        return response

            if fast_only:
                print(f"20위권 밖 처리. keyword={keyword}, place_id={target_place['place_id']}", flush=True)
                return {
                    "status": "out_of_top",
                    "message": "20위권 밖입니다.",
                    "total_rank": None,
                    "pure_rank": None,
                    "name": target_meta.get("place_name") if target_meta else None,
                    "place_id": target_place["place_id"],
                    "place_url": target_place["place_url"],
                    "visitor_reviews": None,
                    "blog_reviews": None
                }

        scan_start = int(scan_start or 1)
        scan_end = int(scan_end or 99)

        driver.get(f"https://map.naver.com/p/search/{quote(keyword)}")
        
        try:
            WebDriverWait(driver, 8).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "searchIframe")))
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "서비스 이용이 제한되었습니다" in body_text or "과도한 접근 요청" in body_text:
                print("네이버 지도 서비스 이용 제한 감지", flush=True)
                return {
                    "status": "error",
                    "message": "네이버가 자동 검색을 잠시 제한했습니다. 잠시 후 다시 시도하거나 Chrome에서 네이버 지도에 직접 접속한 뒤 다시 검색해 주세요."
                }
        except:
            print(f"검색 iframe을 찾지 못했습니다. keyword={keyword}", flush=True)
            return {"status": "error", "message": "네이버 검색 결과 영역을 찾지 못했습니다."}
        
        checked_items = set()
        total_rank = 0
        pure_rank = 0

        # 네이버 지도는 목록 링크에 ID가 없어서 항목을 클릭한 뒤 주소창의 place ID를 비교합니다.
        for _ in range(10): 
            items = driver.find_elements(By.CSS_SELECTOR, "li, div[role='listitem']")
            for item in items:
                text = item.text
                if not text or "검색결과가 없습니다" in text: continue
                item_key = re.sub(r"\s+", " ", text).strip()
                if item_key in checked_items:
                    continue
                checked_items.add(item_key)

                total_rank += 1
                if "광고" not in text and "AD" not in text: pure_rank += 1

                if total_rank < scan_start:
                    continue
                if total_rank > scan_end:
                    return {
                        "status": "need_more" if scan_end < 99 else "not_found",
                        "message": f"{scan_start}-{scan_end}위 안에서 찾지 못했습니다.",
                        "checked_start": scan_start,
                        "checked_end": scan_end,
                        "next_start": scan_end + 1 if scan_end < 99 else None,
                        "next_end": min(scan_end + 20, 99) if scan_end < 99 else None,
                        "place_id": target_place["place_id"],
                        "place_url": target_place["place_url"]
                    }

                found_place_id = None
                if item_matches_place(item, target_place):
                    # HTML에서 ID를 찾았더라도, 리뷰를 읽기 위해 우측 패널을 여는 클릭 동작을 강제로 실행합니다.
                    click_item_and_get_place_id(driver, item)
                    found_place_id = target_place["place_id"]
                else:
                    found_place_id = click_item_and_get_place_id(driver, item)

                if found_place_id == target_place["place_id"]:
                    review_counts = (
                        get_review_counts(driver, target_place)
                        if include_review_detail
                        else {"visitor_reviews": None, "blog_reviews": None}
                    )
                    return {
                        "status": "success",
                        "total_rank": total_rank,
                        "pure_rank": pure_rank,
                        "name": text.splitlines()[0],
                        "place_url": target_place["place_url"],
                        "place_id": target_place["place_id"],
                        "visitor_reviews": review_counts["visitor_reviews"],
                        "blog_reviews": review_counts["blog_reviews"]
                    }

                try:
                    driver.switch_to.default_content()
                    WebDriverWait(driver, 4).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "searchIframe")))
                except:
                    pass
            
            try:
                scroll_box = driver.find_element(By.CSS_SELECTOR, "#_pcmap_list_scroll_container")
                # ✨ 튜닝 3: 맨 밑바닥으로 점프하지 않고, 화면 높이(800px)만큼만 살짝살짝 내립니다.
                driver.execute_script("arguments[0].scrollBy(0, 800);", scroll_box)
                # ✨ 튜닝 4: 스크롤 후 쉬는 시간을 1.5초 -> 0.5초로 팍 줄여서 딜레이 최소화!
                time.sleep(0.5) 
            except: break
        print(f"순위 결과 없음. keyword={keyword}, place_id={target_place['place_id']}", flush=True)
        return {
            "status": "need_more" if scan_end < 99 else "not_found",
            "message": f"{scan_start}-{scan_end}위 안에서 찾지 못했습니다.",
            "checked_start": scan_start,
            "checked_end": scan_end,
            "next_start": scan_end + 1 if scan_end < 99 else None,
            "next_end": min(scan_end + 20, 99) if scan_end < 99 else None,
            "place_id": target_place["place_id"],
            "place_url": target_place["place_url"]
        }
    except Exception as e:
        print(f"에러 발생: {e}", flush=True)
        return {"status": "error", "message": str(e)}
    finally:
        if driver and not use_shared_driver:
            driver.quit()

@app.route('/')
def index(): return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    data = request.json
    target_place_url = data.get('target_place_url') or data.get('target_address') or data.get('target_name')
    place_alias = data.get('place_alias')
    user_data = load_history().get(data.get('user_id'), {})
    target_meta = user_data.get("$meta", {}).get(target_place_url, {})
    res = get_ranking(data['keyword'], target_place_url, target_meta=target_meta, fast_only=True, fast_limit=20)
    if res['status'] in ('success', 'out_of_top'):
        save_to_history(
            data['user_id'],
            data['keyword'],
            target_place_url,
            res['total_rank'],
            res['pure_rank'],
            res.get('visitor_reviews'),
            res.get('blog_reviews'),
            res.get('name'),
            place_alias,
            res.get('place_id'),
            preserve_missing_reviews=res['status'] == 'success'
        )
    return jsonify(res)

@app.route('/search_more', methods=['POST'])
def search_more():
    data = request.json
    target_place_url = data.get('target_place_url') or data.get('target_address') or data.get('target_name')
    place_alias = data.get('place_alias')
    start_rank = int(data.get('start_rank', 21))
    end_rank = int(data.get('end_rank', min(start_rank + 19, 99)))
    user_data = load_history().get(data.get('user_id'), {})
    target_meta = user_data.get("$meta", {}).get(target_place_url, {})

    res = get_ranking(
        data['keyword'],
        target_place_url,
        target_meta=target_meta,
        fast_only=False,
        include_review_detail=False,
        scan_start=start_rank,
        scan_end=end_rank
    )
    if res['status'] == 'success':
        save_to_history(
            data['user_id'],
            data['keyword'],
            target_place_url,
            res['total_rank'],
            res['pure_rank'],
            res.get('visitor_reviews'),
            res.get('blog_reviews'),
            res.get('name'),
            place_alias,
            res.get('place_id')
        )
    return jsonify(res)

@app.route('/history', methods=['POST'])
def get_user_history():
    data = request.json
    user_id = data.get('user_id')
    user_data = load_history().get(user_id, {})
    meta_data = user_data.get("$meta", {})
    
    places_group = {}
    
    for key, date_entries in user_data.items():
        if key == "$meta":
            continue
        if "_" not in key:
            continue
        keyword, url = key.split("_", 1)
        
        if url not in places_group:
            meta = meta_data.get(url, {})
            places_group[url] = {
                "target_place_url": url,
                "place_name": meta.get("place_name"),
                "place_alias": meta.get("place_alias"),
                "dates": {}
            }
            
        for date, info in date_entries.items():
            if date not in places_group[url]["dates"]:
                places_group[url]["dates"][date] = {
                    "visitor_reviews": None,
                    "blog_reviews": None,
                    "keywords": []
                }
            
            # 리뷰 데이터 업데이트
            if info.get("visitor_reviews") is not None:
                places_group[url]["dates"][date]["visitor_reviews"] = info.get("visitor_reviews")
            if info.get("blog_reviews") is not None:
                places_group[url]["dates"][date]["blog_reviews"] = info.get("blog_reviews")
                
            # 키워드별 순위 정보 추가
            places_group[url]["dates"][date]["keywords"].append({
                "keyword": keyword,
                "total_rank": info.get("total_rank"),
                "pure_rank": info.get("pure_rank"),
                "time": info.get("time")
            })
            
    formatted_places = []
    for url, content in places_group.items():
        sorted_dates = []
        for d in sorted(content["dates"].keys(), reverse=True):
            sorted_dates.append({
                "date": d,
                "visitor_reviews": content["dates"][d]["visitor_reviews"],
                "blog_reviews": content["dates"][d]["blog_reviews"],
                "keywords": content["dates"][d]["keywords"]
            })
            
        formatted_places.append({
            "target_place_url": url,
            "place_name": content.get("place_name"),
            "place_alias": content.get("place_alias"),
            "history": sorted_dates
        })
        
    return jsonify({"status": "success", "places": formatted_places})

@app.route('/api/update_alias', methods=['POST'])
def update_alias():
    data = request.json
    user_id = data.get('user_id')
    target_place_url = data.get('target_place_url')
    place_alias = data.get('place_alias', '').strip()

    if not user_id or not target_place_url:
        return jsonify({"status": "error", "message": "필수 값이 없습니다."})

    history = load_history()
    if user_id not in history:
        history[user_id] = {}
    if "$meta" not in history[user_id]:
        history[user_id]["$meta"] = {}
    if target_place_url not in history[user_id]["$meta"]:
        history[user_id]["$meta"][target_place_url] = {"target_place_url": target_place_url}

    history[user_id]["$meta"][target_place_url]["place_alias"] = place_alias

    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

    return jsonify({"status": "success"})

@app.route('/api/delete_keyword', methods=['POST'])
def delete_keyword():
    data = request.json
    user_id = data.get('user_id')
    target_place_url = data.get('target_place_url')
    keyword = data.get('keyword', '').strip()

    if not user_id or not target_place_url or not keyword:
        return jsonify({"status": "error", "message": "필수 값이 없습니다."})

    history = load_history()
    user_data = history.get(user_id, {})
    key = f"{keyword}_{target_place_url}"

    if key not in user_data:
        return jsonify({"status": "error", "message": "삭제할 키워드를 찾지 못했습니다."})

    del user_data[key]

    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

    return jsonify({"status": "success"})

@app.route('/api/keyword_rankings', methods=['POST'])
def keyword_rankings():
    data = request.json
    keyword = data.get('keyword', '').strip()
    limit = int(data.get('limit', 20))

    if not keyword:
        return jsonify({"status": "error", "message": "키워드를 입력해 주세요."})

    driver = None
    use_shared_driver = os.environ.get("NAVER_REUSE_DRIVER", "1") == "1"
    try:
        driver = get_shared_driver() if use_shared_driver else create_driver(enable_performance_logs=True)
        results = get_fast_search_results(driver, keyword, limit)
        rows = []
        for idx, result in enumerate(results, start=1):
            rows.append({
                "rank": idx,
                "place_id": result.get("id"),
                "name": result.get("name"),
                "address": result.get("roadAddress") or result.get("address"),
                "category": " > ".join(result.get("category", [])) if result.get("category") else None,
                "visitor_reviews": first_count(
                    result.get("placeReviewCount"),
                    result.get("visitorReviewCount"),
                    result.get("visitorReviewCnt")
                ),
                "blog_reviews": first_count(
                    result.get("blogReviewCount"),
                    result.get("blogReviewCnt"),
                    result.get("blogCafeReviewCount"),
                    result.get("blogCafeReviewCnt"),
                    result.get("blogCafeReview")
                )
            })
        return jsonify({
            "status": "success",
            "keyword": keyword,
            "count": len(rows),
            "rankings": rows
        })
    except Exception as e:
        print(f"키워드 순위표 수집 실패: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)})
    finally:
        if driver and not use_shared_driver:
            driver.quit()

@app.route('/api/refresh_all', methods=['POST'])
def refresh_all():
    user_id = request.json.get('user_id')
    user_data = load_history().get(user_id, {})
    targets = [(kw, tpu) for key in user_data.keys() if key != "$meta" and "_" in key for kw, tpu in [key.split("_", 1)]]
    if not targets: return jsonify({'status': 'error', 'message': '저장된 기록이 없습니다.'})
    return start_refresh_job(user_id, targets)

@app.route('/api/refresh_place', methods=['POST'])
def refresh_place():
    data = request.json
    user_id = data.get('user_id')
    target_place_url = data.get('target_place_url')
    user_data = load_history().get(user_id, {})
    targets = [
        (kw, tpu)
        for key in user_data.keys()
        if key != "$meta" and "_" in key
        for kw, tpu in [key.split("_", 1)]
        if tpu == target_place_url
    ]
    if not targets:
        return jsonify({'status': 'error', 'message': '이 플레이스에 저장된 키워드가 없습니다.'})
    return start_refresh_job(user_id, targets, label="플레이스")

def start_refresh_job(user_id, targets, label="전체"):
    with REFRESH_LOCK:
        job = REFRESH_JOBS.get(user_id)
        if job and job.get("status") == "running":
            return jsonify(job)

        REFRESH_JOBS[user_id] = {
            "status": "running",
            "total": len(targets),
            "done": 0,
            "success_count": 0,
            "current_keyword": "",
            "message": f"{label} {len(targets)}개 갱신을 시작했습니다.",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None
        }

    thread = threading.Thread(target=run_refresh_job, args=(user_id, targets), daemon=True)
    thread.start()
    return jsonify(REFRESH_JOBS[user_id])

@app.route('/api/refresh_status', methods=['POST'])
def refresh_status():
    user_id = request.json.get('user_id')
    with REFRESH_LOCK:
        job = REFRESH_JOBS.get(user_id)
        if not job:
            return jsonify({"status": "idle", "message": "진행 중인 업데이트가 없습니다."})
        return jsonify(job)

def run_refresh_job(user_id, targets):
    try:
        for kw, tpu in targets:
            with REFRESH_LOCK:
                REFRESH_JOBS[user_id]["current_keyword"] = kw
                REFRESH_JOBS[user_id]["message"] = f"{kw} 갱신 중..."

            try:
                user_data = load_history().get(user_id, {})
                meta = user_data.get("$meta", {}).get(tpu, {})
                res = get_ranking(kw, tpu, target_meta=meta, fast_only=True, include_review_detail=True, fast_limit=20)
                if res['status'] in ('success', 'out_of_top'):
                    save_to_history(
                        user_id,
                        kw,
                        tpu,
                        res['total_rank'],
                        res['pure_rank'],
                        res.get('visitor_reviews'),
                        res.get('blog_reviews'),
                        res.get('name'),
                        meta.get("place_alias"),
                        res.get('place_id'),
                        preserve_missing_reviews=True
                    )
                    with REFRESH_LOCK:
                        if res['status'] == 'success':
                            REFRESH_JOBS[user_id]["success_count"] += 1
            except Exception as e:
                print(f"전체 최신화 실패({kw}): {e}", flush=True)
            finally:
                with REFRESH_LOCK:
                    REFRESH_JOBS[user_id]["done"] += 1

        with REFRESH_LOCK:
            job = REFRESH_JOBS[user_id]
            job["status"] = "success"
            job["current_keyword"] = ""
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["message"] = f"총 {job['total']}개 중 {job['success_count']}개 갱신 완료!"
    except Exception as e:
        with REFRESH_LOCK:
            REFRESH_JOBS[user_id]["status"] = "error"
            REFRESH_JOBS[user_id]["message"] = f"업데이트 중 오류가 발생했습니다: {e}"
            REFRESH_JOBS[user_id]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", "8080"))
    app.run(debug=False, use_reloader=False, port=port, host='0.0.0.0')
