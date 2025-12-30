"""
시간대별 AMT/CNT 예측 (수원시 한식 동별 데이터)
- 입력: 날짜(TA_YMD), DONG, (기상청 API로 가져온) 시간대별 TEMP/RAIN
- 출력: 시간대별 AMT, CNT
"""

import os
import joblib
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_absolute_error

from sklearn.neural_network import MLPRegressor
from xgboost import XGBRegressor
from dotenv import load_dotenv

# =========================
# A) 설정
# =========================
load_dotenv('')
SERVICE_KEY = os.getenv('RAIN_ID')

DATA_CSV = "data/수원시 한식 동별 데이터백업.csv"
MODEL_DIR = "data/models_hourly_amt_cnt"
os.makedirs(MODEL_DIR, exist_ok=True)

# ✅ 예측 결과 저장 폴더(명시)
OUTPUT_DIR = "data/pred_outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

KMA_VILAGE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
BASE_TIMES = ["2300", "2000", "1700", "1400", "1100", "0800", "0500", "0200"]

HOUR_SLOT_TO_REP_TIME = {
    1: "0300",
    2: "0800",
    3: "1000",
    4: "1200",
    5: "1400",
    6: "1600",
    7: "1800",
    8: "2000",
    9: "2200",
    10: "2300",
}

DONG_GRID_XLSX = "data/수원시 격자.xlsx"
_DONG_TO_GRID = None  # lazy cache


# =========================
# B) 유틸: 요일 계산 (1~7, 월=1 ... 일=7)
# =========================
def ymd_to_day_1to7(yyyymmdd: str) -> int:
    dt = datetime.strptime(str(yyyymmdd), "%Y%m%d")
    return dt.weekday() + 1


# =========================
# DONG -> (nx,ny) 로딩/조회
# =========================
def load_dong_to_grid(xlsx_path: str = DONG_GRID_XLSX) -> dict:
    global _DONG_TO_GRID
    if _DONG_TO_GRID is not None:
        return _DONG_TO_GRID

    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"격자 파일이 없습니다: {xlsx_path}")

    df = pd.read_excel(xlsx_path, header=None)
    if df.shape[1] < 3:
        raise ValueError(f"격자 파일 컬럼 수가 부족합니다(A,B,C 필요): {xlsx_path}")

    df = df.iloc[:, :3].copy()
    df.columns = ["DONG_NAME", "nx", "ny"]

    df["DONG_NAME"] = df["DONG_NAME"].astype(str).str.strip()
    df["nx"] = pd.to_numeric(df["nx"], errors="coerce")
    df["ny"] = pd.to_numeric(df["ny"], errors="coerce")
    df = df.dropna(subset=["nx", "ny"]).copy()
    df["nx"] = df["nx"].astype(int)
    df["ny"] = df["ny"].astype(int)

    _DONG_TO_GRID = {row["DONG_NAME"]: (int(row["nx"]), int(row["ny"])) for _, row in df.iterrows()}
    return _DONG_TO_GRID


def find_grid_by_dong(dong: str, xlsx_path: str = DONG_GRID_XLSX) -> tuple[int, int]:
    dong = str(dong).strip()
    if not dong:
        raise ValueError("dong 입력이 비었습니다.")

    dmap = load_dong_to_grid(xlsx_path)

    if dong in dmap:
        return dmap[dong]

    keys = list(dmap.keys())
    cand = []
    for k in keys:
        if (k in dong) or (dong in k):
            cand.append(k)

    if not cand:
        tok = dong.split()[-1]
        for k in keys:
            if tok in k or k in tok:
                cand.append(k)

    if not cand:
        raise ValueError(f"'{dong}'에 해당하는 nx/ny를 격자 엑셀에서 찾지 못했습니다. A열 동이름을 확인하세요.")

    cand = sorted(cand, key=len)
    return dmap[cand[0]]


# =========================
# C) 기상청 API: base_date/base_time 결정
# =========================
def pick_base_datetime_kst(now_kst: datetime) -> tuple[str, str]:
    today = now_kst.strftime("%Y%m%d")
    hhmm = now_kst.strftime("%H%M")

    for bt in BASE_TIMES:
        if hhmm >= bt:
            return today, bt

    yday = (now_kst - timedelta(days=1)).strftime("%Y%m%d")
    return yday, "2300"


# =========================
# D) 기상청 단기예보 호출 + 특정 날짜/시각 TMP, PCP 추출
# =========================
def fetch_vilage_fcst_items(service_key: str, base_date: str, base_time: str, nx: int, ny: int,
                            num_of_rows: int = 2000) -> list[dict]:
    params = {
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": num_of_rows,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }
    r = requests.get(KMA_VILAGE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["response"]["body"]["items"]["item"]


def parse_pcp_to_float(pcp_val) -> float:
    if pcp_val is None:
        return 0.0
    s = str(pcp_val).strip()
    if s in ("강수없음", "없음", ""):
        return 0.0
    s = s.replace("mm", "").replace("미만", "").strip()
    try:
        return float(s)
    except:
        return 0.0


def get_temp_rain_for_datetime(service_key: str, dong: str, target_yyyymmdd: str, target_hhmm: str) -> tuple[float, float]:
    nx, ny = find_grid_by_dong(dong)

    now_kst = datetime.now(timezone(timedelta(hours=9)))
    base_date, base_time = pick_base_datetime_kst(now_kst)

    items = fetch_vilage_fcst_items(service_key, base_date, base_time, nx, ny)

    tmp = None
    pcp = None
    for it in items:
        if it.get("fcstDate") == target_yyyymmdd and it.get("fcstTime") == target_hhmm:
            cat = it.get("category")
            if cat == "TMP":
                tmp = float(it.get("fcstValue"))
            elif cat == "PCP":
                pcp = parse_pcp_to_float(it.get("fcstValue"))

    if tmp is None:
        tmp = 0.0
    if pcp is None:
        pcp = 0.0

    return tmp, pcp


def get_hourly_weather_features(service_key: str, dong: str, yyyymmdd: str) -> pd.DataFrame:
    day_1to7 = ymd_to_day_1to7(yyyymmdd)

    rows = []
    for hour_slot in range(1, 11):
        rep_time = HOUR_SLOT_TO_REP_TIME[hour_slot]
        temp, rain = get_temp_rain_for_datetime(service_key, dong, yyyymmdd, rep_time)
        rows.append({
            "TA_YMD": int(yyyymmdd),
            "DONG": dong,
            "HOUR": hour_slot,
            "DAY": day_1to7,
            "TEMP": float(temp),
            "RAIN": float(rain),
        })
    return pd.DataFrame(rows)


# =========================
# E) 모델 학습 (시간대별 저장)
# =========================
def build_preprocess():
    cat_cols = ["DONG"]
    num_cols = ["DAY", "TEMP", "RAIN"]
    pre = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore"), cat_cols),
            ("num", "passthrough", num_cols),
        ],
        remainder="drop",
    )
    return pre


def train_and_save_models(random_state: int = 42) -> pd.DataFrame:
    df = pd.read_csv(DATA_CSV)

    df["DONG"] = df["DONG"].astype(str)
    df["HOUR"] = df["HOUR"].astype(int)
    df["DAY"] = df["DAY"].astype(int)
    df["TEMP"] = df["TEMP"].astype(float)
    df["RAIN"] = df["RAIN"].astype(float)

    y_cols = ["AMT", "CNT"]
    metrics = []

    for h in range(1, 11):
        sub = df[df["HOUR"] == h].copy()
        sub = sub.dropna(subset=["DONG", "DAY", "TEMP", "RAIN", "AMT", "CNT"])

        X = sub[["DONG", "DAY", "TEMP", "RAIN"]]
        y = sub[y_cols]

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=random_state, shuffle=True
        )

        pre = build_preprocess()

        if h == 1:
            base_model = MLPRegressor(
                hidden_layer_sizes=(128, 64),
                activation="relu",
                solver="adam",
                alpha=1e-4,
                batch_size=256,
                learning_rate_init=1e-3,
                max_iter=300,
                random_state=42,
                early_stopping=True,
                n_iter_no_change=10,
                verbose=False,
            )
        else:
            base_model = XGBRegressor(
                n_estimators=600,
                learning_rate=0.05,
                max_depth=6,
                subsample=0.9,
                colsample_bytree=0.9,
                reg_alpha=0.0,
                reg_lambda=1.0,
                objective="reg:squarederror",
                random_state=random_state,
                tree_method="hist",
                n_jobs=-1,
            )

        model = MultiOutputRegressor(base_model)

        pipe = Pipeline([
            ("pre", pre),
            ("model", model),
        ])

        pipe.fit(X_train, y_train)

        pred = pipe.predict(X_test)
        mae_amt = mean_absolute_error(y_test["AMT"].values, pred[:, 0])
        mae_cnt = mean_absolute_error(y_test["CNT"].values, pred[:, 1])

        model_path = os.path.join(MODEL_DIR, f"hour_{h:02d}_amt_cnt.joblib")
        joblib.dump(pipe, model_path)

        metrics.append({
            "HOUR": h,
            "MAE_AMT": mae_amt,
            "MAE_CNT": mae_cnt,
            "model_path": model_path,
            "model_type": "MLP(Deep)" if h == 1 else "XGB",
            "n_train": len(X_train),
            "n_test": len(X_test),
        })

    metrics_df = pd.DataFrame(metrics).sort_values("HOUR")
    metrics_df.to_csv(os.path.join(MODEL_DIR, "metrics_summary.csv"), index=False, encoding="utf-8-sig")

    # ✅ 너가 원한 출력 1) 유지
    print("metrics_summary.csv 저장 완료", flush=True)

    return metrics_df


# =========================
# F) 예측 (날짜+동 입력 -> 시간대별 AMT/CNT)
# =========================
def load_models():
    models = {}
    for h in range(1, 11):
        p = os.path.join(MODEL_DIR, f"hour_{h:02d}_amt_cnt.joblib")
        if not os.path.exists(p):
            raise FileNotFoundError(f"모델 파일이 없습니다: {p} (먼저 train_and_save_models() 실행)")
        models[h] = joblib.load(p)
    return models


def predict_day(yyyymmdd: str, dong: str, save_csv: bool = True) -> pd.DataFrame:
    feat_df = get_hourly_weather_features(SERVICE_KEY, dong, yyyymmdd)
    models = load_models()

    outs = []
    for h in range(1, 11):
        row = feat_df[feat_df["HOUR"] == h][["DONG", "DAY", "TEMP", "RAIN"]]
        pred_amt, pred_cnt = models[h].predict(row)[0]
        outs.append({
            "TA_YMD": yyyymmdd,
            "DONG": dong,
            "HOUR": h,
            "TEMP": float(feat_df.loc[feat_df["HOUR"] == h, "TEMP"].iloc[0]),
            "RAIN": float(feat_df.loc[feat_df["HOUR"] == h, "RAIN"].iloc[0]),
            "PRED_AMT": int(round(pred_amt)),
            "PRED_CNT": int(round(pred_cnt)),
        })

    pred_df = pd.DataFrame(outs)

    if save_csv:
        filename = f"pred_{dong}_{yyyymmdd}.csv"
        saved_path = os.path.join(OUTPUT_DIR, filename)
        pred_df.to_csv(saved_path, index=False, encoding="utf-8-sig")

        print(f"예측 결과 저장: {filename}", flush=True)
        # 만약 "전체 경로"도 보고싶으면 아래로 교체:
        # print(f"예측 결과 저장: {saved_path}", flush=True)

    return pred_df


# =========================
# G) 실행 예시 (원하는 3개만 출력)
# =========================
if __name__ == "__main__":
    _ = train_and_save_models()

    if not SERVICE_KEY:
        raise ValueError("환경변수 RAIN_ID(SERVICE_KEY)가 비었습니다. 예측을 하려면 .env를 확인하세요.")

    yyyymmdd = "20251230"
    dong = "매교동"
    pred_df = predict_day(yyyymmdd, dong, save_csv=True)

    print(pred_df, flush=True)
