FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir fastapi==0.142.4 uvicorn==0.54.0 pydantic==2.13.5 httpx==0.28.1 huggingface_hub numpy
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PYTHONUNBUFFERED=1
WORKDIR /home/user/app
COPY --chown=user . .
CMD ["bash", "start.sh"]
