"""Consume real React-submitted JSON with production contracts/calculation policy."""
from datetime import date
from pathlib import Path
import json
from round_robin.schemas.briefs import DraftBrief
from round_robin.orchestration.reporting import comparison_data
from round_robin.research.calculator import lifecycle_total

root=Path('/tmp/rr-audit2-client')
before=DraftBrief.model_validate_json((root/'typed-constraint-before.json').read_text())
after=DraftBrief.model_validate_json((root/'typed-constraint-after.json').read_text())
# Complete the synthetic fixture's otherwise unrelated omitted categories using
# the documented not-applicable representation, identically before and after.
def complete(brief):
    payload=brief.model_dump(mode='json')
    for option in payload['options']:
        kinds={item['kind'] for item in option['cost_components']}
        for kind in {'upfront','recurring','installation','maintenance','tax_delivery'}-kinds:
            option['cost_components'].append({'id':f'{option["id"]}_{kind}', 'label':kind,'kind':kind,'amount':'0','currency':'USD','frequency':'once','status':'not_applicable','source_ref':'explicit_zero'})
    return DraftBrief.model_validate(payload)
def result(brief):
    comparisons,_=comparison_data(complete(brief),as_of=date(2026,11,16))
    return [{'option':item['option_id'],'total':item['total'],'minimum_spend':next(x for x in item['constraint_results'] if x['requirement_id'] in {'minimum_project_spend','requirement_manual_1'})} for item in comparisons]
edited=DraftBrief.model_validate_json((root/'ignored-cost-edit.json').read_text())
cost=lifecycle_total(edited.options[0],edited.horizon_months,edited.currency,calculation_id='calculation_probe',as_of=date(2026,11,16))
output={'typed_requirement_before':result(before),'typed_requirement_after':result(after),'entered_9000_upfront':{'schema_valid':True,'total':cost.output,'formula':cost.formula,'inputs':[x.model_dump(mode='json') for x in cost.inputs]}}
(root/'contract-semantic-results.json').write_text(json.dumps(output,indent=2))
print(json.dumps(output,indent=2))
