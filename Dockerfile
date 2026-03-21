# 軽量な Python イメージを使用
FROM python:3.12-slim

# 作業ディレクトリの設定
WORKDIR /app

# 依存関係のインストール（キャッシュを効かせるため先に COPY）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# 環境変数の設定
# ※ GOOGLE_APPLICATION_CREDENTIALS はデプロイ環境（App Runner）の変数で上書きされます
ENV PORT=8080
ENV FLASK_ENV=production

# ポートの公開
EXPOSE 8080

# Gunicorn で起動
# 予約投稿（APScheduler）の重複実行を避けるため、ワーカー数は 1 を推奨します
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]