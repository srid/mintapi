FROM python:3-slim

ADD . /app
WORKDIR /app
RUN python setup.py install
