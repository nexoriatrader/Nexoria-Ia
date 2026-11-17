"""Fail-closed Deriv public tick feed keepalive tests (no broker requests)."""
import asyncio
import json

import pytest

from app import market


def test_public_feed_sends_application_ping_during_silence():
    class SilentUntilPing:
        def __init__(self):
            self.pings=[]
            self.ready=asyncio.Event()

        async def recv(self):
            await self.ready.wait()
            return json.dumps({'msg_type':'ping','ping':'pong'})

        async def send(self, raw):
            self.pings.append(json.loads(raw))
            self.ready.set()

    async def check():
        ws=SilentUntilPing()
        stream=market.public_feed_messages(ws,heartbeat_seconds=0.015)
        try:
            raw=await asyncio.wait_for(anext(stream),timeout=1)
            assert json.loads(raw)['msg_type']=='ping'
            assert ws.pings==[{'ping':1,'req_id':7301}]
        finally:
            await stream.aclose()

    asyncio.run(check())


def test_public_feed_passes_through_real_tick_without_generating_prices():
    class TickSocket:
        async def recv(self):
            return json.dumps({'msg_type':'tick','tick':{'quote':'100.12'}})
        async def send(self,raw):
            pytest.fail('No keepalive needed before first tick')

    async def check():
        stream=market.public_feed_messages(TickSocket(),heartbeat_seconds=35)
        try:
            raw=await asyncio.wait_for(anext(stream),timeout=1)
            assert json.loads(raw)=={'msg_type':'tick','tick':{'quote':'100.12'}}
        finally:
            await stream.aclose()

    asyncio.run(check())


def test_public_feed_socket_errors_propagate_for_reconnect():
    class BrokenSocket:
        async def recv(self):
            raise OSError('simulated disconnection')
        async def send(self,raw):
            pytest.fail('No keepalive on broken socket')

    async def check():
        stream=market.public_feed_messages(BrokenSocket(),heartbeat_seconds=35)
        try:
            with pytest.raises(OSError,match='disconnection'):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(check())


def test_invalid_keepalive_interval_rejected():
    async def check():
        stream=market.public_feed_messages(None,heartbeat_seconds=61)
        with pytest.raises(ValueError,match='heartbeat'):
            await anext(stream)
        await stream.aclose()

    asyncio.run(check())
