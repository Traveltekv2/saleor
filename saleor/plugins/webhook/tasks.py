import json
import logging
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type
from urllib.parse import urlparse, urlunparse

import boto3
import requests
from botocore.exceptions import ClientError
from celery.exceptions import MaxRetriesExceededError
from celery.utils.log import get_task_logger
from google.cloud import pubsub_v1
from requests.exceptions import RequestException

from ...celeryconf import app
from ...core import EventDeliveryStatus
from ...core.models import EventDelivery, EventPayload
from ...core.tracing import webhooks_opentracing_trace
from ...graphql.webhook.subscription_payload import (
    generate_payload_from_subscription,
    initialize_context,
)
from ...payment import PaymentError
from ...settings import WEBHOOK_SYNC_TIMEOUT, WEBHOOK_TIMEOUT
from ...site.models import Site
from ...webhook.event_types import SUBSCRIBABLE_EVENTS
from ...webhook.utils import get_webhooks_for_event
from . import signature_for_payload
from .utils import (
    attempt_update,
    catch_duration_time,
    clear_successful_delivery,
    create_attempt,
    create_event_delivery_list_for_webhooks,
    delivery_update,
)

if TYPE_CHECKING:
    from ...app.models import App

logger = logging.getLogger(__name__)
task_logger = get_task_logger(__name__)


class WebhookSchemes(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    AWS_SQS = "awssqs"
    GOOGLE_CLOUD_PUBSUB = "gcpubsub"


@dataclass
class WebhookResponse:
    content: str
    request_headers: Optional[Dict] = None
    response_headers: Optional[Dict] = None
    response_status_code: Optional[int] = None
    status: str = EventDeliveryStatus.SUCCESS
    duration: float = 0.0


def create_deliveries_for_subscriptions(
    event_type, subscribable_object, webhooks, requestor=None
) -> List[EventDelivery]:
    """Create webhook payload based on subscription query.

    It uses a defined subscription query, defined for webhook to explicitly determine
    what fields should be included in the payload.

    :param event_type: event type which should be triggered.
    :param subscribable_object: subscribable object to process via subscription query.
    :param requestor: used in subscription webhooks to generate meta data for payload.
    :return: List of event deliveries to send via webhook tasks.
    """
    if event_type not in SUBSCRIBABLE_EVENTS:
        logger.info(
            "Skipping subscription webhook. Event %s is not subscribable.", event_type
        )
        return []

    context = initialize_context(requestor)

    event_payloads = []
    event_deliveries = []
    for webhook in webhooks:
        data = generate_payload_from_subscription(
            event_type=event_type,
            subscribable_object=subscribable_object,
            subscription_query=webhook.subscription_query,
            context=context,
            app=webhook.app,
        )
        if not data:
            logger.warning(
                "No payload was generated with subscription for event: %s" % event_type
            )
            continue

        event_payload = EventPayload(payload=json.dumps({**data}))
        event_payloads.append(event_payload)
        event_deliveries.append(
            EventDelivery(
                status=EventDeliveryStatus.PENDING,
                event_type=event_type,
                payload=event_payload,
                webhook=webhook,
            )
        )

    EventPayload.objects.bulk_create(event_payloads)
    return EventDelivery.objects.bulk_create(event_deliveries)


def trigger_webhooks_async(
    data, event_type, webhooks, subscribable_object=None, requestor=None
):
    """Trigger async webhooks - both regular and subscription.

    :param data: used as payload in regular webhooks.
    :param event_type: used in both webhook types as event type.
    :param webhooks: used in both webhook types, queryset of async webhooks.
    :param subscribable_object: subscribable object used in subscription webhooks.
    :param requestor: used in subscription webhooks to generate meta data for payload.
    """
    regular_webhooks, subscription_webhooks = group_webhooks_by_subscription(webhooks)
    deliveries = []

    if regular_webhooks:
        payload = EventPayload.objects.create(payload=data)
        deliveries.extend(
            create_event_delivery_list_for_webhooks(
                webhooks=webhooks,
                event_payload=payload,
                event_type=event_type,
            )
        )
    if subscription_webhooks:
        deliveries.extend(
            create_deliveries_for_subscriptions(
                event_type=event_type,
                subscribable_object=subscribable_object,
                webhooks=subscription_webhooks,
                requestor=requestor,
            )
        )

    for delivery in deliveries:
        send_webhook_request_async.delay(delivery.id)


def group_webhooks_by_subscription(webhooks):
    subscription = [webhook for webhook in webhooks if webhook.subscription_query]
    regular = [webhook for webhook in webhooks if not webhook.subscription_query]

    return regular, subscription


def trigger_webhook_sync(
    event_type: str, data: str, app: "App", timeout=None
) -> Optional[Dict[Any, Any]]:
    """Send a synchronous webhook request."""
    webhooks = get_webhooks_for_event(event_type, app.webhooks.all())
    webhook = webhooks.first()
    if not webhook:
        raise PaymentError(f"No payment webhook found for event: {event_type}.")
    event_payload = EventPayload.objects.create(payload=data)
    delivery = EventDelivery.objects.create(
        status=EventDeliveryStatus.PENDING,
        event_type=event_type,
        payload=event_payload,
        webhook=webhook,
    )
    kwargs = {}
    if timeout:
        kwargs = {"timeout": timeout}
    return send_webhook_request_sync(app.name, delivery, **kwargs)


def send_webhook_using_http(
    target_url, message, domain, signature, event_type, timeout=WEBHOOK_TIMEOUT
):
    """Send a webhook request using http / https protocol.

    :param target_url: Target URL request will be sent to.
    :param message: Payload that will be used.
    :param domain: Current site domain.
    :param signature: Webhook secret key checksum.
    :param event_type: Webhook event type.
    :param timeout: Request timeout.

    :return: WebhookResponse object.
    """
    headers = {
        "Content-Type": "application/json",
        # X- headers will be deprecated in Saleor 4.0, proper headers are without X-
        "X-Saleor-Event": event_type,
        "X-Saleor-Domain": domain,
        "X-Saleor-Signature": signature,
        "Saleor-Event": event_type,
        "Saleor-Domain": domain,
        "Saleor-Signature": signature,
    }

    response = requests.post(target_url, data=message, headers=headers, timeout=timeout)
    return WebhookResponse(
        content=response.text,
        request_headers=headers,
        response_headers=dict(response.headers),
        response_status_code=response.status_code,
        duration=response.elapsed.total_seconds(),
        status=(
            EventDeliveryStatus.SUCCESS if response.ok else EventDeliveryStatus.FAILED
        ),
    )


def send_webhook_using_aws_sqs(target_url, message, domain, signature, event_type):
    parts = urlparse(target_url)
    region = "us-east-1"
    hostname_parts = parts.hostname.split(".")
    if len(hostname_parts) == 4 and hostname_parts[0] == "sqs":
        region = hostname_parts[1]
    client = boto3.client(
        "sqs",
        region_name=region,
        aws_access_key_id=parts.username,
        aws_secret_access_key=parts.password,
    )
    queue_url = urlunparse(
        ("https", parts.hostname, parts.path, parts.params, parts.query, parts.fragment)
    )
    is_fifo = parts.path.endswith(".fifo")

    msg_attributes = {
        "SaleorDomain": {"DataType": "String", "StringValue": domain},
        "EventType": {"DataType": "String", "StringValue": event_type},
    }
    if signature:
        msg_attributes["Signature"] = {"DataType": "String", "StringValue": signature}

    message_kwargs = {
        "QueueUrl": queue_url,
        "MessageAttributes": msg_attributes,
        "MessageBody": message.decode("utf-8"),
    }
    if is_fifo:
        message_kwargs["MessageGroupId"] = domain
    with catch_duration_time() as duration:
        response = client.send_message(**message_kwargs)
        return WebhookResponse(content=response, duration=duration())


def send_webhook_using_google_cloud_pubsub(
    target_url, message, domain, signature, event_type
):
    parts = urlparse(target_url)
    client = pubsub_v1.PublisherClient()
    topic_name = parts.path[1:]  # drop the leading slash
    with catch_duration_time() as duration:
        future = client.publish(
            topic_name,
            message,
            saleorDomain=domain,
            eventType=event_type,
            signature=signature,
        )
        response_duration = duration()
        response = future.result()
        return WebhookResponse(content=response, duration=response_duration)


def send_webhook_using_scheme_method(
    target_url, domain, secret, event_type, data
) -> WebhookResponse:
    parts = urlparse(target_url)
    message = data.encode("utf-8")
    signature = signature_for_payload(message, secret)
    scheme_matrix: Dict[
        WebhookSchemes, Tuple[Callable, Tuple[Type[Exception], ...]]
    ] = {
        WebhookSchemes.HTTP: (send_webhook_using_http, (RequestException,)),
        WebhookSchemes.HTTPS: (send_webhook_using_http, (RequestException,)),
        WebhookSchemes.AWS_SQS: (send_webhook_using_aws_sqs, (ClientError,)),
        WebhookSchemes.GOOGLE_CLOUD_PUBSUB: (
            send_webhook_using_google_cloud_pubsub,
            (pubsub_v1.publisher.exceptions.MessageTooLargeError, RuntimeError),
        ),
    }
    if method := scheme_matrix.get(parts.scheme.lower()):
        send_method, send_exception = method
        try:
            return send_method(
                target_url,
                message,
                domain,
                signature,
                event_type,
            )
        except send_exception as e:
            return WebhookResponse(content=str(e), status=EventDeliveryStatus.FAILED)
    raise ValueError("Unknown webhook scheme: %r" % (parts.scheme,))


@app.task(
    bind=True,
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
)
def send_webhook_request_async(self, event_delivery_id):
    try:
        delivery = EventDelivery.objects.select_related("payload", "webhook__app").get(
            id=event_delivery_id
        )
    except EventDelivery.DoesNotExist:
        logger.error("Event delivery id: %r not found", event_delivery_id)
        return

    if not delivery.webhook.is_active:
        delivery_update(delivery=delivery, status=EventDeliveryStatus.FAILED)
        logger.info("Event delivery id: %r webhook is disabled.", event_delivery_id)
        return

    webhook = delivery.webhook
    data = delivery.payload.payload
    domain = Site.objects.get_current().domain
    attempt = create_attempt(delivery, self.request.id)
    delivery_status = EventDeliveryStatus.SUCCESS
    try:
        with webhooks_opentracing_trace(
            delivery.event_type, domain, app_name=webhook.app.name
        ):
            response = send_webhook_using_scheme_method(
                webhook.target_url,
                domain,
                webhook.secret_key,
                delivery.event_type,
                data,
            )
        attempt_update(attempt, response)
        if response.status == EventDeliveryStatus.FAILED:
            task_logger.info(
                "[Webhook ID: %r] Failed request to %r: %r for event: %r."
                " Delivery attempt id: %r",
                webhook.id,
                webhook.target_url,
                response.content,
                delivery.event_type,
                attempt.id,
            )
            try:
                countdown = self.retry_backoff * (2**self.request.retries)
                self.retry(countdown=countdown, **self.retry_kwargs)
            except MaxRetriesExceededError:
                task_logger.warning(
                    "[Webhook ID: %r] Failed request to %r: exceeded retry limit."
                    "Delivery id: %r",
                    webhook.id,
                    webhook.target_url,
                    delivery.id,
                )
                delivery_status = EventDeliveryStatus.FAILED
        elif response.status == EventDeliveryStatus.SUCCESS:
            task_logger.info(
                "[Webhook ID:%r] Payload sent to %r for event %r. Delivery id: %r",
                webhook.id,
                webhook.target_url,
                delivery.event_type,
                delivery.id,
            )
        delivery_update(delivery, delivery_status)
    except ValueError as e:
        response = WebhookResponse(content=str(e), status=EventDeliveryStatus.FAILED)
        attempt_update(attempt, response)
        delivery_update(delivery=delivery, status=EventDeliveryStatus.FAILED)
    clear_successful_delivery(delivery)


def send_webhook_request_sync(
    app_name, delivery, timeout=WEBHOOK_SYNC_TIMEOUT
) -> Optional[Dict[Any, Any]]:
    event_payload = delivery.payload
    data = event_payload.payload
    webhook = delivery.webhook
    parts = urlparse(webhook.target_url)
    domain = Site.objects.get_current().domain
    message = data.encode("utf-8")
    signature = signature_for_payload(message, webhook.secret_key)

    if parts.scheme.lower() not in [WebhookSchemes.HTTP, WebhookSchemes.HTTPS]:
        delivery_update(delivery, EventDeliveryStatus.FAILED)
        raise ValueError("Unknown webhook scheme: %r" % (parts.scheme,))

    logger.debug(
        "[Webhook] Sending payload to %r for event %r.",
        webhook.target_url,
        delivery.event_type,
    )
    attempt = create_attempt(delivery=delivery, task_id=None)
    response = WebhookResponse(content="")
    response_data = None

    try:
        with webhooks_opentracing_trace(
            delivery.event_type, domain, sync=True, app_name=app_name
        ):
            response = send_webhook_using_http(
                webhook.target_url,
                message,
                domain,
                signature,
                delivery.event_type,
                timeout=timeout,
            )
            response_data = json.loads(response.content)
    except RequestException as e:
        logger.warning(
            "[Webhook] Failed request to %r: %r. "
            "ID of failed DeliveryAttempt: %r . ",
            webhook.target_url,
            e,
            attempt.id,
        )
        response.status = EventDeliveryStatus.FAILED
        if e.response:
            response.content = e.response.text
            response.response_headers = dict(e.response.headers)
            response.response_status_code = e.response.status_code

    except JSONDecodeError as e:
        logger.warning(
            "[Webhook] Failed parsing JSON response from %r: %r."
            "ID of failed DeliveryAttempt: %r . ",
            webhook.target_url,
            e,
            attempt.id,
        )
        response.status = EventDeliveryStatus.FAILED
    else:
        if response.status == EventDeliveryStatus.SUCCESS:
            logger.debug(
                "[Webhook] Success response from %r."
                "Successful DeliveryAttempt id: %r",
                webhook.target_url,
                attempt.id,
            )

    attempt_update(attempt, response)
    delivery_update(delivery, response.status)
    clear_successful_delivery(delivery)

    return response_data if response.status == EventDeliveryStatus.SUCCESS else None


# DEPRECATED
# to be removed in task: #1q2x7xw
@app.task(compression="zlib")
def trigger_webhooks_for_event(event_type, data):
    """Send a webhook request for an event as an async task."""
    webhooks = get_webhooks_for_event(event_type)
    for webhook in webhooks:
        send_webhook_request.delay(
            webhook.app.name,
            webhook.pk,
            webhook.target_url,
            webhook.secret_key,
            event_type,
            data,
        )


# to be removed in task: #1q2x7xw
@app.task(
    bind=True,
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    compression="zlib",
)
def send_webhook_request(
    self, app_name, webhook_id, target_url, secret, event_type, data
):
    parts = urlparse(target_url)
    domain = Site.objects.get_current().domain
    message = data.encode("utf-8")
    signature = signature_for_payload(message, secret)

    scheme_matrix = {
        WebhookSchemes.HTTP: (send_webhook_using_http, RequestException),
        WebhookSchemes.HTTPS: (send_webhook_using_http, RequestException),
        WebhookSchemes.AWS_SQS: (send_webhook_using_aws_sqs, ClientError),
        WebhookSchemes.GOOGLE_CLOUD_PUBSUB: (
            send_webhook_using_google_cloud_pubsub,
            pubsub_v1.publisher.exceptions.MessageTooLargeError,
        ),
    }

    if methods := scheme_matrix.get(parts.scheme.lower()):
        send_method, send_exception = methods
        try:
            with webhooks_opentracing_trace(event_type, domain, app_name=app_name):
                send_method(target_url, message, domain, signature, event_type)
        except send_exception as e:
            task_logger.info("[Webhook] Failed request to %r: %r.", target_url, e)
            try:
                countdown = self.retry_backoff * (2**self.request.retries)
                self.retry(countdown=countdown, **self.retry_kwargs)
            except MaxRetriesExceededError:
                task_logger.warning(
                    "[Webhook] Failed request to %r: exceeded retry limit.",
                    target_url,
                )
        task_logger.info(
            "[Webhook ID:%r] Payload sent to %r for event %r",
            webhook_id,
            target_url,
            event_type,
        )
    else:
        raise ValueError("Unknown webhook scheme: %r" % (parts.scheme,))
