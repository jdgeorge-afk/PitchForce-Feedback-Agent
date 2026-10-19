# PitchForce Feedback – one image, two roles (web and worker), selected by the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-postgres.txt ./
RUN pip install --no-cache-dir -r requirements-postgres.txt
COPY . .
ENV FFMPEG_PATH=/usr/bin/ffmpeg
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["python", "-m", "pitchforce", "web", "--host", "0.0.0.0", "--port", "8000"]
