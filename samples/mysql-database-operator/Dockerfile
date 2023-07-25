FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
    kubernetes-asyncio \
    aiomysql \
    passlib

FROM python:alpine
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/mysql-database-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT /app/mysql_database_operator.py run
ADD operatorlib.py /app/
ADD samples/mysql-database-operator/mysql_database_operator.py /app/
