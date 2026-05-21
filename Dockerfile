FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

RUN pip install --no-cache-dir flask

COPY mavat_watch.py ui.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "ui.py"]
