FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/film.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据库文件目录（由 volume 持久化）；启动时自动建库建表
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 依赖安装与数据库初始化均已纳入启动流程：
# uvicorn 触发 lifespan -> init_db() 幂等建表
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
