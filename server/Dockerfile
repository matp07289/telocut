# TeloCut アセットサーバー（+ レンダーノード）を Docker で動かす（Docker を使わない場合は server/setup_linux.sh）
#   ビルド: docker build -t telocut-server -f server/Dockerfile .
#   起動  : docker compose -f server/docker-compose.yml up -d
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements-server.txt /app/
RUN pip install --no-cache-dir -r requirements-server.txt

# サーバーとレンダリングに必要なものだけ入れる（GUI は使わない）
COPY core /app/core
COPY editor /app/editor
COPY telocut.py /app/

ENV TELOCUT_HOME=/data/home \
    QT_QPA_PLATFORM=offscreen \
    PYTHONUNBUFFERED=1

EXPOSE 8770
VOLUME ["/data", "/media"]

# 既定はアセットサーバー。レンダーノードにする場合は command を上書き（docker-compose.yml 参照）
CMD ["python", "-m", "editor.assetserver", "--root", "/media", "--data", "/data", "--port", "8770"]
