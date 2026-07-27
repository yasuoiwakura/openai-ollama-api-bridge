FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py translator.py ./

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; exit(0 if urllib.request.urlopen('http://localhost:8080/health').status == 200 else 1)"

EXPOSE 8080
CMD ["python", "proxy.py"]
