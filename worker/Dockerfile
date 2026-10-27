FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY worker.py .

CMD ["python", "worker.py"]
