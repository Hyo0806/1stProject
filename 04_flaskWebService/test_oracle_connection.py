"""
선생님 Oracle DB 연결 테스트
"""
import oracledb

# Oracle Client 초기화 (필요시)
try:
    oracledb.init_oracle_client()
except:
    pass  # 이미 초기화되었거나 불필요

# 연결 테스트
try:
    conn = oracledb.connect(
        user="scott",
        password="tiger",
        host="210.121.189.12",
        port=1521,
        sid="xe"
    )
    
    print("✅ Oracle DB 연결 성공!")
    
    cursor = conn.cursor()
    
    # 테스트 쿼리
    cursor.execute("SELECT * FROM tab")
    print("\n📊 현재 사용 가능한 테이블:")
    for row in cursor:
        print(f"  - {row[0]}")
    
    cursor.close()
    conn.close()
    
    print("\n✅ 연결 테스트 완료!")
    
except Exception as e:
    print(f"❌ 연결 실패: {e}")
