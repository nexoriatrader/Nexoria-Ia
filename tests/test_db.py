from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base, User, PaperTrade

def test_user_and_history_isolation():
    e=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(e)
    with Session(e) as s:
        a=User(deriv_account_id='V001',stake='2.00')
        b=User(deriv_account_id='V002',stake='3.00')
        s.add_all([a,b]);s.flush()
        s.add(PaperTrade(user_id=a.id, symbol='X', direction='RISE', stake='2.00', entry='1', exit='2',pnl='1.60',result='WIN'))
        s.commit();aid=a.id;bid=b.id
    with Session(e) as s:
        assert s.scalar(select(User.stake).where(User.id==aid))=='2.00'
        assert s.scalar(select(User.stake).where(User.id==bid))=='3.00'
        assert len(s.execute(select(PaperTrade).where(PaperTrade.user_id==aid)).scalars().all())==1
        assert len(s.execute(select(PaperTrade).where(PaperTrade.user_id==bid)).scalars().all())==0
