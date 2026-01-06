"""
CSV 데이터를 Oracle DB로 임포트하는 스크립트
선생님 서버용 (210.121.189.12)
"""
import os
import pandas as pd
import oracledb
from dotenv import load_dotenv

load_dotenv()

# Oracle 설정 (선생님 서버)
ORACLE_HOST = os.getenv("ORACLE_HOST", "210.121.189.12")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SID = os.getenv("ORACLE_SID", "xe")
ORACLE_USER = os.getenv("ORACLE_USER", "scott")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "tiger")

# Oracle Client 초기화
try:
    oracledb.init_oracle_client()
except:
    pass

# CSV 파일 경로
CSV_PATH = "data/수원시 한식 동별 데이터백업.csv"

def create_table(conn):
    """테이블 생성 (Oracle 11g 호환)"""
    cursor = conn.cursor()
    
    # 기존 시퀀스 삭제
    try:
        cursor.execute("DROP SEQUENCE SALES_DATA_SEQ")
    except:
        pass
    
    # 기존 테이블 삭제 (주의!)
    try:
        cursor.execute("DROP TABLE SALES_DATA")
        print("✅ 기존 테이블 삭제")
    except:
        pass
    
    # 새 테이블 생성 (Oracle 11g 호환)
    create_sql = """
    CREATE TABLE SALES_DATA (
        ID NUMBER PRIMARY KEY,
        TA_YMD VARCHAR2(8) NOT NULL,
        DONG VARCHAR2(50) NOT NULL,
        HOUR NUMBER(2) NOT NULL,
        DAY NUMBER(1),
        AMT NUMBER(12, 2),
        CNT NUMBER(8),
        UNIT VARCHAR2(20),
        TEMP NUMBER(5, 2),
        RAIN NUMBER(6, 2)
    )
    """
    cursor.execute(create_sql)
    print("✅ 테이블 생성 완료")
    
    # 시퀀스 생성 (자동 증가 ID용)
    cursor.execute("""
        CREATE SEQUENCE SALES_DATA_SEQ
        START WITH 1
        INCREMENT BY 1
        NOCACHE
        NOCYCLE
    """)
    print("✅ 시퀀스 생성 완료")
    
    # 인덱스 생성 (성능 향상)
    cursor.execute("""
        CREATE INDEX IDX_SALES_YMD_DONG ON SALES_DATA(TA_YMD, DONG)
    """)
    cursor.execute("""
        CREATE INDEX IDX_SALES_YMD_DONG_HOUR ON SALES_DATA(TA_YMD, DONG, HOUR)
    """)
    print("✅ 인덱스 생성 완료")
    
    conn.commit()
    cursor.close()

def import_csv_to_oracle(csv_path):
    """CSV 데이터를 Oracle DB로 임포트"""
    
    print(f"\n{'='*60}")
    print(f"📂 CSV → Oracle DB 임포트 시작")
    print(f"{'='*60}\n")
    
    # CSV 읽기
    print(f"📖 CSV 파일 읽는 중: {csv_path}")
    
    # 인코딩 시도
    for encoding in ['utf-8-sig', 'cp949', 'utf-8']:
        try:
            df = pd.read_csv(csv_path, encoding=encoding)
            print(f"✅ CSV 로딩 성공 ({encoding}): {len(df):,} 행")
            break
        except:
            continue
    
    # Oracle 연결
    print(f"\n🔌 Oracle DB 연결 중...")
    conn = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        host=ORACLE_HOST,
        port=ORACLE_PORT,
        sid=ORACLE_SID
    )
    print(f"✅ Oracle 연결 성공")
    
    # 테이블 생성
    print(f"\n📊 테이블 생성 중...")
    create_table(conn)
    
    # 데이터 삽입
    print(f"\n⏳ 데이터 삽입 중... (시간이 좀 걸립니다)")
    cursor = conn.cursor()
    
    # 시퀀스를 사용한 INSERT
    insert_sql = """
        INSERT INTO SALES_DATA 
        (ID, TA_YMD, DONG, HOUR, DAY, AMT, CNT, UNIT, TEMP, RAIN)
        VALUES (SALES_DATA_SEQ.NEXTVAL, :1, :2, :3, :4, :5, :6, :7, :8, :9)
    """
    
    # 배치 삽입 준비
    batch_data = []
    batch_size = 1000
    
    for idx, row in df.iterrows():
        # 동 이름 정규화
        dong = str(row.get('DONG', '')).strip().replace(" ", "")
        if '동' in dong:
            import re
            m = re.findall(r"([가-힣0-9]+동)", dong)
            dong = m[-1] if m else dong
        
        batch_data.append((
            str(row.get('TA_YMD', '')).replace("-", "").strip(),
            dong,
            int(row.get('HOUR', 0)),
            int(row.get('DAY', 0)) if pd.notna(row.get('DAY')) else None,
            float(row.get('AMT', 0)) if pd.notna(row.get('AMT')) else None,
            int(row.get('CNT', 0)) if pd.notna(row.get('CNT')) else None,
            str(row.get('UNIT', '')) if pd.notna(row.get('UNIT')) else None,
            float(row.get('TEMP', 0)) if pd.notna(row.get('TEMP')) else None,
            float(row.get('RAIN', 0)) if pd.notna(row.get('RAIN')) else None,
        ))
        
        # 배치 실행
        if len(batch_data) >= batch_size:
            cursor.executemany(insert_sql, batch_data)
            conn.commit()
            print(f"  ✓ {idx+1:,} / {len(df):,} 행 삽입 완료")
            batch_data = []
    
    # 남은 데이터 삽입
    if batch_data:
        cursor.executemany(insert_sql, batch_data)
        conn.commit()
        print(f"  ✓ {len(df):,} / {len(df):,} 행 삽입 완료")
    
    cursor.close()
    
    # 통계 확인
    print(f"\n📊 임포트 완료! 통계:")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM SALES_DATA")
    total_rows = cursor.fetchone()[0]
    print(f"  - 총 행 수: {total_rows:,}")
    
    cursor.execute("SELECT COUNT(DISTINCT TA_YMD) FROM SALES_DATA")
    unique_dates = cursor.fetchone()[0]
    print(f"  - 유니크 날짜: {unique_dates:,}")
    
    cursor.execute("SELECT COUNT(DISTINCT DONG) FROM SALES_DATA")
    unique_dongs = cursor.fetchone()[0]
    print(f"  - 유니크 동: {unique_dongs}")
    
    cursor.close()
    conn.close()
    
    print(f"\n{'='*60}")
    print(f"✅ 임포트 완료!")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    import_csv_to_oracle(CSV_PATH)
