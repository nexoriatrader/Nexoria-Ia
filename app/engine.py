from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Optional
from .analysis import analyze_prices


def money(value):
    try:
        v = Decimal(str(value).replace(',', '.'))
    except InvalidOperation as e:
        raise ValueError('Valor inválido') from e
    if not v.is_finite():
        raise ValueError('Valor inválido')
    return v.quantize(Decimal('0.01'))


@dataclass
class Settings:
    stake: Decimal = Decimal('2.00')
    stop_win: Decimal = Decimal('0.00')
    stop_loss: Decimal = Decimal('0.00')
    martingale: bool = False
    multiplier: Decimal = Decimal('2.00')
    max_gales: int = 2
    payout: Decimal = Decimal('0.80')  # valor fictício para simulação; não é payout Deriv
    expiry_ticks: int = 10  # SIMULAÇÃO em ticks; não é contrato nem expiração oficial

    @classmethod
    def parse(cls, payload: dict):
        stake, sw, sl = map(money, (payload.get('stake', 2), payload.get('stop_win', 0), payload.get('stop_loss', 0)))
        mg = payload.get('martingale', False)
        if not isinstance(mg, bool):
            raise ValueError('Martingale deve ser verdadeiro ou falso')
        mult = money(payload.get('multiplier', 2))
        if stake < Decimal('0.50') or sw < 0 or sl < 0 or mult not in (Decimal('2'), Decimal('3'), Decimal('4')):
            raise ValueError('Valores fora dos limites aceitos')
        return cls(stake=stake, stop_win=sw, stop_loss=sl, martingale=mg, multiplier=mult)


@dataclass
class Position:
    direction: str
    entry: float
    stake: Decimal
    symbol: str
    ticks_remaining: int
    opened_at: str


@dataclass
class Engine:
    settings: Settings = field(default_factory=Settings)
    running: bool = False
    connected: bool = False
    source: str = 'offline'
    symbol: str = '1HZ100V'
    prices: deque = field(default_factory=lambda: deque(maxlen=250))
    position: Optional[Position] = None
    pnl: Decimal = Decimal('0.00')
    wins: int = 0
    losses: int = 0
    gale: int = 0
    closed: list = field(default_factory=list)
    last_reason: str = 'Aguardando dados'
    ticks_since_close: int = 999
    session_opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def configure(self, s: Settings):
        if self.running or self.position:
            raise ValueError('Desligue o robô e aguarde o fechamento da operação para editar')
        self.settings = s
        self.reset_session()

    def reset_session(self):
        self.pnl = Decimal('0.00')
        self.wins = self.losses = self.gale = 0
        self.closed.clear()
        self.position = None
        self.ticks_since_close = 999
        self.session_opened_at = datetime.now(timezone.utc).isoformat()

    def guard_reason(self, next_stake: Decimal):
        if not self.connected:
            return 'Sem conexão com dados de mercado'
        if self.settings.stop_win > 0 and self.pnl >= self.settings.stop_win:
            return 'Stop Win atingido'
        if self.settings.stop_loss > 0 and -self.pnl >= self.settings.stop_loss:
            return 'Stop Loss atingido'
        # Limita perda potencial total antes da próxima entrada, sem presumir payout.
        if self.settings.stop_loss > 0 and next_stake > self.settings.stop_loss + self.pnl:
            return 'Entrada ultrapassaria o Stop Loss'
        if self.position:
            return 'Já existe uma operação aberta'
        return None

    def next_stake(self):
        if self.settings.martingale:
            return (self.settings.stake * (self.settings.multiplier ** self.gale)).quantize(Decimal('0.01'))
        return self.settings.stake

    def signal(self):
        """Return an experimental signal for local simulation only."""
        signal = analyze_prices(list(self.prices))["signal"]
        return {'CALL': 'RISE', 'PUT': 'FALL'}.get(signal)

    def push_tick(self, price: float):
        if not (0 < price < float('inf')):
            return
        self.prices.append(price)
        if self.position:
            self.position.ticks_remaining -= 1
            if self.position.ticks_remaining <= 0:
                self._settle(price)
            return
        self.ticks_since_close += 1
        if not self.running:
            return
        stake = self.next_stake()
        reason = self.guard_reason(stake)
        if reason:
            self.last_reason = reason
            if reason in ('Stop Win atingido', 'Stop Loss atingido', 'Entrada ultrapassaria o Stop Loss'):
                self.running = False
            return
        if self.ticks_since_close < 15:
            self.last_reason = 'Intervalo entre entradas'
            return
        direction = self.signal()
        if not direction:
            self.last_reason = 'Aguardando oportunidade'
            return
        self.position = Position(direction, price, stake, self.symbol, self.settings.expiry_ticks,
                                 datetime.now(timezone.utc).isoformat())
        self.last_reason = 'Operação virtual em acompanhamento'

    def _settle(self, final_price: float):
        pos = self.position
        if pos is None:
            return
        correct = (final_price > pos.entry) if pos.direction == 'RISE' else (final_price < pos.entry)
        tie = final_price == pos.entry
        # Empate tratado como perda nesta simulação CONSERVADORA; contratos reais variam.
        profit = (pos.stake * self.settings.payout).quantize(Decimal('0.01')) if correct else -pos.stake
        self.pnl += profit
        self.wins += int(correct)
        self.losses += int(not correct)
        self.closed.insert(0, {'time': datetime.now(timezone.utc).isoformat(), 'symbol':pos.symbol,
                               'direction':pos.direction,'stake':str(pos.stake),'entry':pos.entry,
                               'exit':final_price,'result':'WIN' if correct else 'LOSS',
                               'pnl':str(profit), 'tie': tie})
        self.closed = self.closed[:100]
        self.position = None
        self.ticks_since_close = 0
        if correct or not self.settings.martingale or self.gale >= self.settings.max_gales:
            self.gale = 0
        else:
            self.gale += 1
        self.last_reason = 'Última operação encerrada'
        if (self.settings.stop_win > 0 and self.pnl >= self.settings.stop_win) or (self.settings.stop_loss > 0 and -self.pnl >= self.settings.stop_loss):
            self.running = False
            self.last_reason = 'Limite da sessão atingido'

    def status(self):
        return {'running':self.running, 'connected':self.connected, 'source':self.source,
                'symbol':self.symbol, 'latest_price': self.prices[-1] if self.prices else None,
                'ticks':len(self.prices), 'pnl':str(self.pnl),'wins':self.wins,'losses':self.losses,
                'gale':self.gale,'reason':self.last_reason,'open_position':bool(self.position),
                'history':self.closed[:20], 'session_opened_at':self.session_opened_at,
                'settings': {'stake':str(self.settings.stake), 'stop_win':str(self.settings.stop_win),
                             'stop_loss':str(self.settings.stop_loss), 'martingale':self.settings.martingale,
                             'multiplier':str(self.settings.multiplier)},
                'analysis':analyze_prices(list(self.prices))}
