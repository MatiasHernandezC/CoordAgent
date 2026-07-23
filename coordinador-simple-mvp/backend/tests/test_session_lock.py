from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.services.session_service import SessionService


def test_same_session_is_serialized() -> None:
    service = SessionService()
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first_operation() -> None:
        with service.session_lock("group-a"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_operation() -> None:
        with service.session_lock("group-a"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_operation)
        assert first_entered.wait(timeout=1)
        second = executor.submit(second_operation)
        try:
            assert not second_entered.wait(timeout=0.1)
        finally:
            release_first.set()

        first.result(timeout=2)
        second.result(timeout=2)
        assert second_entered.is_set()


def test_different_sessions_can_progress_in_parallel() -> None:
    service = SessionService()
    first_entered = Event()
    release_first = Event()
    other_entered = Event()

    def first_operation() -> None:
        with service.session_lock("group-a"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def other_operation() -> None:
        with service.session_lock("group-b"):
            other_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_operation)
        assert first_entered.wait(timeout=1)
        other = executor.submit(other_operation)
        try:
            assert other_entered.wait(timeout=1)
        finally:
            release_first.set()

        first.result(timeout=2)
        other.result(timeout=2)
