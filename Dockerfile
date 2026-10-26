FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
ENV TZ=Asia/Tokyo PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY tools tools
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
