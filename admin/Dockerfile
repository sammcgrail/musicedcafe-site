FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MEC_ROOT=/app PORT=8081
# pillow-heif: HEIC photos from the Sell Your Records form (optional; without it HEIC originals are kept as-is)
RUN pip install --no-cache-dir "pillow>=11" "pillow-heif>=0.18"
WORKDIR /app
# code is bind-mounted read-only at /app (data/ and public/ are writable mounts)
EXPOSE 8081
CMD ["python3", "-I", "/app/admin/server.py"]
