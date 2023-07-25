FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
    kubernetes-asyncio \
    aiopg \
    passlib

FROM python:alpine
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/postgres-database-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT /app/postgres_database_operator.py run
ADD operatorlib.py /app/
ADD samples/postgres-database-operator/postgres_database_operator.py /app/
