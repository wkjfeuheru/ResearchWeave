FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep bash git && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pydantic==2.13.5 httpx==0.28.1 pypdf==6.19.0 python-docx==1.2.0 openpyxl==3.1.5
RUN useradd -m -s /bin/bash ohuser
USER ohuser
