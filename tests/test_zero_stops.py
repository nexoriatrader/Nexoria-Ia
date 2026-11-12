"""Zero stops mean disabled DEMO thresholds; all other safeguards remain."""
from decimal import Decimal
from types import SimpleNamespace
from app.demo_ledger import order_guard, OrderGuardError
from app.engine import Settings, Engine
import pytest


def user(win='0.00',loss='0.00',mg=False):
    return SimpleNamespace(stop_win=win,stop_loss=loss,martingale=mg,stake='2.00')


def trade(profit):
    return SimpleNamespace(state='SETTLED',pnl=str(profit))


def test_zero_stops_disable_only_stop_gates():
    assert order_guard(user(),[trade('-1000')], '2.00')==Decimal('-1000')
    assert order_guard(user(),[trade('2000')], '2.00')==Decimal('2000')
    with pytest.raises(OrderGuardError,match='Contrato demo'):
        order_guard(user(),[SimpleNamespace(state='UNKNOWN')], '2.00')
    with pytest.raises(OrderGuardError,match='Martingale'):
        order_guard(user(mg=True),[], '2.00')
    with pytest.raises(OrderGuardError,match='Valor da entrada'):
        order_guard(user(),[], '1.00')


def test_one_zero_stop_preserves_the_other_limit():
    with pytest.raises(OrderGuardError,match='Stop Win'):
        order_guard(user(win='20',loss='0'),[trade('22')],'2')
    with pytest.raises(OrderGuardError,match='Stop Loss'):
        order_guard(user(win='0',loss='10'),[trade('-9')],'2')
    assert order_guard(user(win='0',loss='10'),[trade('200')],'2')==Decimal('200')


def test_negative_stops_rejected():
    with pytest.raises(OrderGuardError,match='Stops'):
        order_guard(user(win='-1'),[], '2')
    with pytest.raises(ValueError):
        Settings.parse({'stake':2,'stop_win':0,'stop_loss':-1,'multiplier':2})


def test_engine_respects_zero_limits():
    cfg=Settings.parse({'stake':2,'stop_win':0,'stop_loss':0,'multiplier':2})
    assert cfg.stop_win==0
    assert cfg.stop_loss==0
    rt=Engine(settings=cfg)
    rt.connected=True
    rt.pnl=Decimal('-500')
    assert rt.guard_reason(Decimal('2')) is None
    rt.settings=Settings.parse({'stake':2,'stop_win':0,'stop_loss':10,'multiplier':2})
    assert rt.guard_reason(Decimal('2'))=='Stop Loss atingido'


def test_setup_explains_zero_limit():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    assert 'id="setupWin"' in html and 'value="0.00"' in html
    assert 'id="setupLoss"' in html
    assert 'ambos em zero' in html
    assert 'desligá-lo manualmente' in html
