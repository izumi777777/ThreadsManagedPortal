import os
import time
import datetime
import requests
import traceback
import boto3
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from openai import AzureOpenAI
from dotenv import load_dotenv

# firebase_config から Firestore インスタンスをインポート
from firebase_config import fs_db

# .envファイルの読み込み
load_dotenv()

BUCKET_NAME = os.environ.get('S3_BUCKET_NAME') 
S3_REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')

# S3クライアントの初期化（App Runner の Instance Role を自動使用）
s3_client = boto3.client('s3', region_name=S3_REGION)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ------------------------------------------------------------------ #
# データベースヘルパー（Firestore用）
# ------------------------------------------------------------------ #

def get_settings():
    """設定情報を取得（ドキュメントID 'default' 固定）"""
    doc = fs_db.collection('threads_settings').document('default').get()
    return doc.to_dict() if doc.exists else {}

def get_post_ref(post_id):
    """特定の投稿ドキュメントへの参照を取得"""
    return fs_db.collection('threads_posts').document(str(post_id))

def post_to_dict(doc):
    """Firestoreドキュメントをテンプレート/API用辞書に変換"""
    data = doc.to_dict()
    data['id'] = doc.id
    # フロントエンドが期待するキー名に合わせる
    data['imageUrl'] = data.get('image_url', '')
    data['scheduledAt'] = data.get('scheduled_at', '')
    return data

# ------------------------------------------------------------------ #
# Threads API 統計情報の更新処理
# ------------------------------------------------------------------ #

def update_post_insights(post_id):
    """特定の投稿のインサイトを更新"""
    post_ref = get_post_ref(post_id)
    post_doc = post_ref.get()
    settings = get_settings()

    if not post_doc.exists or not settings.get('token'):
        return

    post = post_doc.to_dict()
    if not post.get('thread_id'):
        return

    try:
        metrics = "views,likes,replies,reposts,quotes"
        url = f"https://graph.threads.net/v1.0/{post['thread_id']}/insights?metric={metrics}&access_token={settings['token']}"
        
        res = requests.get(url)
        data = res.json()
        
        if "data" in data:
            updates = {}
            for item in data["data"]:
                name = item.get("name")
                value = item.get("values", [{}])[0].get("value", 0)
                updates[name] = value
            post_ref.update(updates)
    except Exception as e:
        print(f"Insight update error (Post {post_id}): {e}")

def update_all_insights():
    """公開済みの全投稿のインサイトを一括更新"""
    # 最近30日以内に公開された投稿を対象
    recent_date = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()
    docs = fs_db.collection('threads_posts') \
                 .where('status', '==', 'Published') \
                 .where('created_at', '>=', recent_date) \
                 .stream()
    
    for doc in docs:
        update_post_insights(doc.id)
        time.sleep(1) # レート制限対策

# ------------------------------------------------------------------ #
# Threads API 投稿実行処理
# ------------------------------------------------------------------ #

def execute_threads_post(post_id):
    """Threadsへの投稿を実行"""
    post_ref = get_post_ref(post_id)
    post_doc = post_ref.get()
    settings = get_settings()
    
    if not post_doc.exists: return
    post = post_doc.to_dict()

    if post.get('status') == 'Published': return
    
    if not settings.get('token') or not settings.get('user_id'):
        post_ref.update({'status': 'Failed', 'error': 'API設定が未完了です'})
        return

    try:
        post_ref.update({'status': 'Publishing'})
        
        base_url = "https://graph.threads.net/v1.0"
        access_token = settings['token']
        
        raw_urls = post.get('image_url', '').split(',') if post.get('image_url') else []
        final_image_urls = []
        for url in raw_urls:
            u = url.strip()
            if u.startswith('/') and settings.get('base_url'):
                u = f"{settings['base_url'].rstrip('/')}{u}"
            if u: final_image_urls.append(u)

        creation_id = None
        if len(final_image_urls) > 1:
            # カルーセル投稿
            child_ids = []
            for img in final_image_urls:
                res = requests.post(f"{base_url}/{settings['user_id']}/threads", data={
                    "access_token": access_token, "image_url": img,
                    "media_type": "IMAGE", "is_carousel_item": "true"
                })
                res_data = res.json()
                if "id" not in res_data: raise Exception(f"子コンテナ作成失敗: {res_data}")
                child_ids.append(res_data["id"])
            
            res = requests.post(f"{base_url}/{settings['user_id']}/threads", data={
                "access_token": access_token, "media_type": "CAROUSEL",
                "children": ",".join(child_ids), "text": post.get('text', '')
            })
            creation_id = res.json().get("id")
        else:
            # 単一投稿
            payload = {
                "access_token": access_token, "text": post.get('text', ''),
                "media_type": "IMAGE" if final_image_urls else "TEXT"
            }
            if final_image_urls: payload["image_url"] = final_image_urls[0]
            res = requests.post(f"{base_url}/{settings['user_id']}/threads", data=payload)
            creation_id = res.json().get("id")

        if not creation_id: raise Exception(f"コンテナ作成失敗: {res.json()}")

        time.sleep(5)
        publish_res = requests.post(f"{base_url}/{settings['user_id']}/threads_publish", data={
            "access_token": access_token, "creation_id": creation_id
        })
        pub_data = publish_res.json()
        
        if "id" not in pub_data: raise Exception(f"公開失敗: {pub_data}")

        post_ref.update({
            'thread_id': pub_data["id"],
            'status': 'Published',
            'error': "",
            'created_at': datetime.datetime.now().isoformat()
        })
    except Exception as e:
        post_ref.update({'status': 'Failed', 'error': str(e)})

# ------------------------------------------------------------------ #
# スケジューラー
# ------------------------------------------------------------------ #

scheduler = BackgroundScheduler()
scheduler.start()

def check_scheduled_posts():
    """予約投稿のチェック（重要：複合インデックスが必要）"""
    now_iso = datetime.datetime.now().isoformat()
    # 状態が Pending かつ 予定時刻を過ぎたものをクエリ
    docs = fs_db.collection('threads_posts') \
                 .where('status', '==', 'Pending') \
                 .where('scheduled_at', '<=', now_iso) \
                 .stream()
    for doc in docs:
        execute_threads_post(doc.id)

# 10秒おきにスケジュールチェック
scheduler.add_job(func=check_scheduled_posts, trigger="interval", seconds=10)
# 1時間おきにインサイト更新
scheduler.add_job(func=update_all_insights, trigger="interval", hours=1)

# ------------------------------------------------------------------ #
# Azure OpenAI ヘルパー
# ------------------------------------------------------------------ #

def get_azure_client():
    api_key = os.environ.get('AZURE_OPENAI_API_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    api_version = os.environ.get('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')

    if not api_key: return None, "APIキー未設定"
    try:
        client = AzureOpenAI(api_version=api_version, azure_endpoint=endpoint, api_key=api_key)
        return client, None
    except Exception as e: return None, str(e)

# ------------------------------------------------------------------ #
# API エンドポイント
# ------------------------------------------------------------------ #

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        data = request.json
        fs_db.collection('threads_settings').document('default').set({
            'token': data.get('token', ''),
            'user_id': data.get('userId', ''),
            'base_url': data.get('baseUrl', ''),
            'pict_space_url': data.get('pictSpaceUrl', ''),
            'pixiv_url': data.get('pixivUrl', '')
        })
        return jsonify({"success": True})
    
    s = get_settings()
    return jsonify({
        "token": s.get('token', ''), "userId": s.get('user_id', ''),
        "baseUrl": s.get('base_url', ''), "pictSpaceUrl": s.get('pict_space_url', ''),
        "pixivUrl": s.get('pixiv_url', '')
    })

@app.route('/api/posts', methods=['GET', 'POST'])
def api_posts():
    if request.method == 'POST':
        data = request.json
        # scheduledAt は ISOフォーマット文字列のまま保存
        new_post = {
            "text": data['text'],
            "image_url": data.get('imageUrl', ''),
            "scheduled_at": data['scheduledAt'],
            "status": 'Pending',
            "error": "",
            "created_at": datetime.datetime.now().isoformat(),
            "views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0
        }
        _, doc_ref = fs_db.collection('threads_posts').add(new_post)
        if data.get('postNow'):
            execute_threads_post(doc_ref.id)
        return jsonify({"id": doc_ref.id, **new_post})
    
    # 全投稿を日付順に取得
    docs = fs_db.collection('threads_posts').order_by('scheduled_at', direction='DESCENDING').stream()
    return jsonify([post_to_dict(doc) for doc in docs])

@app.route('/api/posts/<post_id>/execute', methods=['POST'])
def force_execute_post(post_id):
    execute_threads_post(post_id)
    doc = get_post_ref(post_id).get()
    return jsonify(post_to_dict(doc))

@app.route('/api/insights/update', methods=['POST'])
def api_update_insights():
    try:
        update_all_insights()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ai/generate', methods=['POST'])
def api_ai_generate():
    data = request.json
    client, err = get_azure_client()
    if err: return jsonify({"error": err}), 400
    try:
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": data.get('prompt')}],
            max_tokens=1000,
            model=os.environ.get('AZURE_OPENAI_DEPLOYMENT')
        )
        return jsonify({"text": response.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ai/chat', methods=['POST'])
def api_ai_chat():
    data = request.json
    client, err = get_azure_client()
    if err: return jsonify({"error": err}), 400
    try:
        response = client.chat.completions.create(
            messages=data.get('messages', []),
            max_tokens=1500,
            model=os.environ.get('AZURE_OPENAI_DEPLOYMENT')
        )
        return jsonify({"reply": response.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file"}), 400
    
    # ファイル名の安全なクレンジングと重複防止
    filename = secure_filename(file.filename)
    new_filename = f"{int(time.time())}_{filename}"
    
    try:
        # S3へアップロード
        s3_client.upload_fileobj(
            file,
            BUCKET_NAME,
            new_filename,
            ExtraArgs={
                # 前の手順でバケットポリシー（PublicRead）を設定済みなら
                # この ACL 指定はなくても公開されますが、明示しておくと安心です
                'ContentType': file.content_type
            }
        )
        
        # 公開URLの生成
        s3_url = f"https://{BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/{new_filename}"
        
        return jsonify({"url": s3_url, "filename": new_filename})

    except Exception as e:
        # エラーログを出力しておくとデバッグが捗ります
        print(f"S3 Upload Error: {e}")
        return jsonify({"error": str(e)}), 500
    

@app.route('/api/fetch_user', methods=['POST'])
def fetch_user():
    token = request.json.get('token')
    url = f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={token}"
    res = requests.get(url)
    return jsonify(res.json())

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    # Firestore版では db.create_all() は不要
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)