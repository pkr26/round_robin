"""Read-only repository audit probes; all application state uses a temporary DB."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from io import BytesIO
import hashlib, json, os, random
from PIL import Image
from fastapi.testclient import TestClient
from round_robin.configuration import Settings
from round_robin.app import create_app
from round_robin.fixtures import sample_brief
from round_robin.visual.assets import validate_raster
from round_robin.visual.reviews import issue_review_receipt, PixelView
from round_robin.visual.schemas import ImageCapabilities

def command(**values):
    return {"command_id":str(uuid4()),**values}

results={}
os.environ['ROUND_ROBIN_VISUAL_ENABLED']='1'
os.environ['ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED']='1'
with TemporaryDirectory(prefix='rr-visual-review-') as temporary:
    root=Path(temporary).resolve()
    app=create_app(f"sqlite:///{root/'review.db'}",settings=replace(Settings(),artifact_directory=root/'artifacts'),initialize=True)
    with TestClient(app,raise_server_exceptions=False) as client:
        meeting=client.post('/api/meetings',json=command(brief=sample_brief().model_dump(mode='json'))).json()
        create_path=f"/api/meetings/{meeting['id']}/design-sessions"
        design=client.post(create_path,json=command(mode='review_only',brief={'purpose':'Wide banner','audience':'test'})).json()
        output=BytesIO();Image.new('RGB',(9000,1),'blue').save(output,format='PNG');raw=output.getvalue()
        upload=client.post(f"/api/design-sessions/{design['id']}/images",content=raw,headers={'Content-Type':'image/png','X-Command-Id':str(uuid4()),'X-Expected-Revision':'1','X-Content-Sha256':hashlib.sha256(raw).hexdigest()})
        results['wide_upload']={'dimensions':[9000,1],'bytes':len(raw),'upload_status':upload.status_code,'read_design_status':client.get(f"/api/design-sessions/{design['id']}").status_code,'list_designs_status':client.get(create_path).status_code}
        design=client.post(create_path,json=command(mode='improve',brief={'purpose':'Square diagram','audience':'test','domain':'structured_diagram','width':1024,'height':1024})).json()
        diagram={'width':800,'height':400,'nodes':[{'id':'node_a','label':'A','x':50,'y':70},{'id':'node_b','label':'B','x':450,'y':70}],'edges':[{'source':'node_a','target':'node_b'}]}
        rendered=client.post(f"/api/design-sessions/{design['id']}/native-diagram",json=command(expected_revision=1,diagram=diagram))
        results['native_frozen_dimensions']={'frozen_dimensions':[1024,1024],'status':rendered.status_code,'actual_dimensions':[[v['width'],v['height']]for v in rendered.json().get('versions',[])]}
        svg=client.get(f"/api/design-versions/{rendered.json()['versions'][0]['id']}/source.svg").text
        results['directed_edges']={'source':'node_a','target':'node_b','svg_line_only': '<line ' in svg and 'marker-end' not in svg and '<polygon' not in svg}

# Random source is synthetic and remains only in memory.
image=Image.frombytes('RGB',(2200,2200),random.Random(0).randbytes(2200*2200*3));output=BytesIO();image.save(output,format='JPEG',quality=95)
raster=validate_raster(output.getvalue(),'image/jpeg')
results['canonical_review_limit']={'uploaded_jpeg_bytes':len(output.getvalue()),'accepted_png_bytes':len(raster.canonical_bytes),'critic_maximum_bytes':64*1024*1024}
try:
    issue_review_receipt(ImageCapabilities(provider='anthropic',endpoint_version='test',understands_images=True,supported_media=['image/png'],maximum_input_bytes=64*1024*1024),'version_one',raster.canonical_hash,[PixelView(raster.canonical_bytes,raster.canonical_hash)])
except Exception as error:
    results['canonical_review_limit']['review_error']=f'{type(error).__name__}: {error}'
print(json.dumps(results,indent=2))
