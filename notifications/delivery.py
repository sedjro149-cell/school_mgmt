# notifications/delivery.py
import logging
from typing import Dict, List, Tuple

from django.core.mail import send_mail, BadHeaderError
from django.utils import timezone

from .models import Notification, NotificationAttempt, Channel, UserDevice

logger = logging.getLogger(__name__)

# --- helpers for each channel (production replaceable) ---


def _send_inapp(notification: Notification) -> Tuple[bool, str]:
    """
    In-app is considered always successful because the Notification row is stored in DB.
    We still create a NotificationAttempt to track it.
    """
    return True, "stored-in-db"


def _send_email(notification: Notification, title: str, body: str) -> Tuple[bool, str]:
    """
    Uses django.core.mail.send_mail. Returns (success, response_message).
    In dev without email backend, this may raise; we catch exceptions upstream.
    """
    user = notification.recipient_user
    to = getattr(user, "email", None)
    if not to:
        return False, "no-email"
    try:
        # from_email can be set to a setting if you prefer
        send_mail(subject=title or "", message=body or "", from_email="no-reply@school.local", recipient_list=[to], fail_silently=False)
        return True, "sent"
    except BadHeaderError as e:
        logger.exception("BadHeaderError sending email for notif %s: %s", notification.id, e)
        return False, f"bad-header: {e}"
    except Exception as e:
        logger.exception("Exception sending email for notif %s: %s", notification.id, e)
        return False, str(e)


def _send_sms(notification: Notification, body: str) -> Tuple[bool, str]:
    """
    Placeholder SMS sender. Replace with integration to your SMS provider.
    Tries common phone attributes on the user.
    """
    user = notification.recipient_user
    phone = getattr(user, "phone", None) or getattr(user, "phone_number", None) or getattr(user, "mobile", None)
    if not phone:
        return False, "no-phone"
    try:
        # TODO: integrate real SMS sender here
        # sms_send(phone, body)
        return True, "sms-sent-placeholder"
    except Exception as e:
        logger.exception("Exception sending SMS for notif %s to %s: %s", notification.id, phone, e)
        return False, str(e)


def _send_push(notification: Notification, title: str, body: str) -> Tuple[bool, str]:
    """
    Placeholder push sender. Iterates over UserDevice tokens.
    Replace with FCM/APNs/OneSignal or your provider.
    """
    user = notification.recipient_user
    devices_qs = getattr(user, "devices", None)
    devices = list(devices_qs.all()) if devices_qs is not None else []

    if not devices:
        return False, "no-devices"

    try:
        # For each device you might call provider API. Here we just simulate success if devices exist.
        sent_count = 0
        for dev in devices:
            # TODO: push_send(dev.provider, dev.token, title, body)
            # simulate per-device success
            sent_count += 1
        return True, f"push-sent-{sent_count}"
    except Exception as e:
        logger.exception("Exception sending push for notif %s: %s", notification.id, e)
        return False, str(e)


# --- main pipeline ---


def _normalize_channel(ch) -> str:
    """
    Accept either Channel enum members or raw strings.
    Normalize to lowercase strings: 'inapp', 'email', 'sms', 'push'
    """
    if ch is None:
        return ""
    if isinstance(ch, Channel):
        return str(ch.value).lower()
    # If it's stored as 'inapp' or 'INAPP' or Channel.INAPP.name
    return str(ch).lower()


def send_notification(notification: Notification) -> Dict[str, bool]:
    """
    Harmonized delivery pipeline.
    - For each configured channel, attempt delivery and create a NotificationAttempt.
    - If at least one channel succeeds, mark the Notification as sent.
    - Returns a dict mapping channel -> bool (success).
    """
    results: Dict[str, bool] = {}
    any_success = False

    title = ""
    body = ""
    try:
        title = notification.render_title() if hasattr(notification, "render_title") else ""
    except Exception as e:
        logger.exception("Error rendering title for notification %s: %s", getattr(notification, "id", None), e)
        title = ""

    try:
        body = notification.render_body() if hasattr(notification, "render_body") else ""
    except Exception as e:
        logger.exception("Error rendering body for notification %s: %s", getattr(notification, "id", None), e)
        body = ""

    # determine channels (fallback to template default channels or inapp)
    raw_channels = notification.channels
    if not raw_channels and getattr(notification, "template", None):
        raw_channels = getattr(notification.template, "default_channels", []) or []
    if not raw_channels:
        raw_channels = ["inapp"]

    for raw_ch in raw_channels:
        ch = _normalize_channel(raw_ch)
        try:
            success = False
            response = ""

            if ch == _normalize_channel(Channel.INAPP):
                success, response = _send_inapp(notification)
                # In-app attempt still recorded below
            elif ch == _normalize_channel(Channel.EMAIL):
                success, response = _send_email(notification, title, body)
            elif ch == _normalize_channel(Channel.SMS):
                success, response = _send_sms(notification, body)
            elif ch == _normalize_channel(Channel.PUSH):
                success, response = _send_push(notification, title, body)
            else:
                # unknown channel string: try to match verbose names
                if raw_ch and str(raw_ch).lower() in ("inapp", "email", "sms", "push"):
                    # already normalized above; this branch reachable only for odd types
                    success = False
                    response = f"unhandled-channel:{raw_ch}"
                else:
                    success = False
                    response = f"unknown-channel:{raw_ch}"

            # create NotificationAttempt record (store the original channel representation for clarity)
            try:
                NotificationAttempt.objects.create(
                    notification=notification,
                    channel=raw_ch if not isinstance(raw_ch, Channel) else raw_ch.value,
                    success=bool(success),
                    response=str(response)
                )
            except Exception as e:
                logger.exception("Failed to create NotificationAttempt for notif %s channel %s: %s", getattr(notification, "id", None), raw_ch, e)

            results[str(raw_ch)] = bool(success)
            if success:
                any_success = True

        except Exception as e:
            # unexpected per-channel failure: log and record a failed attempt
            logger.exception("Unexpected error delivering notification %s via %s: %s", getattr(notification, "id", None), raw_ch, e)
            try:
                NotificationAttempt.objects.create(
                    notification=notification,
                    channel=raw_ch if not isinstance(raw_ch, Channel) else raw_ch.value,
                    success=False,
                    response=str(e)
                )
            except Exception:
                logger.exception("Also failed to create NotificationAttempt after exception for notif %s", getattr(notification, "id", None))
            results[str(raw_ch)] = False

    # finalize notification status
    try:
        if any_success:
            notification.mark_sent(sent_at=timezone.now())
        else:
            notification.error = "all channels failed"
            notification.save(update_fields=["error"])
    except Exception as e:
        logger.exception("Failed to update notification.sent/error for notif %s: %s", getattr(notification, "id", None), e)

    return results
