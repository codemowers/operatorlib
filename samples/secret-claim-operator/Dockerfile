FROM python:alpine AS build
RUN pip3 wheel --wheel-dir=/wheels \
   kubernetes-asyncio \
   passlib

FROM python:alpine
COPY --from=build /wheels /wheels
RUN pip3 install --no-index /wheels/*.whl && rm -Rfv /wheels
LABEL name="codemowers/secret-claim-operator" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT /app/secret_claim_operator.py run
ADD operatorlib.py /app/
ADD samples/secret-claim-operator/secret_claim_operator.py /app/
