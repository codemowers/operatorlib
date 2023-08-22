FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
   kubernetes-asyncio \
   passlib \
   httpx-auth \
   httpx
FROM python:alpine
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/wildduck-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT /app/wildduck_operator.py run
ADD operatorlib.py /app/
ADD samples/wildduck-operator/wildduck_operator.py /app/
