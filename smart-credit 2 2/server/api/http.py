"""JSON HTTP API + static file server (stdlib http.server)."""
import json
import mimetypes
import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..services.smart_credit import ServiceError, SmartCreditService
from ..store.db import seed

DEMO_OFFICER_CODE = "DEMO-OFFICER-2026"  # demo-only shared code; NOT real authentication


class App:
    """Holds the service plus static/reference locations."""

    def __init__(self, service: SmartCreditService, web_root: str, reference_dir: str):
        self.service = service
        self.web_root = web_root
        self.reference_dir = reference_dir
        self.reset_lock = threading.Lock()

    def reset(self) -> None:
        """Re-seed the demo database in place, under the DB lock, so concurrent
        requests never see a half-deleted file."""
        with self.reset_lock:
            svc = self.service
            with svc.db.lock:
                svc.db.wipe()
                seed(svc.db, svc.fixtures)


Route = Tuple[str, "re.Pattern[str]", Callable[..., Any]]


def build_routes(app: App) -> List[Route]:
    def svc() -> SmartCreditService:
        return app.service

    def customer(h: "Handler") -> str:
        cid = h.headers.get("X-Demo-Customer")
        if not cid:
            raise ServiceError(401, "DEMO_CUSTOMER_REQUIRED", "Select a demo customer first (X-Demo-Customer header).")
        return cid

    def officer(h: "Handler") -> str:
        if h.headers.get("X-Demo-Officer-Code") != DEMO_OFFICER_CODE:
            raise ServiceError(401, "DEMO_OFFICER_REQUIRED",
                               "Officer endpoints need the demo access code. Demo-only control, not real authentication.")
        return h.headers.get("X-Demo-Officer") or "demo.officer"

    def body_int(b: Dict[str, Any], key: str, required: bool = False) -> Optional[int]:
        v = b.get(key)
        if v is None:
            if required:
                raise ServiceError(422, "FIELD_REQUIRED", "'%s' is required." % key)
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ServiceError(422, "INVALID_FIELD", "'%s' must be an integer." % key)
        return v

    def reference(_h, _b, _m):
        path = os.path.join(app.reference_dir, "dataset_audit.json")
        if not os.path.exists(path):
            raise ServiceError(404, "NOT_GENERATED", "Run scripts/dataset_audit.py to generate dataset baselines.")
        with open(path) as f:
            data = json.load(f)
        data.pop("columns", None)
        return 200, data

    def reset(h, _b, _m):
        officer(h)
        app.reset()
        return 200, {"reset": True, "note": "Demo database re-seeded from fixtures."}

    R: List[Tuple[str, str, Callable[..., Any]]] = [
        ("GET", r"/api/health", lambda h, b, m: (200, {"ok": True, "simulated": True})),
        ("GET", r"/api/config/policy", lambda h, b, m: (200, svc().policy)),
        ("GET", r"/api/reference/dataset", reference),
        ("GET", r"/api/demo/customers", lambda h, b, m: (200, svc().list_customers())),
        ("GET", r"/api/demo/customers/(?P<cid>[\w-]+)", lambda h, b, m: (200, svc().get_customer(m["cid"]))),
        ("GET", r"/api/demo/qr-codes", lambda h, b, m: (200, svc().list_qr_codes())),
        ("GET", r"/api/demo/scenarios", lambda h, b, m: (200, svc().fixtures["scenarios"])),
        ("POST", r"/api/demo/reset", reset),
        ("POST", r"/api/demo/controls", lambda h, b, m: (200, svc().set_demo_control(
            str(b.get("key")), str(b.get("value")), officer(h)))),

        ("GET", r"/api/demo/clock", lambda h, b, m: (200, {"business_date": svc().as_of.isoformat(),
                                                          "demo_date": svc().demo_date.isoformat()})),
        ("POST", r"/api/demo/clock", lambda h, b, m: (200, svc().set_business_date(
            "demo-clock", b.get("date"), b.get("advance_days")))),
        ("POST", r"/api/demo/transactions/(?P<tx>[\w-]+)/expire-now", lambda h, b, m: (200, _expire(svc(), m["tx"], h))),

        ("POST", r"/api/qr/resolve", lambda h, b, m: _created(svc().resolve_qr(customer(h), b.get("payload")))),
        ("GET", r"/api/credit", lambda h, b, m: (200, svc().credit_summary(customer(h)))),
        ("GET", r"/api/transactions", lambda h, b, m: (200, svc().history(customer(h)))),
        ("GET", r"/api/transactions/(?P<tx>[\w-]+)", lambda h, b, m: (200, svc().get_view(m["tx"], customer(h)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/terms-and-conditions", lambda h, b, m: (200, svc().accept_terms_and_conditions(
            m["tx"], customer(h), b.get("accepted")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/payment-method", lambda h, b, m: (200, svc().select_payment_method(
            m["tx"], customer(h), b.get("method"), body_int(b, "tenure_months")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/consent/withdraw",
         lambda h, b, m: (200, svc().withdraw_consent(m["tx"], customer(h)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/pay", lambda h, b, m: (200, svc().pay(
            m["tx"], customer(h), h.headers.get("Idempotency-Key") or b.get("idempotency_key")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/cancel", lambda h, b, m: (200, svc().cancel(m["tx"], customer(h)))),
        ("GET", r"/api/transactions/(?P<tx>[\w-]+)/audit", lambda h, b, m: (200, _own_audit(svc(), m["tx"], customer(h)))),

        ("POST", r"/api/applications/(?P<app>[\w-]+)/assess", lambda h, b, m: (200, svc().assess(m["app"], customer(h)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/documents", lambda h, b, m: (200, svc().upload_application_document(
            m["app"], customer(h), b))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/information", lambda h, b, m: (200, svc().submit_information(
            m["app"], customer(h), b.get("fields") or {}))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/third-party-consent", lambda h, b, m: (200, svc().third_party_consent(
            m["app"], customer(h), b.get("granted")))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/manual-review", lambda h, b, m: (200, svc().request_manual_review(
            m["app"], customer(h)))),
        ("GET", r"/api/applications/(?P<app>[\w-]+)/terms", lambda h, b, m: (200, svc().preview_terms(
            m["app"], customer(h), _int_q(h, "tenure", 6)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/terms/confirm", lambda h, b, m: (200, svc().confirm_terms(
            m["app"], customer(h), body_int(b, "tenure_months", True), b.get("accepted")))),

        ("GET", r"/api/loans", lambda h, b, m: (200, svc().list_loans(customer(h)))),
        ("GET", r"/api/loans/(?P<loan>[\w-]+)", lambda h, b, m: (200, svc().get_loan(m["loan"], customer(h)))),
        ("POST", r"/api/loans/(?P<loan>[\w-]+)/repay", lambda h, b, m: (200, svc().repay(
            m["loan"], customer(h), b.get("instalments") or [], h.headers.get("Idempotency-Key") or b.get("idempotency_key")))),
        ("GET", r"/api/notifications", lambda h, b, m: (200, svc().notifications(customer(h)))),
        ("POST", r"/api/notifications/read", lambda h, b, m: (200, svc().mark_notifications_read(customer(h)))),

        ("GET", r"/api/limit-requests", lambda h, b, m: (200, svc().list_limit_requests(customer(h)))),
        ("POST", r"/api/limit-requests", lambda h, b, m: (201, svc().create_limit_request(
            customer(h), b.get("requested_limit_vnd"), b.get("reason") or "", b.get("transaction_id")))),
        ("GET", r"/api/limit-requests/(?P<req>[\w-]+)", lambda h, b, m: (200, svc().get_limit_request(m["req"], customer(h)))),
        ("POST", r"/api/limit-requests/(?P<req>[\w-]+)/documents", lambda h, b, m: (200, svc().upload_limit_document(
            m["req"], customer(h), b))),
        ("POST", r"/api/limit-requests/(?P<req>[\w-]+)/submit", lambda h, b, m: (200, svc().submit_limit_request(
            m["req"], customer(h)))),
        ("POST", r"/api/limit-requests/(?P<req>[\w-]+)/cancel", lambda h, b, m: (200, svc().cancel_limit_request(
            m["req"], customer(h)))),

        ("GET", r"/api/officer/cases", lambda h, b, m: (200, _with_officer(officer(h), svc().officer_cases()))),
        ("GET", r"/api/officer/cases/(?P<app>[\w-]+)", lambda h, b, m: (200, _with_officer(officer(h), svc().officer_case(m["app"])))),
        ("POST", r"/api/officer/cases/(?P<app>[\w-]+)/decision", lambda h, b, m: (200, svc().officer_decide(
            m["app"], officer(h), b.get("action"), b.get("note"), body_int(b, "expected_version"),
            b.get("requested_documents")))),
        ("GET", r"/api/officer/limit-requests", lambda h, b, m: (200, _with_officer(officer(h), svc().officer_limit_requests()))),
        ("GET", r"/api/officer/limit-requests/(?P<req>[\w-]+)", lambda h, b, m: (200, _with_officer(
            officer(h), svc().officer_limit_request(m["req"])))),
        ("POST", r"/api/officer/limit-requests/(?P<req>[\w-]+)/decision", lambda h, b, m: (200, svc().officer_limit_decide(
            m["req"], officer(h), b.get("action"), b.get("note"), b.get("expected_version"), b.get("approved_limit_vnd"),
            b.get("requested_documents")))),
        ("GET", r"/api/officer/audit", lambda h, b, m: (200, _with_officer(officer(h), svc().audit_log()))),
    ]
    return [(meth, re.compile("^" + pat + "$"), fn) for meth, pat, fn in R]


def _int_q(h: "Handler", key: str, default: int) -> int:
    try:
        return int((h.query.get(key) or [default])[0])
    except ValueError:
        raise ServiceError(422, "INVALID_QUERY", "'%s' must be an integer." % key)


def _expire(svc: SmartCreditService, tx_id: str, h: "Handler") -> Dict[str, Any]:
    """Demo control: the owning demo customer (or an officer) can fast-forward the 5-minute timer."""
    cid = h.headers.get("X-Demo-Customer")
    if cid:
        svc.get_view(tx_id, cid)
    elif h.headers.get("X-Demo-Officer-Code") != DEMO_OFFICER_CODE:
        raise ServiceError(401, "DEMO_AUTH_REQUIRED", "Demo customer or officer header required.")
    return svc.expire_now(tx_id, cid or "officer")


def _created(result: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return (200 if result.get("resumed") else 201), result


def _with_officer(_officer: str, data: Any) -> Any:
    return data


def _own_audit(svc: SmartCreditService, tx_id: str, customer_id: str) -> List[Dict[str, Any]]:
    svc.get_view(tx_id, customer_id)  # ownership check
    keep = ("event_id", "ts", "event_type", "from_state", "to_state", "actor_type")
    return [{k: e[k] for k in keep} for e in svc.audit_log(transaction_id=tx_id)]


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartCreditDemo/1.0"
    routes: List[Route] = []
    app: App = None  # type: ignore
    query: Dict[str, List[str]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        if os.environ.get("SMART_CREDIT_QUIET") != "1":
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        self.query = parse_qs(parsed.query)
        if not parsed.path.startswith("/api/"):
            return self._static(parsed.path) if method == "GET" else self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED"}})
        body: Dict[str, Any] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": {"code": "INVALID_JSON", "message": "Request body is not valid JSON."}})
                if not isinstance(body, dict):
                    return self._send(400, {"error": {"code": "INVALID_JSON", "message": "Body must be a JSON object."}})
        path_matched = False
        for meth, pattern, fn in self.routes:
            m = pattern.match(parsed.path)
            if not m:
                continue
            path_matched = True
            if meth != method:
                continue
            try:
                status, payload = fn(self, body, m.groupdict())
                return self._send(status, payload)
            except ServiceError as e:
                return self._send(e.http_status, {"error": {"code": e.code, "message": e.message, "details": e.details}})
            except Exception as e:  # pragma: no cover - safety net
                traceback.print_exc()
                return self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": "Unexpected error: %s" % e}})
        if path_matched:
            return self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "Method not allowed."}})
        return self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown endpoint."}})

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(self.app.web_root, path.lstrip("/")))
        if not full.startswith(os.path.realpath(self.app.web_root)) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(full)[0] or "application/octet-stream") + "; charset=utf-8"
                         if full.endswith((".html", ".css", ".js")) else mimetypes.guess_type(full)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"routes": build_routes(app), "app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
