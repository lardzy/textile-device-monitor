FROM python:3.12-slim

WORKDIR /opt

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libglib2.0-0 \
    libgl1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth=1 https://github.com/zai-org/GLM-OCR.git /opt/GLM-OCR

WORKDIR /opt/GLM-OCR

RUN pip install --no-cache-dir -e ".[all]"

COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh \
    && find /opt/GLM-OCR -type f -name '*.sh' -exec sed -i 's/\r$//' {} + \
    && chmod +x /entrypoint.sh

EXPOSE 5002

CMD ["/entrypoint.sh"]
