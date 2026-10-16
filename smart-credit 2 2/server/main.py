"""Entry point:  python3 -m server.main [--port 8080] [--reset]"""
import argparse
import json
import os

from .api.http import App, make_server
from .services.smart_credit import SmartCreditService
from .store.db import open_database

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_json(*parts: str):
    with open(os.path.join(ROOT, *parts)) as f:
        return json.load(f)


def build_app(db_path: str, payment_delay_ms=None) -> App:
    policy = load_json("server", "policy", "demo_policy.json")
    fixtures = load_json("server", "fixtures", "demo_data.json")

    service = SmartCreditService(open_database(db_path, fixtures), policy, fixtures, payment_delay_ms)
    return App(service, os.path.join(ROOT, "web"), os.path.join(ROOT, "data", "reference"))


def main() -> None:
    p = argparse.ArgumentParser(description="HLB Connect Smart Credit - demo backend (simulated, fictional data)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    p.add_argument("--db", default=os.path.join(ROOT, "data", "smart_credit_demo.sqlite3"))
    p.add_argument("--reset", action="store_true", help="Delete and re-seed the demo database")
    a = p.parse_args()
    if a.reset and os.path.exists(a.db):
        os.remove(a.db)  # safe: nothing has opened the database yet
    app = build_app(a.db)
    server = make_server(app, a.host, a.port)
    print("Smart Credit demo running at http://%s:%d  (officer dashboard: /officer.html)" % (a.host, a.port))
    print("SIMULATION ONLY - fictional data, no real banking, CIC or payment systems.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
