from flask import Flask, render_template, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)
HISTORY_FILE = "rank_history.json"

def save_to_history(user_id, keyword, target_name, total_rank, pure_rank):
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            try: history = json.load(f)
            except: history = {}
    else:
        history = {}

    if user_id not in history:
        history[user_id] = {}

    key = f"{keyword}_{target_name}"
    if key not in history[user_id]:
        history[user_id][key] = {
            yesterday_str: {
                "total_rank": total_rank,
                "pure_rank": pure_rank,
                "time": "00:00:00"
            }
        }

    history[user_id][key][today_str] = {
        "total_rank": total_rank,
        "pure_rank": pure_rank,
        "time": today.strftime("%H:%M:%S")
    }

    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

def get_ranking(keyword, target_name):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")  # ✨ 서버 필수 설정
    chrome_options.add_argument("--disable-dev-shm-usage")  # ✨ 메모리 부족 방지
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    
    # 🚀 서버에 설치된 구글 크롬 경로를 자동으로 찾는 설정
    if os.path.exists("/usr/bin/google-chrome"):
        chrome_options.binary_location = "/usr/bin/google-chrome"
    elif os.path.exists("/usr/bin/chromium-browser"):
        chrome_options.binary_location = "/usr/bin/chromium-browser"

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        driver.get(f"https://map.naver.com/v5/search/{keyword}")
        time.sleep(7)
        try: driver.switch_to.frame("searchIframe")
        except: return {"status": "error"}
        
        scroll_box = driver.find_element(By.CSS_SELECTOR, "#_pcmap_list_scroll_container")
        found = False
        result_data = {"status": "not_found"}
        
        for i in range(15):
            all_items = driver.find_elements(By.CSS_SELECTOR, "li")
            total_rank = 0
            pure_rank = 0
            for item in all_items:
                item_text = item.text
                if not item_text: continue
                total_rank += 1
                if not ("광고" in item_text or "AD" in item_text):
                    pure_rank += 1
                if target_name.replace(" ", "") in item_text.replace(" ", ""):
                    result_data = {"status": "success", "total_rank": total_rank, "pure_rank": pure_rank, "name": item_text.splitlines()[0]}
                    found = True
                    break
            if found: break
            scroll_box.send_keys(Keys.PAGE_DOWN)
            time.sleep(1.5)
        return result_data
    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error"}
    finally:
        try: driver.quit()
        except: pass

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    data = request.json
    user_id, keyword, target_name = data.get('user_id'), data.get('keyword'), data.get('target_name')
    result = get_ranking(keyword, target_name)
    if result.get("status") == "success":
        save_to_history(user_id, keyword, target_name, result["total_rank"], result["pure_rank"])
    return jsonify(result)

@app.route('/history', methods=['POST'])
def get_user_history():
    data = request.json
    user_id = data.get('user_id')
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                return jsonify(history.get(user_id, {}))
            except: pass
    return jsonify({})

if __name__ == '__main__':
    # 🚀 Render 서버 환경에 맞춰서 포트 번호를 자동으로 잡게 수정했습니다.
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, port=port, host='0.0.0.0')