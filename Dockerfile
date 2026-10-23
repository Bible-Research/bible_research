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

# collectstatic needs a minimal config.yaml (build-time only)
RUN printf 'SECRET_KEY: "build"\nDEBUG: false\n' > config.yaml \
    && printf 'DBT_KEY: ""\nESV_KEY: ""\n' >> config.yaml \
    && printf 'DATABASES:\n  default:\n' >> config.yaml \
    && printf '    ENGINE: django.db.backends.sqlite3\n' >> config.yaml \
    && printf '    NAME: ":memory:"\n' >> config.yaml \
    && python manage.py collectstatic --noinput \
    && rm config.yaml

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", \
     "--threads", "4", "bible_research.wsgi:application"]
