# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
ARG PROFILE=monitoring
WORKDIR /build
COPY scripts/ops_wheelhouse.py scripts/ops_wheelhouse.py
COPY backend/requirements.lock backend/requirements.lock
COPY ops/wheelhouse/bootstrap.lock ops/wheelhouse/bootstrap.lock
# PROFILE is passed as a quoted argument; the Python CLI checks its allowed values.
RUN python scripts/ops_wheelhouse.py build --profile "$PROFILE" --output /wheelhouse

FROM scratch AS artifact
COPY --from=build /wheelhouse/ /
