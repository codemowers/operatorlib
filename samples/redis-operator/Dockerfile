FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
   kubernetes-asyncio \
   redis \
   passlib

FROM python:alpine
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/redis-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT /app/redis_operator.py run
ADD operatorlib.py /app/
ADD samples/redis-operator/redis_operator.py /app/
