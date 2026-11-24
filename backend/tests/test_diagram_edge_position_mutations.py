from io import BytesIO
from xml.etree import ElementTree

from PIL import Image
from round_robin.visual.renderers.diagram import Diagram, render_diagram


def test_relationship_label_is_centered_between_unequal_nodes_in_svg_and_raster():
    source = {
        "width": 500,
        "height": 300,
        "nodes": [
            {
                "id": "node_left",
                "label": "Left",
                "x": 30,
                "y": 30,
                "width": 91,
                "height": 53,
            },
            {
                "id": "node_right",
                "label": "Right",
                "x": 280,
                "y": 131,
                "width": 113,
                "height": 69,
            },
        ],
        "edges": [{"source": "node_left", "target": "node_right", "label": "Link"}],
    }
    rendered = render_diagram(Diagram.model_validate(source))
    root = ElementTree.fromstring(rendered.svg)
    texts = [
        node
        for node in root.iter()
        if node.tag.endswith("text") and node.text == "Link"
    ]
    assert len(texts) == 1
    assert (float(texts[0].attrib["x"]), float(texts[0].attrib["y"])) == (205, 105)
    image = Image.open(BytesIO(rendered.raster.canonical_bytes)).convert("RGB")
    # The trusted typeface draws exact interior pixels above the label baseline;
    # this box is clear of both node rectangles and the connecting line.
    assert any(
        image.getpixel((x, y)) == (38, 61, 48)
        for x in range(205, 230)
        for y in range(90, 105)
    )
