from celery import shared_task


@shared_task(bind=True, max_retries=0, acks_late=True)
def ping(self):
    return "pong"
