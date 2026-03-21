import sqlite3
from firebase_config import fs_db
from datetime import datetime

def migrate():
    conn = sqlite3.connect('threads_dashboard.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. 設定情報の移行
    cursor.execute("SELECT * FROM app_settings")
    settings = cursor.fetchone()
    if settings:
        data = dict(settings)
        # ドキュメントIDを "default" 固定にして管理しやすくする
        fs_db.collection('threads_settings').document('default').set(data)
        print("✅ AppSettings の移行完了")

    # 2. 投稿データの移行
    cursor.execute("SELECT * FROM post")
    posts = cursor.fetchall()
    batch = fs_db.batch()
    
    for p in posts:
        data = dict(p)
        doc_id = str(data['id'])
        # Firestoreで扱いやすいよう日付を文字列(ISO)に変換
        # SQLiteの型に合わせて調整
        doc_ref = fs_db.collection('threads_posts').document(doc_id)
        batch.set(doc_ref, data)

    batch.commit()
    print(f"✅ {len(posts)} 件の投稿データを移行完了")
    conn.close()

if __name__ == '__main__':
    migrate()