import time

from fha.services.clock import Clock, SystemClock


def test_system_clock_reads_epoch_seconds() -> None:
    clock: Clock = SystemClock()
    before = time.time()
    now = clock.now()
    assert before <= now <= time.time()
