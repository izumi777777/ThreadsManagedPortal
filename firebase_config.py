import os
import json
from flask_login import UserMixin

fs_db = None

try:
    from google.cloud import firestore
    from google.oauth2 import service_account

    credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON', '')

    if credentials_json:
        # App Runner: 環境変数から JSON 文字列で認証
        credentials_info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=['https://www.googleapis.com/auth/cloud-platform'],
        )
        fs_db = firestore.Client(
            project=credentials_info.get('project_id'),
            credentials=credentials,
        )
        print("[firebase] Connected via FIREBASE_CREDENTIALS_JSON env var.")

    else:
        # ローカル・EC2: GOOGLE_APPLICATION_CREDENTIALS のキーファイルで認証
        fs_db = firestore.Client()
        print("[firebase] Connected via GOOGLE_APPLICATION_CREDENTIALS.")

except Exception as e:
    print(f"[firebase] Firestore connection error: {e}")
    fs_db = None


class FirestoreUser(UserMixin):
    def __init__(self, user_data: dict):
        self.id            = user_data.get('id') or user_data.get('username')
        self.username      = user_data.get('username', '')
        self.display_name  = user_data.get('display_name', '')
        self.email         = user_data.get('email', '')
        self.password_hash = user_data.get('password_hash', '')
        self.family_id     = user_data.get('family_id')

        family_name = user_data.get('family_name', 'おうち家計簿')
        self.family = type('Family', (), {
            'name':    family_name,
            'profile': user_data.get('family_profile'),
        })()