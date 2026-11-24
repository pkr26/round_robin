from round_robin.schemas.briefs import DraftBrief
from round_robin.fixtures import sample_brief
from round_robin.orchestration.reporting import comparison_data
from round_robin.visual.renderers.diagram import Diagram, diagram_font, render_diagram
import json
body=sample_brief().model_dump(mode="json")
body.update(currency="KWD", budget="1.001", horizon_months=1, hard_requirements=[{"id":"limit_kwd","description":"Cost ceiling","origin":"user_supplied","metric":"total_cost","operator":"lte","threshold":"1.001"}])
body["options"]=[{"id":"option_kwd","label":"KWD quote","provenance":"user_supplied","cost_components":[{"id":"cost_kwd","label":"KWD line","kind":"upfront","amount":"1.005","currency":"KWD","source_ref":"quote_kwd","frequency":"once"},*[{"id":"cost_"+kind,"label":kind,"kind":kind,"amount":"0","currency":"KWD","frequency":"once","status":"not_applicable","source_ref":"quote_kwd"} for kind in ["installation","recurring","maintenance","tax_delivery"]]]}]
comparisons,_=comparison_data(DraftBrief.model_validate(body))
label="REQUIRED LABEL MUST REMAIN VISIBLE"
diagram=Diagram.model_validate({"width":1000,"height":300,"nodes":[{"id":"node_one","label":"A","x":700,"y":0,"width":60,"height":70},{"id":"node_two","label":"B","x":900,"y":200,"width":60,"height":70}],"edges":[{"source":"node_one","target":"node_two","label":label}]})
rendered=render_diagram(diagram)
result={"currency":{"amount":"1.005","budget":"1.001","currency":"KWD","observed_total":comparisons[0]["total"],"observed_feasibility":comparisons[0]["constraint_results"][0]["result"]},"edge_label":{"viewport_width":1000,"label_start_x":830,"label_pixel_width":diagram_font().getlength(label),"right_edge":830+diagram_font().getlength(label),"source_accepted":True,"rendered_width":rendered.raster.width}}
print(json.dumps(result,indent=2))
