"""
SMS-API:t, /api/sms/v1/ (dokumentationen för kunden: /kund/sms/dokumentation/).

    POST /api/sms/v1/messages/        skicka ett sms (eller provkör: dryrun)
    GET  /api/sms/v1/messages/<id>/   ett sms och dess status
    GET  /api/sms/v1/usage/           månadens förbrukning och tak
    GET  /api/sms/v1/senders/         kontots godkända avsändare

Inloggning med kundens nyckel i Authorization: Bearer adxsms_... Ingen
cookie och därför ingen CSRF (samma mönster som apps/projects/api.py).
Svaren är JSON; fel har formen {"error": {"code": ..., "message": ...}} med
stabila koder (service.HTTP_STATUS).

Leveransrapporterna från 46elks kommer till /api/sms/46elks/dlr/<id>/<sign>/,
en adress per sms med en HMAC som bara servern kan räkna fram
(service.dlr_signature). Fel signatur ger samma 404 som en okänd adress.
"""

import json
import logging
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from apps.common.net import client_ip

from . import pricing, ratelimit, service
from .models import SmsApiKey, SmsMessage

logger = logging.getLogger(__name__)

#: Ett anrop är ett sms: mer än så här är inte ett sms.
MAX_BODY_BYTES = 16 * 1024


def error_response(code, message="", retry_after=None, extra=None):
    payload = {"error": {"code": code, "message": message or service.ERROR_TEXTS.get(code, "")}}
    if extra:
        payload["error"].update(extra)
    response = JsonResponse(payload, status=service.HTTP_STATUS.get(code, 400))
    if retry_after:
        response["Retry-After"] = str(int(retry_after))
    return response


def _when(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def message_json(message):
    """Ett sms i API:t. Ett reserverat sms med oklart svar från 46elks har
    status "unknown" (felkod provider_unknown) tills läget är avgjort."""
    error = None
    if message.error_code:
        text = message.error
        if message.error_code in ("provider_error", "provider_unknown"):
            text = service.ERROR_TEXTS[message.error_code]
        error = {"code": message.error_code, "message": text}
    return {
        "id": message.pk,
        "status": "unknown" if message.is_unknown else message.status,
        "to": message.to,
        "country": message.country or None,
        "from": message.sender,
        "message": message.body,
        "parts": message.parts,
        "encoding": message.encoding,
        "price_sek": pricing.api_amount(message.customer_price),
        "price_is_estimate": message.is_estimate or message.needs_check,
        "reference": message.reference or None,
        "error": error,
        "test_mode": message.test_mode,
        "created_at": _when(message.created_at),
        "sent_at": _when(message.sent_at),
        "delivered_at": _when(message.delivered_at),
    }


def _bearer(request):
    header = request.headers.get("Authorization", "")
    if header[:7].lower() != "bearer ":
        return ""
    return header[7:].strip()


def api_endpoint(view):
    """Nyckeln -> request.sms_key och request.sms_account. Kontot ska vara
    aktiverat (och kunden aktiv), och sekundgränsen gäller alla anrop. Ett
    oväntat fel blir ett JSON-svar, aldrig en HTML-sida."""

    @csrf_exempt
    @never_cache
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        key = SmsApiKey.lookup(_bearer(request))
        if key is None:
            return error_response("invalid_key")
        account = key.account
        if not account.is_enabled or not account.customer.is_active:
            return error_response("sms_not_enabled")
        retry = ratelimit.check_burst(key)
        if retry:
            return error_response("rate_limited", retry_after=retry)
        key.touch()
        request.sms_key = key
        request.sms_account = account
        try:
            return view(request, *args, **kwargs)
        except Exception:  # noqa: BLE001 - kunden ska alltid få JSON
            logger.exception("SMS-API: oväntat fel (konto %s)", account.pk)
            return error_response("internal_error")

    return wrapper


def _json_body(request):
    if len(request.body or b"") > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@api_endpoint
def messages(request):
    if request.method != "POST":
        return error_response("method_not_allowed")
    data = _json_body(request)
    if data is None:
        return error_response("invalid_request", "Skicka ett JSON-objekt i kroppen.")
    retry = ratelimit.check_send(request.sms_key)
    if retry:
        return error_response("rate_limited", retry_after=retry)
    outcome = service.send(request.sms_key, data)
    if outcome.error:
        extra = {"id": outcome.message.pk} if outcome.message else None
        return error_response(outcome.error, outcome.detail, outcome.retry_after, extra)
    if outcome.estimate:
        return JsonResponse({"dryrun": True, **outcome.estimate})
    payload = message_json(outcome.message)
    if outcome.duplicate:
        payload["duplicate"] = True
    return JsonResponse(payload, status=outcome.http_status)


@api_endpoint
def message_detail(request, pk):
    if request.method != "GET":
        return error_response("method_not_allowed")
    message = SmsMessage.objects.filter(account=request.sms_account, pk=pk).first()
    if message is None:
        return error_response("not_found")
    return JsonResponse(message_json(message))


@api_endpoint
def usage(request):
    if request.method != "GET":
        return error_response("method_not_allowed")
    account = request.sms_account
    data = pricing.usage(account)
    return JsonResponse(
        {
            "period": f"{data['period']:%Y-%m}",
            "messages": data["sms"],
            "parts": data["parts"],
            "delivered": data["delivered"],
            "failed": data["failed"],
            "stopped": data["stopped"],
            "cost_sek": pricing.api_amount(data["cost"]),
            "test_cost_sek": pricing.api_amount(data["test_cost"]),
            "cap_sek": pricing.api_amount(data["cap"]),
            "remaining_sek": pricing.api_amount(data["remaining"]),
            "cap_reached": data["cap_reached"],
            "from": account.sender_name,
            "senders": account.senders,
            "allowed_countries": account.countries,
            "limits": {**ratelimit.limits(), "max_parts": service.MAX_PARTS},
        }
    )


@api_endpoint
def senders(request):
    """Kontots godkända avsändare: standarden (används när from utelämnas)
    och, om kunden får välja, de extra namnen ADX godkänt."""
    if request.method != "GET":
        return error_response("method_not_allowed")
    account = request.sms_account
    return JsonResponse(
        {
            "default": account.sender_name,
            "senders": account.senders,
            "choose_per_message": account.customer_sets_sender,
        }
    )


def _dlr_ip_allowed(request):
    allowed = [ip.strip() for ip in getattr(settings, "SMS_DLR_ALLOWED_IPS", []) if ip.strip()]
    return not allowed or client_ip(request) in allowed


@csrf_exempt
@never_cache
def dlr(request, pk, signature):
    """46elks leveransrapport: POST med id, status och delivered
    (application/x-www-form-urlencoded). Svarar 200 när rapporten är
    hanterad, så att 46elks slutar försöka; 409 när sms:et ännu inte fått
    sitt id, så att 46elks försöker igen."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    if not service.dlr_signature_ok(pk, signature) or not _dlr_ip_allowed(request):
        return JsonResponse({"ok": False}, status=404)
    data = request.POST
    if not data and request.body:
        parsed = _json_body(request)
        data = parsed if parsed is not None else {}
    status, text = service.apply_delivery_report(
        pk, data.get("id"), data.get("status"), data.get("delivered")
    )
    return JsonResponse({"ok": status == 200, "result": text}, status=status)
