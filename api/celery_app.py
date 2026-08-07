import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

app = Celery("api")
app.conf.broker_url = REDIS_URL
app.conf.task_serializer = "json"
app.conf.result_backend = None
app.conf.broker_transport_options = {"socket_connect_timeout": 1}
