"""10 simulated users reading independent dashboard data; no Deriv or live service calls.

This is a bounded local concurrency regression, not a production load benchmark.
Use a temporary SQLite file and synthetic sessions; never submit orders.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from statistics import median
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.security import encrypt, hashsecret


READ_ROUTES = (
    "/api/automation/profile",
    "/api/demo/orders",
    "/api/real/orders",
)
USERS = 10
PASSES = 3


def _percentile(values, percentile):
    values = sorted(values)
    assert values
    position = max(0, min(len(values) - 1, int((len(values) - 1) * percentile + 0.999999)))
    return round(values[position], 2)


def test_ten_simulated_users_read_independent_histories(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/readonly-load.sqlite",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)

    # Explicitly mock the trading entrypoints: the test must not use external money.
    def forbidden_broker_call(*args, **kwargs):
        raise AssertionError("No broker or purchase call is permitted in read-only load tests")

    monkeypatch.setattr(main, "demo_buy", forbidden_broker_call)

    try:
        with factory() as session:
            for index in range(USERS):
                account = f"V_READONLY_{index:02d}"
                user = db.User(
                    deriv_account_id=account,
                    selected_mode="demo",
                    stake="1.00", stop_win="10.00", stop_loss="5.00",
                )
                session.add(user)
                session.flush()
                session.add(db.Session(
                    session_hash=hashsecret(f"read-only-session-{index}"),
                    user_id=user.id,
                    token_encrypted=encrypt("synthetic-token"),
                    expires_at=db.utcnow() + timedelta(hours=1),
                ))
                symbol = f"R_TEST_{index:02d}"
                session.add(db.AutomationProfile(
                    user_id=user.id, market="volatility",
                    symbols_json='["' + symbol + '"]', strategy="trend",
                    account_mode="demo",
                ))
                session.add(db.DemoOrder(
                    user_id=user.id, request_key=f"demo-synthetic-{index}",
                    state="SETTLED", contract_id=f"DEMO_SYNTHETIC_{index}",
                    symbol=symbol, direction="CALL",
                    duration_minutes=5, stake="1.00", pnl="0.50",
                ))
                session.add(db.RealOrder(
                    user_id=user.id, account_id=f"REAL_SYNTHETIC_{index}",
                    request_key=f"real-synthetic-{index}",
                    state="SETTLED", contract_id=f"REAL_SYNTHETIC_CONTRACT_{index}",
                    symbol=symbol, direction="PUT", duration_minutes=5,
                    stake="1.00", currency="USD", pnl="-1.00",
                ))
            session.commit()

        def read_user(index):
            expected_symbol = f"R_TEST_{index:02d}"
            timings = []
            # Do not use TestClient as a context manager: that would trigger
            # app lifespan workers and could contact a remote broker.
            client = TestClient(main.app)
            try:
                client.cookies.set("nexora_sid", f"read-only-session-{index}")
                for _ in range(PASSES):
                    for route in READ_ROUTES:
                        start = perf_counter()
                        response = client.get(route)
                        timings.append((perf_counter() - start) * 1000)
                        assert response.status_code == 200, (route, response.text[:150])
                        body = response.json()
                        symbols = body["symbols"] if route.endswith("/profile") else [
                            item["symbol"] for item in body["items"]
                        ]
                        assert symbols == [expected_symbol], (index, route, symbols)
            finally:
                client.close()
            return timings

        # All clients share one file-backed DB, but their sessions are distinct.
        with ThreadPoolExecutor(max_workers=USERS) as pool:
            durations = list(pool.map(read_user, range(USERS)))
        elapsed = [ms for user_timings in durations for ms in user_timings]
        assert len(elapsed) == USERS * PASSES * len(READ_ROUTES)
        print(
            f"READ_ONLY_LOCAL_SQLITE users={USERS} requests={len(elapsed)} "
            f"p50_ms={round(median(elapsed), 2)} "
            f"p95_ms={_percentile(elapsed, 0.95)} "
            f"max_ms={round(max(elapsed), 2)}"
        )
        # Deliberately no fixed latency threshold: shared CI runners are variable.
        # Correctness/isolation and bounded request count are the regression guards.
    finally:
        engine.dispose()
