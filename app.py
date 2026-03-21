import os
import time
import datetime
import requests
import traceback
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from openai import AzureOpenAI
from dotenv import load_dotenv

# .envファイルの読み込み (APIキーなどの環境変数)
load_dotenv()

# --- Flask & Database 設定 ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///threads_dashboard.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'

# 画像のアップロード先フォルダを自動作成
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# --- データベースモデル ---
class AppSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(500), default="")
    user_id = db.Column(db.String(100), default="")
    base_url = db.Column(db.String(500), default="")
    pict_space_url = db.Column(db.String(500), default="") # AI用: PictSpaceのURL
    pixiv_url = db.Column(db.String(500), default="")      # AI用: PixivのURL

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.String(100), default="") # Threads側での投稿ID
    text = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(500), default="") # カンマ区切りのURL
    scheduled_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='Pending')
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)
    
    # 統計情報（インサイト）
    views = db.Column(db.Integer, default=0)   # インプレッション
    likes = db.Column(db.Integer, default=0)   # いいね
    replies = db.Column(db.Integer, default=0) # 返信
    reposts = db.Column(db.Integer, default=0) # 再投稿
    quotes = db.Column(db.Integer, default=0)  # 引用

    def to_dict(self):
        return {
            "id": self.id,
            "text": self.text,
            "imageUrl": self.image_url,
            "scheduledAt": self.scheduled_at.strftime('%Y-%m-%dT%H:%M'),
            "status": self.status,
            "error": self.error,
            "views": self.views,
            "likes": self.likes,
            "replies": self.replies,
            "reposts": self.reposts,
            "quotes": self.quotes
        }

# --- Threads API 統計情報の更新処理 ---
def update_post_insights(post_id):
    """特定の投稿のインサイトをThreads APIから取得して更新する"""
    with app.app_context():
        post = Post.query.get(post_id)
        settings = AppSettings.query.first()
        if not post or not post.thread_id or not settings or not settings.token:
            return

        try:
            # メトリクスを指定して取得
            metrics = "views,likes,replies,reposts,quotes"
            url = f"https://graph.threads.net/v1.0/{post.thread_id}/insights?metric={metrics}&access_token={settings.token}"
            
            res = requests.get(url)
            data = res.json()
            
            if "data" in data:
                for item in data["data"]:
                    name = item.get("name")
                    value = item.get("values", [{}])[0].get("value", 0)
                    if name == "views": post.views = value
                    elif name == "likes": post.likes = value
                    elif name == "replies": post.replies = value
                    elif name == "reposts": post.reposts = value
                    elif name == "quotes": post.quotes = value
                
                db.session.commit()
        except Exception as e:
            print(f"Insight update error (Post {post_id}): {e}")

def update_all_insights():
    """公開済みの全投稿のインサイトを一括更新する"""
    with app.app_context():
        # 最近30日以内に公開された投稿を対象にする
        recent_date = datetime.datetime.now() - datetime.timedelta(days=30)
        published_posts = Post.query.filter(
            Post.status == 'Published', 
            Post.thread_id != "",
            Post.created_at >= recent_date
        ).all()
        
        for p in published_posts:
            update_post_insights(p.id)
            time.sleep(1) # APIレート制限対策

# --- Threads API 投稿実行処理 ---
def execute_threads_post(post_id):
    with app.app_context():
        post = Post.query.get(post_id)
        settings = AppSettings.query.first()
        
        if not post or post.status == 'Published':
            return
        if not settings or not settings.token or not settings.user_id:
            post.status = 'Failed'
            post.error = 'API設定が未完了です'
            db.session.commit()
            return

        try:
            post.status = 'Publishing'
            db.session.commit()

            base_url = "https://graph.threads.net/v1.0"
            access_token = settings.token
            
            # 画像URLのリストを作成
            raw_urls = post.image_url.split(',') if post.image_url else []
            final_image_urls = []
            for url in raw_urls:
                u = url.strip()
                if u.startswith('/'):
                    if not settings.base_url:
                        raise Exception("ローカル画像には公開ベースURLの設定が必要です")
                    u = f"{settings.base_url.rstrip('/')}{u}"
                if u:
                    final_image_urls.append(u)

            creation_id = None

            # --- 投稿ロジック ---
            if len(final_image_urls) > 1:
                # カルーセル投稿
                child_ids = []
                for img in final_image_urls:
                    res = requests.post(f"{base_url}/{settings.user_id}/threads", data={
                        "access_token": access_token,
                        "image_url": img,
                        "media_type": "IMAGE",
                        "is_carousel_item": "true"
                    })
                    res_data = res.json()
                    if "id" not in res_data:
                        raise Exception(f"子コンテナ作成失敗: {res_data}")
                    child_ids.append(res_data["id"])
                
                res = requests.post(f"{base_url}/{settings.user_id}/threads", data={
                    "access_token": access_token,
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "text": post.text
                })
                creation_id = res.json().get("id")
            else:
                # 1枚投稿 または テキストのみ
                payload = {
                    "access_token": access_token,
                    "text": post.text,
                    "media_type": "IMAGE" if final_image_urls else "TEXT"
                }
                if final_image_urls:
                    payload["image_url"] = final_image_urls[0]

                res = requests.post(f"{base_url}/{settings.user_id}/threads", data=payload)
                creation_id = res.json().get("id")

            if not creation_id:
                raise Exception(f"コンテナ作成に失敗しました: {res.json()}")

            # 公開処理待ち
            time.sleep(5)
            
            publish_res = requests.post(f"{base_url}/{settings.user_id}/threads_publish", data={
                "access_token": access_token,
                "creation_id": creation_id
            })
            pub_data = publish_res.json()
            
            if "id" not in pub_data:
                raise Exception(f"公開失敗: {pub_data}")

            # 成功時に投稿IDを保存
            post.thread_id = pub_data["id"]
            post.status = 'Published'
            post.error = ""
            
        except Exception as e:
            post.status = 'Failed'
            post.error = str(e)
            
        db.session.commit()

# --- スケジューラー ---
scheduler = BackgroundScheduler()
scheduler.start()

def check_scheduled_posts():
    with app.app_context():
        now = datetime.datetime.now()
        pending_posts = Post.query.filter(Post.status == 'Pending', Post.scheduled_at <= now).all()
        for p in pending_posts:
            execute_threads_post(p.id)

# 10秒おきにスケジュール投稿をチェック
scheduler.add_job(func=check_scheduled_posts, trigger="interval", seconds=10)
# 1時間おきにインサイト（統計情報）を自動更新
scheduler.add_job(func=update_all_insights, trigger="interval", hours=1)

# --- Azure OpenAI ---
def get_azure_client():
    api_key = os.environ.get('AZURE_OPENAI_API_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    api_version = os.environ.get('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')

    if not api_key:
        return None, "APIキーが設定されていません。"

    try:
        client = AzureOpenAI(api_version=api_version, azure_endpoint=endpoint, api_key=api_key)
        return client, None
    except Exception as e:
        return None, str(e)

# --- API エンドポイント ---
@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    settings = AppSettings.query.first()
    if not settings:
        settings = AppSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST':
        data = request.json
        settings.token = data.get('token', '')
        settings.user_id = data.get('userId', '')
        settings.base_url = data.get('baseUrl', '')
        settings.pict_space_url = data.get('pictSpaceUrl', '')
        settings.pixiv_url = data.get('pixivUrl', '')
        db.session.commit()
        return jsonify({"success": True})
    
    return jsonify({
        "token": settings.token, 
        "userId": settings.user_id,
        "baseUrl": settings.base_url,
        "pictSpaceUrl": settings.pict_space_url,
        "pixivUrl": settings.pixiv_url
    })

@app.route('/api/posts', methods=['GET', 'POST'])
def api_posts():
    if request.method == 'POST':
        data = request.json
        dt = datetime.datetime.strptime(data['scheduledAt'], '%Y-%m-%dT%H:%M')
        new_post = Post(
            text=data['text'],
            image_url=data.get('imageUrl', ''),
            scheduled_at=dt
        )
        db.session.add(new_post)
        db.session.commit()
        if data.get('postNow'):
            execute_threads_post(new_post.id)
        return jsonify(new_post.to_dict())
    
    posts = Post.query.order_by(Post.scheduled_at.desc()).all()
    return jsonify([p.to_dict() for p in posts])

@app.route('/api/posts/<int:post_id>/execute', methods=['POST'])
def force_execute_post(post_id):
    execute_threads_post(post_id)
    post = Post.query.get(post_id)
    return jsonify(post.to_dict())

@app.route('/api/insights/update', methods=['POST'])
def api_update_insights():
    """インサイトを手動で一括更新するエンドポイント"""
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
    if not file: return jsonify({"error": "No file"}), 400
    filename = secure_filename(file.filename)
    name, ext = os.path.splitext(filename)
    new_filename = f"{name}_{int(time.time())}{ext}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
    return jsonify({"url": f"/static/uploads/{new_filename}"})

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
    with app.app_context():
        db.create_all()
    # PORT環境変数があればそれを使い、なければ5000を使う
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
