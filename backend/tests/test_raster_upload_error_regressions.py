import hashlib
from io import BytesIO
from uuid import uuid4

from PIL import Image
import pytest
from sqlalchemy import select

from backend.tests.test_visual import visual_app as visual_app  # noqa: PLC0414
from round_robin.database.visual_models import DesignOperation, DesignVersion


@pytest.mark.parametrize('case,expected', [('mismatch', 'declared'), ('animated', 'single'), ('corrupt', 'safely decoded')])
def test_upload_rejection_explains_problem_and_releases_storage(visual_app, case, expected):
    app, client, _, design = visual_app
    output = BytesIO()
    image = Image.new('RGB', (2, 2), 'red')
    if case == 'mismatch': image.save(output, format='JPEG')
    elif case == 'animated': image.save(output, format='PNG', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')], duration=40, loop=0)
    else: output.write(b'not an image')
    raw, command = output.getvalue(), uuid4()
    response = client.post(f"/api/design-sessions/{design['id']}/images", content=raw, headers={
        'Content-Type': 'image/png', 'X-Command-Id': str(command), 'X-Expected-Revision': str(design['revision']),
        'X-Content-Sha256': hashlib.sha256(raw).hexdigest(),
    })
    assert response.status_code == 422 and response.json()['error']['code'] == 'invalid_raster'
    assert expected in response.json()['error']['message'].casefold()
    with app.state.session_factory() as session:
        hold = session.get(DesignOperation, f'visualupload_{command.hex}')
        assert hold.payload['status'] == 'released'
        assert session.scalar(select(DesignVersion).where(DesignVersion.session_id == design['id'])) is None
