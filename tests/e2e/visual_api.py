"""Disposable loopback API used only by the visual browser checks."""
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path

from fastapi.responses import Response
from PIL import Image, PngImagePlugin

from round_robin.app import create_app
from round_robin.configuration import Settings

root = Path(os.environ["ROUND_ROBIN_VISUAL_TEST_DIRECTORY"]).resolve()
if not root.name.startswith("round-robin-visual-e2e-"):
    raise RuntimeError("Visual browser checks require their own disposable directory")
app = create_app(f"sqlite:///{root / 'test.sqlite'}", settings=replace(Settings(), artifact_directory=root / "artifacts"), initialize=True)


@app.get("/_synthetic_raster")
def synthetic_raster():
    result = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Location", "synthetic-location-sentinel")
    Image.new("RGB", (72, 48), "#3a6648").save(result, format="PNG", pnginfo=info)
    return Response(result.getvalue(), media_type="image/png")
