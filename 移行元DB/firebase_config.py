# firebase_config.py

from google.cloud import firestore
from flask_login import UserMixin
import os
from dotenv import load_dotenv

load_dotenv()

fs_db = None
try:
    fs_db = firestore.Client()
except Exception as e:
    print(f"Firestore Client Error: {e}")

class FirestoreUser(UserMixin):
    def __init__(self, user_data):
        self.id = user_data.get('username')
        self.username = user_data.get('username')
        self.display_name = user_data.get('display_name')
        self.email = user_data.get('email')
        self.password_hash = user_data.get('password_hash')
        self.family_id = user_data.get('family_id')

    # --- ここを追加：テンプレートの current_user.family.name に対応させる ---
    @property
    def family(self):
        """
        テンプレート側で current_user.family.name と呼ばれた時に、
        Firestoreから家族情報を取得して返す。
        """
        if not self.family_id or not fs_db:
            return type('Obj', (object,), {'name': '家族未設定'})
        
        try:
            # Firestoreの families コレクションから family_id で検索
            doc = fs_db.collection('families').document(self.family_id).get()
            if doc.exists:
                # .name でアクセスできるように、辞書を簡易オブジェクト化して返す
                data = doc.to_dict()
                return type('Obj', (object,), {'name': data.get('name', '名称未設定')})
        except Exception as e:
            print(f"Error fetching family data: {e}")
            
        return type('Obj', (object,), {'name': '取得失敗'})