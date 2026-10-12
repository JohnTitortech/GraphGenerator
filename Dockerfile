FROM python:3.11-slim

# Jalan sebagai user non-root (aman, dan kompatibel dengan Hugging Face)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Render mengisi variabel PORT otomatis; default 7860 untuk Hugging Face/lokal
ENV PORT=7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
