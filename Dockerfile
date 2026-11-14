# Portable image for any container host (Render, Railway, Fly.io, Hugging Face Spaces, Cloud Run)
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000 HTTPS_ONLY=0
EXPOSE 8000
CMD ["sh", "-c", "uvicorn leave_tracker.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
