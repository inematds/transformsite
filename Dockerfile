FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -m -u 1000 bot

# O projeto (transformsite.yaml, services/, tools/, kb/, data/) é montado em /project
WORKDIR /project
USER bot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)"
CMD ["transformsite", "serve", "--host", "0.0.0.0", "--port", "8000"]
