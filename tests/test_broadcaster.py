from kalshi_bot.dashboard.broadcaster import Broadcaster


class _FakeClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise ConnectionError("client disconnected")
        self.sent.append(data)


async def test_broadcast_reaches_all_registered_clients():
    broadcaster = Broadcaster()
    a, b = _FakeClient(), _FakeClient()
    await broadcaster.register(a)
    await broadcaster.register(b)

    await broadcaster.broadcast({"type": "index_tick", "index_id": "BRTI", "value": 100.0})

    assert len(a.sent) == 1
    assert len(b.sent) == 1
    assert "BRTI" in a.sent[0]


async def test_unregistered_client_receives_nothing():
    broadcaster = Broadcaster()
    a = _FakeClient()
    await broadcaster.register(a)
    await broadcaster.unregister(a)

    await broadcaster.broadcast({"type": "index_tick"})

    assert a.sent == []


async def test_broadcast_drops_dead_clients_without_failing_others():
    broadcaster = Broadcaster()
    dead, alive = _FakeClient(fail=True), _FakeClient()
    await broadcaster.register(dead)
    await broadcaster.register(alive)

    await broadcaster.broadcast({"type": "index_tick"})
    assert len(alive.sent) == 1

    # The dead client should have been dropped; a second broadcast only reaches `alive`.
    await broadcaster.broadcast({"type": "index_tick"})
    assert len(alive.sent) == 2
