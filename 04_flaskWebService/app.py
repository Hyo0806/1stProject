import os
import json
import re
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import joblib
import oracledb
from flask import Flask, render_template, request
from dotenv import load_dotenv

# sklearn 버전 경고 무시
warnings.filterwarnings('ignore', category=UserWarning)

load_dotenv()

# =========================
# Config
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
DATA_DIR = os.path.join(BASE_DIR, "data")

KMA_SERVICE_KEY = os.getenv("KMA_SERVICE_KEY", "")
KMA_BASE_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"

# Oracle DB 설정 (선생님 서버)
ORACLE_HOST = os.getenv("ORACLE_HOST", "210.121.189.12")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SID = os.getenv("ORACLE_SID", "xe")
ORACLE_USER = os.getenv("ORACLE_USER", "scott")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "tiger")

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# 날씨 캐시 (API 호출 최소화)
# =========================
WEATHER_CACHE = {}
CACHE_FILE = os.path.join(DATA_DIR, "weather_cache.json")

def _load_weather_cache():
    """캐시 파일 로드"""
    global WEATHER_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                WEATHER_CACHE = json.load(f)
            print(f"✅ 날씨 캐시 로드: {len(WEATHER_CACHE)}개")
        except:
            WEATHER_CACHE = {}
    else:
        WEATHER_CACHE = {}

def _save_weather_cache():
    """캐시 파일 저장"""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(WEATHER_CACHE, f, ensure_ascii=False, indent=2)
    except:
        pass

def _get_cached_weather(date_ymd: str, nx: int, ny: int, api_type: str):
    """캐시된 날씨 조회"""
    key = f"{date_ymd}_{nx}_{ny}_{api_type}"
    return WEATHER_CACHE.get(key)

def _set_cached_weather(date_ymd: str, nx: int, ny: int, api_type: str, temp: float, rain: float):
    """날씨 캐시 저장"""
    key = f"{date_ymd}_{nx}_{ny}_{api_type}"
    WEATHER_CACHE[key] = {"temp": temp, "rain": rain, "cached_at": datetime.now().isoformat()}
    _save_weather_cache()

# 서버 시작 시 캐시 로드
_load_weather_cache()

# =========================
# Oracle DB 연결
# =========================
def init_oracle_client():
    """Oracle Client 초기화 (필요시)"""
    try:
        oracledb.init_oracle_client()
        print(f"✅ Oracle Client 초기화 완료")
    except Exception as e:
        # 이미 초기화되었거나 불필요
        pass

def get_oracle_connection():
    """Oracle DB 연결 객체 반환"""
    try:
        conn = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            host=ORACLE_HOST,
            port=ORACLE_PORT,
            sid=ORACLE_SID
        )
        return conn
    except oracledb.Error as error:
        print(f"❌ Oracle 연결 실패: {error}")
        return None

# Oracle Client 초기화
init_oracle_client()

# 연결 테스트
print(f"\n{'='*60}")
print(f"🔌 Oracle DB 연결 테스트")
print(f"{'='*60}")
test_conn = get_oracle_connection()
if test_conn:
    print(f"✅ Oracle DB 연결 성공!")
    print(f"   Host: {ORACLE_HOST}:{ORACLE_PORT}")
    print(f"   SID: {ORACLE_SID}")
    print(f"   User: {ORACLE_USER}")
    test_conn.close()
else:
    print(f"❌ Oracle DB 연결 실패")
print(f"{'='*60}\n")

# =========================
# Load location mapping
# =========================
loc_path = os.path.join(DATA_DIR, "suwon_locations.json")
if not os.path.exists(loc_path):
    raise FileNotFoundError(f"동/격자 파일이 없습니다: {loc_path}")

with open(loc_path, "r", encoding="utf-8") as f:
    LOC = json.load(f)

# =========================
# Time labels
# =========================
TIME_LABELS = {
    1: "00:00 ~ 06:59",
    2: "07:00 ~ 08:59",
    3: "09:00 ~ 10:59",
    4: "11:00 ~ 12:59",
    5: "13:00 ~ 14:59",
    6: "15:00 ~ 16:59",
    7: "17:00 ~ 18:59",
    8: "19:00 ~ 20:59",
    9: "21:00 ~ 22:59",
    10: "23:00 ~ 23:59",
}

# =========================
# Date range for actual data
# =========================
ACTUAL_START_YMD = "20220101"
ACTUAL_END_YMD   = "20251031"

def _norm_dong_name(x: str) -> str:
    """'수원시 팔달구 행궁동' -> '행궁동' 처럼 동 이름을 정규화"""
    if x is None:
        return ""
    s = str(x).strip()
    if not s:
        return ""
    
    # 공백 모두 제거
    s = s.replace(" ", "")
    
    # '... 행궁동' 같은 패턴에서 마지막 'OO동'만 추출
    m = re.findall(r"([가-힣0-9]+동)", s)
    result = m[-1] if m else s
    
    # 최종 결과에서도 공백 제거
    return result.strip().replace(" ", "")

# =========================
# Oracle DB 조회 함수
# =========================
def _get_actual_hour_from_db(ymd8: str, dong_norm: str, hour: int):
    """Oracle DB에서 특정 시간대 데이터 조회"""
    conn = get_oracle_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        
        # 테이블명: SALES_DATA (import_csv_to_oracle.py로 생성)
        query = """
            SELECT AMT, CNT, TEMP, RAIN
            FROM SALES_DATA
            WHERE TA_YMD = :ymd
              AND DONG = :dong
              AND HOUR = :hour
        """
        
        cursor.execute(query, ymd=ymd8, dong=dong_norm, hour=hour)
        row = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if row:
            return {
                "amt": float(row[0]) if row[0] is not None else np.nan,
                "cnt": float(row[1]) if row[1] is not None else np.nan,
                "temp": float(row[2]) if row[2] is not None else np.nan,
                "rain": float(row[3]) if row[3] is not None else np.nan,
            }
        return None
        
    except oracledb.Error as error:
        print(f"❌ DB 조회 실패: {error}")
        if conn:
            conn.close()
        return None

def _get_actual_weather_day_from_db(ymd8: str, dong_norm: str):
    """Oracle DB에서 해당 날짜/동의 평균 TEMP/RAIN 조회"""
    conn = get_oracle_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        
        query = """
            SELECT AVG(TEMP), AVG(RAIN)
            FROM SALES_DATA
            WHERE TA_YMD = :ymd
              AND DONG = :dong
              AND TEMP IS NOT NULL
        """
        
        cursor.execute(query, ymd=ymd8, dong=dong_norm)
        row = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if row and row[0] is not None:
            return float(row[0]), float(row[1] or 0.0), "Oracle DB(실제데이터)"
        return None
        
    except oracledb.Error as error:
        print(f"❌ 날씨 조회 실패: {error}")
        if conn:
            conn.close()
        return None

def _check_actual_data_exists(ymd8: str, dong_norm: str):
    """해당 날짜/동의 데이터가 DB에 있는지 확인"""
    conn = get_oracle_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        
        query = """
            SELECT COUNT(*)
            FROM SALES_DATA
            WHERE TA_YMD = :ymd
              AND DONG = :dong
        """
        
        cursor.execute(query, ymd=ymd8, dong=dong_norm)
        count = cursor.fetchone()[0]
        
        cursor.close()
        conn.close()
        
        return count > 0
        
    except oracledb.Error as error:
        print(f"❌ 데이터 존재 확인 실패: {error}")
        if conn:
            conn.close()
        return False

# =========================
# Load ML models
# =========================
def _load_models():
    """시간대별 머신러닝 모델 로드 (hour_01 ~ hour_10)"""
    models = {}
    for hour in range(1, 11):
        model_path = os.path.join(MODELS_DIR, f"hour_{hour:02d}_amt_cnt.joblib")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"모델 파일이 없습니다: {model_path}")
        try:
            models[hour] = joblib.load(model_path)
            print(f"✓ Loaded model for hour {hour}")
        except Exception as e:
            raise RuntimeError(f"모델 로드 실패 (hour {hour}): {e}")
    return models

MODELS = _load_models()

# =========================
# KMA helpers
# =========================
ASOS_STN_ID = 119  # 수원 관측소 ID
ASOS_BASE_URL = "http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"

def _kma_get(url: str, params: dict):
    if not KMA_SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY가 .env에 없습니다.")
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def get_ultra_now(nx: int, ny: int):
    # 캐시 확인 (오늘 날짜)
    today = datetime.now().strftime("%Y%m%d")
    cached = _get_cached_weather(today, nx, ny, "ultra")
    if cached:
        print(f"📦 캐시 사용 (초단기실황): {cached['temp']}℃, {cached['rain']}mm", flush=True)
        return cached['temp'], cached['rain']
    
    now = datetime.now()
    base_date = now.strftime("%Y%m%d")
    t = now - timedelta(hours=1)
    base_time = t.strftime("%H00")

    url = f"{KMA_BASE_URL}/getUltraSrtNcst"
    params = {
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": "200",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": str(nx),
        "ny": str(ny),
    }
    data = _kma_get(url, params)
    items = data["response"]["body"]["items"]["item"]
    out = {it["category"]: it["obsrValue"] for it in items}

    temp = float(out.get("T1H", 0.0))
    rain = float(out.get("RN1", 0.0))
    
    # 캐시 저장
    _set_cached_weather(today, nx, ny, "ultra", temp, rain)
    
    return temp, rain

def get_vilage_day_avg(nx: int, ny: int, target_date: str):
    # 캐시 확인
    cached = _get_cached_weather(target_date, nx, ny, "village")
    if cached:
        print(f"📦 캐시 사용 (단기예보): {cached['temp']}℃, {cached['rain']}mm", flush=True)
        return cached['temp'], cached['rain']
    
    url = f"{KMA_BASE_URL}/getVilageFcst"

    def _call(base_date: str):
        params = {
            "serviceKey": KMA_SERVICE_KEY,
            "pageNo": "1",
            "numOfRows": "2500",
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": "0500",
            "nx": str(nx),
            "ny": str(ny),
        }
        return _kma_get(url, params)

    today = datetime.now().strftime("%Y%m%d")
    try:
        data = _call(today)
    except Exception:
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        data = _call(yday)

    items = data["response"]["body"]["items"]["item"]
    t_list, r_list = [], []

    for it in items:
        if it.get("fcstDate") != target_date:
            continue

        cat = it.get("category")
        val = it.get("fcstValue")

        if cat == "TMP":
            try:
                t_list.append(float(val))
            except:
                pass
        elif cat == "PCP":
            if val not in ("강수없음", None):
                try:
                    v = val.replace("mm", "").strip()
                    if "미만" in v:
                        r_list.append(0.0)
                    else:
                        r_list.append(float(v))
                except:
                    pass

    temp_avg = float(np.mean(t_list)) if t_list else 15.0
    rain_avg = float(np.mean(r_list)) if r_list else 0.0
    
    # 캐시 저장
    _set_cached_weather(target_date, nx, ny, "village", temp_avg, rain_avg)
    
    return temp_avg, rain_avg

def get_asos_daily_obs(ymd8: str):
    # 캐시 확인
    cached = _get_cached_weather(ymd8, 119, 119, "asos")
    if cached:
        print(f"📦 캐시 사용 (ASOS): {cached['temp']}℃, {cached['rain']}mm", flush=True)
        return cached['temp'], cached['rain']
    
    params = {
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "dataType": "JSON",
        "dataCd": "ASOS",
        "dateCd": "DAY",
        "startDt": ymd8,
        "endDt": ymd8,
        "stnIds": str(ASOS_STN_ID),
    }
    js = _kma_get(ASOS_BASE_URL, params)
    items = js.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    if not items:
        raise RuntimeError(f"ASOS 관측 데이터가 없습니다 (날짜: {ymd8})")
    it = items[0]
    avg_ta = float(it.get("avgTa")) if it.get("avgTa") not in (None, "") else 0.0
    sum_rn = float(it.get("sumRn")) if it.get("sumRn") not in (None, "") else 0.0
    
    # 캐시 저장
    _set_cached_weather(ymd8, 119, 119, "asos", avg_ta, sum_rn)
    
    return avg_ta, sum_rn

def predict_amt_cnt_ml(gu: str, dong: str, hour: int, day: int, temp: float, rain: float = 0.0):
    if hour not in MODELS:
        return 0.0, 0.0
    
    model = MODELS[hour]
    attempts = [
        {'DONG': dong, 'DAY': day, 'TEMP': temp, 'RAIN': rain},
        {'GU': gu, 'DONG': dong, 'DAY': day, 'TEMP': temp, 'RAIN': rain},
        {'DONG': dong, 'DAY': day, 'TEMP': temp},
        {'GU': gu, 'DONG': dong, 'DAY': day, 'TEMP': temp},
    ]
    
    for features in attempts:
        try:
            X = pd.DataFrame([features])
            pred = model.predict(X)[0]
            amt = max(0.0, float(pred[0]))
            cnt = max(0.0, float(pred[1]))
            return amt, cnt
        except:
            continue
    
    return 0.0, 0.0

# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def index():
    gus = sorted(LOC.keys())
    return render_template(
        "index.html",
        gus=gus,
        loc_json=json.dumps(LOC, ensure_ascii=False),
    )

@app.route("/predict", methods=["POST"])
def predict():
    gu = request.form.get("gu")
    dong = request.form.get("dong")
    ymd = request.form.get("date")

    if not (gu and dong and ymd):
        return "입력값이 부족합니다.", 400

    if gu not in LOC:
        return f"선택한 구가 LOC에 없음: {gu}", 400
    if dong not in LOC[gu]:
        return f"선택한 동이 LOC[{gu}]에 없음: {dong}", 400

    target_ymd = ymd.replace("-", "")
    dt = datetime.strptime(target_ymd, "%Y%m%d")
    day = dt.weekday() + 1

    nx = int(LOC[gu][dong]["nx"])
    ny = int(LOC[gu][dong]["ny"])

    print(f"\n{'='*50}", flush=True)
    print(f"📍 예측 요청: {gu} {dong}, 날짜: {ymd}, 요일: {day}", flush=True)
    print(f"{'='*50}", flush=True)

    dong_norm = _norm_dong_name(dong)
    print(f"🔍 동 정규화: '{dong}' -> '{dong_norm}'", flush=True)
    
    today_ymd = datetime.now().strftime("%Y%m%d")
    one_week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    # 날씨 조회
    weather_error = None
    actual_weather = _get_actual_weather_day_from_db(target_ymd, dong_norm)

    try:
        if actual_weather is not None:
            temp, rain, weather_source = actual_weather
        elif target_ymd == today_ymd:
            temp, rain = get_ultra_now(nx, ny)
            weather_source = "초단기실황(getUltraSrtNcst)"
        elif target_ymd > today_ymd:
            temp, rain = get_vilage_day_avg(nx, ny, target_ymd)
            weather_source = "단기예보(getVilageFcst) 일평균"
        elif target_ymd >= one_week_ago:
            # 최근 7일 이내: 단기예보 시도
            temp, rain = get_vilage_day_avg(nx, ny, target_ymd)
            weather_source = "단기예보(getVilageFcst) 일평균 (최근 과거)"
        else:
            # 7일 이전: 월별 평균 사용 (ASOS API 사용 안 함!)
            month = int(target_ymd[4:6])
            avg_temps = {1: -2, 2: 1, 3: 7, 4: 14, 5: 19, 6: 23, 
                        7: 26, 8: 26, 9: 21, 10: 14, 11: 7, 12: 0}
            temp = float(avg_temps.get(month, 15))
            rain = 0.0
            weather_source = f"월별 평균 기온 ({month}월)"
            print(f"💡 7일 이전 날짜 → 월별 평균 사용", flush=True)
        print(f"🌤️  날씨: TEMP={temp}℃, RAIN={rain}mm ({weather_source})", flush=True)
    except Exception as e:
        print(f"⚠️  날씨 조회 실패: {e}", flush=True)
        weather_error = str(e)
        
        # 429 에러(호출 제한) 처리
        if "429" in str(e) or "Too Many Requests" in str(e):
            print(f"💡 API 호출 제한 도달. 최근 평균 날씨로 대체합니다.", flush=True)
            # 같은 월의 평균 날씨 사용
            month = int(target_ymd[4:6])
            # 월별 평균 기온 (수원 기준)
            avg_temps = {1: -2, 2: 1, 3: 7, 4: 14, 5: 19, 6: 23, 
                        7: 26, 8: 26, 9: 21, 10: 14, 11: 7, 12: 0}
            temp = float(avg_temps.get(month, 15))
            rain = 0.0
            weather_source = f"월별 평균 기온 (API 제한)"
            weather_error = "API 호출 제한 (429)"
        else:
            # 기타 에러
            try:
                temp, rain = get_vilage_day_avg(nx, ny, target_ymd)
                weather_source = "단기예보(getVilageFcst) 일평균(폴백)"
                print(f"🌤️  폴백 성공: TEMP={temp}℃, RAIN={rain}mm ({weather_source})", flush=True)
            except Exception as e2:
                temp, rain = 15.0, 0.0
                weather_source = "날씨 조회 실패 → 기본값(TEMP=15℃, RAIN=0mm)"
                weather_error = f"{e} | {e2}"
                print(f"⚠️  기본값 사용: TEMP={temp}℃, RAIN={rain}mm", flush=True)

    results = []
    total_amt = 0
    total_cnt = 0

    # 실제 데이터 사용 가능 여부 확인
    use_actual = (ACTUAL_START_YMD <= target_ymd <= ACTUAL_END_YMD)
    has_any_actual = False
    
    if use_actual:
        has_any_actual = _check_actual_data_exists(target_ymd, dong_norm)
    
    print(f"\n📊 데이터 사용 판단:", flush=True)
    print(f"  - target_ymd: {target_ymd}", flush=True)
    print(f"  - dong (원본): '{dong}'", flush=True)
    print(f"  - dong_norm (정규화): '{dong_norm}'", flush=True)
    print(f"  - use_actual (날짜 범위): {use_actual}", flush=True)
    print(f"  - has_any_actual (DB 데이터 존재): {has_any_actual}", flush=True)

    if use_actual and has_any_actual:
        print(f"✅ 실제데이터 사용 (Oracle DB): {target_ymd} / {dong_norm}", flush=True)
        data_type = "actual"
        
        for hour in range(1, 11):
            rec = _get_actual_hour_from_db(target_ymd, dong_norm, hour)
            if rec and (not np.isnan(rec.get("amt", np.nan))) and (not np.isnan(rec.get("cnt", np.nan))):
                amt_i = int(round(rec["amt"]))
                cnt_i = int(round(rec["cnt"]))
                src = "실제"
            else:
                pred_amt, pred_cnt = predict_amt_cnt_ml(
                    gu=gu, dong=dong, hour=hour, day=day, temp=temp, rain=rain
                )
                amt_i = int(round(pred_amt))
                cnt_i = int(round(pred_cnt))
                src = "예측(누락보정)"

            total_amt += amt_i
            total_cnt += cnt_i

            results.append({
                "HOUR": hour,
                "HOUR_LABEL": TIME_LABELS.get(hour, ""),
                "PRED_AMT_STR": f"{amt_i:,}원",
                "PRED_CNT_STR": f"{cnt_i:,}건",
                "VALUE_SOURCE": src,
            })
    else:
        print(f"🔮 예측 사용: {target_ymd} / {dong_norm}", flush=True)
        data_type = "prediction"
        
        for hour in range(1, 11):
            pred_amt, pred_cnt = predict_amt_cnt_ml(
                gu=gu, dong=dong, hour=hour, day=day, temp=temp, rain=rain
            )

            amt_i = int(round(pred_amt))
            cnt_i = int(round(pred_cnt))

            total_amt += amt_i
            total_cnt += cnt_i

            results.append({
                "HOUR": hour,
                "HOUR_LABEL": TIME_LABELS.get(hour, ""),
                "PRED_AMT_STR": f"{amt_i:,}원",
                "PRED_CNT_STR": f"{cnt_i:,}건",
                "VALUE_SOURCE": "예측",
            })
            
    total_amt_str = f"{total_amt:,}원"
    total_cnt_str = f"{total_cnt:,}건"
    
    print(f"\n📊 총합: AMT={total_amt_str}, CNT={total_cnt_str}", flush=True)
    print(f"{'='*50}\n", flush=True)

    return render_template(
        "result.html",
        gu=gu,
        dong=dong,
        date=ymd,
        nx=nx,
        ny=ny,
        temp=temp,
        rain=rain,
        weather_source=weather_source,
        weather_error=weather_error,
        results=results,
        total_amt_str=total_amt_str,
        total_cnt_str=total_cnt_str,
        data_type=data_type,
    )

if __name__ == "__main__":
    print("\n" + "="*50)
    print("🚀 수원시 시간대별 예상매출 예측 서버 시작")
    print("="*50 + "\n")
    app.run(debug=True)
