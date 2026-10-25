FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py invoice.py clover.py ./
COPY fonts ./fonts
COPY static ./static
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# The host sets PORT (Render: 10000); shell form so it is read at start.
CMD gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 8 --timeout 60 main:app
