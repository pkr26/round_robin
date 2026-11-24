from backend.tests.test_orchestration import factory as factory, seed
from round_robin.database.models import Run
from round_robin.orchestration.budget import release_unused_finish, reserve_finish


def test_absent_and_already_released_holds_are_noops(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        assert release_unused_finish(session, run) is False
        hold = reserve_finish(session, run)
        assert release_unused_finish(session, run) is True
        assert (hold.status, hold.reserved_usd) == ("released", "0")
        assert release_unused_finish(session, run) is False
