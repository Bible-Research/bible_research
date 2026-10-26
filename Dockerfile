FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is runtime-only; dev tools live in
# requirements-dev.txt and are intentionally not installed here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# collectstatic needs a minimal .env (build-time only)
RUN printf 'SECRET_KEY=build\nDEBUG=False\nDATABASE_URL=sqlite:///:memory:\n' > .env \
    && python manage.py collectstatic --noinput \
    && rm .env

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", \
     "--threads", "4", "bible_research.wsgi:application"]
