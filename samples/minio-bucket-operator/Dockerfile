FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
   kubernetes-asyncio \
   passlib \
   pyjwt \
   httpx-auth \
   httpx
FROM minio/mc as minio
FROM python:alpine
COPY --from=minio /usr/bin/mc /usr/bin/mc
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/minio-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT python3 /app/minio_bucket_operator.py run
ADD operatorlib.py /app/
ADD samples/minio-bucket-operator/minio-owner.json /app/
ADD samples/minio-bucket-operator/minio_bucket_operator.py /app/
