from decimal import Decimal

from sqlalchemy import select

from app import main, db
from test_demopilot import prepare, cleanup


def test_panel_profile_cycles_use_cycle_multiplier(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(
        tmp_path, monkeypatch, stop_loss='0.00'
    )
    try:
        with factory() as sess:
            user = sess.get(db.User, uid)
            sess.add(db.PilotValidationBatch(user_id=uid, after_order_id=0))
            sess.add(db.RobotRun(user_id=uid, after_order_id=0))
            profile = sess.get(db.AutomationPanelProfile, (uid, 'strategies'))
            profile.operation_mode = 'strategies'
            profile.market = 'volatility'
            profile.symbols_json = '["1HZ100V"]'
            profile.strategy = 'pulso_v2'
            profile.account_mode = 'demo'
            profile.expiry_minutes = 5
            profile.stake = '2.00'
            profile.requested_martingale = False
            profile.cycles_limit = 2
            profile.cycle_multiplier = '2.50'

            order = db.DemoOrder(
                source_panel_mode='strategies',
                user_id=uid,
                request_key='panel-cycle-profile-001',
                state='SETTLED',
                symbol='EURUSD',
                direction='PUT',
                duration_minutes=5,
                stake='2.00',
                pnl='-2.00',
            )
            sess.add(order)
            sess.flush()
            sess.add(db.AutoPilotAttribution(
                order_id=order.id,
                user_id=uid,
                strategy='pulso_v2',
                direction='PUT',
                reason='fresh signal before next cycle',
            ))
            sess.commit()

        with factory() as sess:
            user = sess.get(db.User, uid)
            profile = sess.get(db.AutomationPanelProfile, (uid, 'strategies'))
            batch = sess.get(db.PilotValidationBatch, uid)
            orders = sess.scalars(
                select(db.DemoOrder)
                .where(db.DemoOrder.user_id == uid)
                .order_by(db.DemoOrder.id)
            ).all()

            entry, last = main.v2_symbol_recovery(
                sess, user, orders, profile, profile, batch, profile
            )

            assert (entry.cycle, entry.gale, entry.stake, entry.retry) == (
                1, 0, Decimal('5.00'), False
            )
            assert last.symbol == 'EURUSD'
    finally:
        cleanup(engine)
