FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# ffmpeg：影片抽影格用（多模態分析）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py run_local.py ./

CMD ["python", "run_local.py"]
