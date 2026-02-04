FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    postgresql-client \
    pandoc \
    curl \
    # OpenCV dependencies for PaddleOCR
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    # Additional image processing libraries
    libgthread-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set environment to skip model source check (faster startup)
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

# Upgrade pip first to ensure proper dependency resolution
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt .
# Install with verbose output and no cache to ensure fresh install
RUN pip install --no-cache-dir -v -r requirements.txt

# Verify PaddleOCR installation with detailed info
RUN python -c "import paddleocr; print(f'PaddleOCR version: {paddleocr.__version__}')" && \
    python -c "from paddleocr import PaddleOCRVL; print('PaddleOCR-VL imported successfully')" && \
    python -c "from paddleocr._pipelines.paddleocr_vl import PaddleOCRVL; import inspect; sig = inspect.signature(PaddleOCRVL.__init__); print(f'PaddleOCRVL parameters: {list(sig.parameters.keys())}')" || \
    echo "PaddleOCR installation check failed - check logs above"

# Create necessary directories
RUN mkdir -p uploads results

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "600"]
