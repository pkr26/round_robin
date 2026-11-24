from dataclasses import replace

from round_robin.database.models import Run
from round_robin.orchestration.configuration import verify_saved_discussion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from sqlalchemy import event

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_measure_query_count(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result=super().generate(request)
            return replace(result,output=result.output.model_copy(update={'objections':[DiscussionObjection(alias=f'new_objection_{i}',concern=f'Concern {request.stage} {self.provider} {i}',severity='minor') for i in range(1,13)]}))
    run_id=seed(factory)
    registry={p:Provider(p) for p in ('openai','anthropic','google')}
    drain(factory,registry)
    stats=[]
    def watch(conn,cursor,statement,parameters,context,executemany):
        stats.append(statement)
    event.listen(factory.kw['bind'],'before_cursor_execute',watch)
    try:
        with factory() as session:
            run=session.get(Run,run_id)
            verify_saved_discussion(session,run)
            print('OBSERVED issues',len(run.configuration.get('discussion_issues',[])),'queries',len(stats),'attempt_queries',sum('attempts' in q.lower() for q in stats))
    finally:
        event.remove(factory.kw['bind'],'before_cursor_execute',watch)
    assert len(stats) <= 10
