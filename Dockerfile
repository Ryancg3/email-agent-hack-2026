FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/app.db \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# collectstatic needs these set, but doesn't use them.
RUN DJANGO_SECRET_KEY=build TOKEN_KEY=build python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["bash", "deploy/start.sh"]
