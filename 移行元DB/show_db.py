import sqlite3

def check_db_details():
    # データベースに接続
    db_path = 'threads_dashboard.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. テーブル一覧を取得
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()

    print(f"\n===== DB: {db_path} の構造確認 =====")
    
    for table in tables:
        table_name = table[0]
        # 件数確認
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        
        # カラム名（列名）の確認
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [col[1] for col in cursor.fetchall()]
        
        print(f"\n[Table: {table_name}] (件数: {count}件)")
        print(f"  Columns: {', '.join(columns)}")

    conn.close()

if __name__ == '__main__':
    check_db_details()