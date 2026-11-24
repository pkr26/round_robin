"""Editable SVG from a validated diagram object, with matching local pixels.

No SVG/XML source, scripts, URLs, images, fonts or CSS are accepted as input.
This supports bounded boxes/relationships only, not arbitrary browser interfaces.
"""
from dataclasses import dataclass
from functools import lru_cache
import base64
import hashlib
from io import BytesIO
import json
from typing import Annotated, Self
from xml.etree.ElementTree import Comment, Element, SubElement, tostring

from PIL import Image, ImageDraw, ImageFont
from pydantic import AfterValidator, Field, model_validator

from round_robin.schemas.common import Contract, Id
from ..assets import Raster, validate_raster


@lru_cache(maxsize=1)
def diagram_font():
    """Use one explicit packaged font, without silently substituting glyphs."""
    try:
        return ImageFont.load_default(size=12)
    except (OSError, ImportError) as error:
        raise ValueError("The diagram renderer font is unavailable") from error


@lru_cache(maxsize=1024)
def supported_glyph(character: str) -> bool:
    if character == " ":
        return True
    font = diagram_font()
    actual, missing = font.getmask(character), font.getmask("\U0010ffff")
    return (actual.size, bytes(actual)) != (missing.size, bytes(missing))


def single_line_label(value: str) -> str:
    if any(ord(character) < 32 or ord(character) in {0xFFFE, 0xFFFF} for character in value):
        raise ValueError("Diagram labels must use a single line of XML-safe text")
    if any(not supported_glyph(character) for character in value):
        raise ValueError("Diagram text contains characters unavailable in the renderer font; use supported text or a raster/native-interface design")
    return value


Label = Annotated[str, AfterValidator(single_line_label)]


class Node(Contract):
    id: Id
    label: Label = Field(min_length=1, max_length=120)
    x: int = Field(ge=0, le=4000, strict=True)
    y: int = Field(ge=0, le=4000, strict=True)
    width: int = Field(default=180, ge=60, le=600, strict=True)
    height: int = Field(default=70, ge=40, le=300, strict=True)


class Edge(Contract):
    source: Id
    target: Id
    label: Label = Field(default="", max_length=120)


class Diagram(Contract):
    width: int = Field(default=1000, ge=100, le=4000, strict=True)
    height: int = Field(default=700, ge=100, le=4000, strict=True)
    nodes: list[Node] = Field(min_length=1, max_length=100)
    edges: list[Edge] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def relationships_and_bounds(self) -> Self:
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes) or self.width * self.height > 4_000_000:
            raise ValueError("Duplicate node or diagram pixel limit")
        for node in self.nodes:
            if node.x + node.width > self.width or node.y + node.height > self.height:
                raise ValueError("Node would be clipped outside the declared viewport")
            if diagram_font().getlength(node.label) > node.width - 20:
                raise ValueError("Label is too long for its box; shorten it or widen the box")
        if any(edge.source not in ids or edge.target not in ids for edge in self.edges):
            raise ValueError("Every relationship must resolve to existing nodes")
        if any(edge.source == edge.target for edge in self.edges):
            raise ValueError("Self relationships are unsupported; use distinct source and target nodes")
        nodes = {node.id: node for node in self.nodes}
        for edge in self.edges:
            if edge.label:
                x, y = edge_label_origin(nodes[edge.source], nodes[edge.target])
                left, top, right, bottom = diagram_font().getbbox(edge.label, anchor="ls")
                # Client rasterizers may add a fractional antialiasing overhang
                # even with the exact font and fixed advance. Two clear pixels
                # cover rounded advances plus the final antialiased ink edge.
                if x + left < 2 or y + top < 2 or x + right > self.width - 2 or y + bottom > self.height - 2:
                    raise ValueError("Relationship label would be clipped outside the declared viewport; shorten it or move the nodes")
        return self


def edge_label_origin(left: Node, right: Node) -> tuple[int, int]:
    return ((left.x + left.width // 2 + right.x + right.width // 2) // 2,
            (left.y + left.height // 2 + right.y + right.height // 2) // 2 - 5)


@dataclass(frozen=True)
class RenderedDiagram:
    source: dict
    source_hash: str
    svg: bytes
    svg_hash: str
    raster: Raster
    renderer_version: str = "structured-box-diagram-v3"


def render_diagram(diagram: Diagram) -> RenderedDiagram:
    source = diagram.model_dump(mode="json")
    source_hash = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    svg = Element("svg", xmlns="http://www.w3.org/2000/svg", width=str(diagram.width), height=str(diagram.height), viewBox=f"0 0 {diagram.width} {diagram.height}")
    SubElement(svg, "title").text = "Editable diagram from validated nodes and relationships"
    font = diagram_font()
    # The trusted font is the one used by the raster and bounds validator.
    # Browser fallback fonts can overflow even when those exact bounds fit.
    # Pillow's bundled Aileron font records its license as No Rights Reserved.
    svg.append(Comment("Embedded Aileron Regular supplied by Pillow: No Rights Reserved."))
    SubElement(svg, "style").text = (
        "@font-face{font-family:RoundRobinDiagram;src:url(data:font/ttf;base64,"
        + base64.b64encode(font.font_bytes).decode("ascii")
        + ") format('truetype')}text{font-family:RoundRobinDiagram;font-kerning:none;font-variant-ligatures:none;white-space:pre}"
    )
    SubElement(svg, "rect", width="100%", height="100%", fill="#ffffff")
    canvas = Image.new("RGB", (diagram.width, diagram.height), "white")
    draw = ImageDraw.Draw(canvas)
    nodes = {node.id: node for node in diagram.nodes}

    def svg_text(label, x, y):
        # Client font engines can use fractional advances where Pillow rounds
        # them. Fix the editable text run to the validated advance, preserving
        # spaces, so repeated glyphs cannot accumulate outside its bounds.
        SubElement(svg, "text", x=str(x), y=str(y), fill="#263d30", **{
            "font-size": "12", "textLength": str(font.getlength(label)),
            "lengthAdjust": "spacingAndGlyphs", "xml:space": "preserve",
        }).text = label

    for edge in diagram.edges:
        left, right = nodes[edge.source], nodes[edge.target]
        start, end = (left.x + left.width // 2, left.y + left.height // 2), (right.x + right.width // 2, right.y + right.height // 2)
        SubElement(svg, "line", x1=str(start[0]), y1=str(start[1]), x2=str(end[0]), y2=str(end[1]), stroke="#365b48", **{"stroke-width": "2"})
        draw.line([start, end], fill="#365b48", width=2)
        if edge.label:
            origin = edge_label_origin(left, right)
            svg_text(edge.label, *origin)
            draw.text(origin, edge.label, fill="#263d30", font=font, anchor="ls")
    for node in diagram.nodes:
        SubElement(svg, "rect", x=str(node.x), y=str(node.y), width=str(node.width), height=str(node.height), rx="7", fill="#edf4e8", stroke="#749068")
        svg_text(node.label, node.x + 10, node.y + node.height // 2 + 4)
        draw.rounded_rectangle((node.x, node.y, node.x + node.width, node.y + node.height), radius=7, fill="#edf4e8", outline="#749068")
        draw.text((node.x + 10, node.y + node.height // 2 - 6), node.label, fill="#263d30", font=font)
    encoded_svg = tostring(svg, encoding="utf-8", xml_declaration=True)
    if len(encoded_svg) > 1024 * 1024:
        raise ValueError("SVG exceeds one MiB")
    output = BytesIO()
    canvas.save(output, format="PNG")
    return RenderedDiagram(source, source_hash, encoded_svg, hashlib.sha256(encoded_svg).hexdigest(), validate_raster(output.getvalue(), "image/png"))
