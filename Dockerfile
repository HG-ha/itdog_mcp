FROM python:3.11-slim

WORKDIR /app

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g; \
            s|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
            /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    fonts-noto-cjk \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple \
    && pip config set install.trusted-host mirrors.aliyun.com \
    && pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN pip install --no-cache-dir playwright \
    && playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_CHROMIUM_NO_SANDBOX=1
ENV PLAYWRIGHT_FORCE_SYSTEM_FONTS=1

EXPOSE 8080

CMD ["python", "main.py"]
