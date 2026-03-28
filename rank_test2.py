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
from datetime import datetime

app = Flask(__name__)
HISTORY_FILE = "rank_history.json"

def save_to_history(user_id, keyword, target_name, total_rank, pure_rank):
    today = datetime.now().strftime("%Y-%m-%d")
    
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            try: history = json.load(f)
            except: history = {}
    else:
        history = {}

    # 1. 사용자 ID 방 만들기
    if user_id not in history:
        history[user_id] = {}

    # 2. 키워드 방 만들기
    key = f"{keyword}_{target_name}"
    if key not in history[user_id]:
        history[user_id][key] = {}

    # 3. 날짜별 기록 덮어쓰기 (최신화)
    history[user_id][key][today] = {
        "total_rank": total_rank,
        "pure_rank": pure_rank,
        "time": datetime.now().strftime("%H:%M:%S")
    }

    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

def get_ranking(keyword, target_name):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920x1080")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    try:
        driver.get(f"https://map.naver.com/v5/search/{keyword}")
        time.sleep(5)
        
        try:
            driver.switch_to.frame("searchIframe")
        except:
            return {"status": "error", "message": "iframe_not_found"}
            
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
                is_ad = "광고" in item_text or "AD" in item_text
                
                if not is_ad:
                    pure_rank += 1
                    
                if target_name.replace(" ", "") in item_text.replace(" ", ""):
                    result_data = {
                        "status": "success",
                        "total_rank": total_rank,
                        "pure_rank": pure_rank,
                        "name": item_text.splitlines()[0]
                    }
                    found = True
                    break
            if found: break
            scroll_box.send_keys(Keys.PAGE_DOWN)
            time.sleep(1)
            
        return result_data
    except Exception as e:
        print(f"에러: {e}")
        return {"status": "error"}
    finally:
        driver.quit()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    data = request.json
    user_id = data.get('user_id') # 프론트엔드에서 보낸 사용자 ID 받기
    keyword = data.get('keyword')
    target_name = data.get('target_name')
    
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
                return jsonify(history.get(user_id, {})) # 해당 사용자의 데이터만 쏙 빼서 줍니다!
            except: pass
    return jsonify({})

if __name__ == '__main__':
    app.run(debug=True, port=5000)