FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY translator_bot ./translator_bot
COPY pyproject.toml README.md ./

ENTRYPOINT ["python", "-m", "translator_bot"]
CMD ["run"]
