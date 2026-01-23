FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/unigraco

COPY ["reference document/UniGraCo", "/opt/unigraco"]
COPY ["textile-device-monitor/unigraco-web", "/opt/unigraco_web"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install -r /opt/unigraco_web/requirements.txt

ENV PYTHONPATH=/opt/unigraco

EXPOSE 7860

CMD ["python", "/opt/unigraco_web/app.py"]
