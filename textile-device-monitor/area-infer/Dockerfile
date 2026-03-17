FROM python:3.11-slim

WORKDIR /app

ARG TORCH_VARIANT=cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY requirements.torch.cpu.txt ./
COPY requirements.torch.gpu.txt ./

RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$TORCH_VARIANT" = "gpu" ]; then \
         pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 -r requirements.torch.gpu.txt; \
       else \
         pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu -r requirements.torch.cpu.txt; \
       fi

COPY app ./app
COPY vendor ./vendor

ENV PYTHONPATH=/app:/app/vendor/yolact

EXPOSE 9001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001"]
