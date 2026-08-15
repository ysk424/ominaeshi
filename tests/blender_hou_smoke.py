"""Background Blender smoke test for MD OBJ to HOU conversion."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


def _shape(identifier, name, x0, x1):
    points = [(x0, 0.0), (x1, 0.0), (x1, 100.0), (x0, 100.0), (x0, 0.0)]
    return {
        "ID": identifier,
        "Name": name,
        "IsClosed": True,
        "ShapeInfo": {
            "LineList": [
                {
                    "PointList": [
                        {"Position": {"x": a[0], "y": a[1]}},
                        {"Position": {"x": b[0], "y": b[1]}},
                    ]
                }
                for a, b in zip(points, points[1:])
            ]
        },
        "InternalLineList": [],
    }


def main():
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo.parent))
    import ominaeshi

    ominaeshi.register()
    try:
        mesh = bpy.data.meshes.new("MD_Garment_Mesh")
        vertices = [
            (-0.5, -0.5, 0.5),
            (0.5, -0.5, 0.5),
            (0.5, 0.5, 0.5),
            (-0.5, 0.5, 0.5),
            (0.5, -0.5, 0.5),
            (1.5, -0.5, 0.5),
            (1.5, 0.5, 0.5),
            (0.5, 0.5, 0.5),
        ]
        mesh.from_pydata(
            vertices,
            [(1, 4), (2, 7)],  # Existing loose stitches must not join panels.
            [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)],
        )
        mesh.update()
        # MD's OBJ UV export uses a different, anisotropic scale and may
        # recenter each panel independently from Pattern JSON millimetres.
        uv_values = [
            (-1.0, -1.15),
            (1.0, -1.15),
            (1.0, 1.15),
            (-1.0, 1.15),
            (-4.0, -1.15),
            (-2.0, -1.15),
            (-2.0, 1.15),
            (-4.0, 1.15),
        ]
        uv = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            uv.data[loop.index].uv = uv_values[loop.vertex_index]
        source = bpy.data.objects.new("MD_Garment", mesh)
        bpy.context.scene.collection.objects.link(source)
        source["ominaeshi_from_md"] = True

        pattern = {
            "Unit": "mm",
            "PatternList": [
                _shape("A", "Front", 0.0, 100.0),
                _shape("B", "Back", 102.0, 202.0),
            ],
            "SeamLinePairGroupList": [
                {
                    "Name": "Side",
                    "PairList": [
                        {
                            "First": {
                                "ShapeID": "A",
                                "LengthParam": {"fStart": 0.25, "fEnd": 0.5},
                            },
                            "Second": {
                                "ShapeID": "B",
                                "LengthParam": {"fStart": 0.75, "fEnd": 1.0},
                            },
                        }
                    ],
                }
            ],
        }
        text = bpy.data.texts.new("Ominaeshi_Test_Pattern")
        text.write(json.dumps(pattern))
        source["ominaeshi_pattern_text"] = text.name

        props = bpy.context.scene.ominaeshi
        props.clothes_object = source
        source_vertex_count = len(source.data.vertices)
        source_edge_count = len(source.data.edges)
        assert bpy.ops.ominaeshi.create_hou() == {"FINISHED"}, props.parse_status
        collection = props.hou_collection
        assert collection is not None
        assert collection.get("housei_role") == "clothes"
        plan = json.loads(collection["housei_sewing_plan_json"])
        assert plan["schema"] == "housei-sewing-plan/1.0.0"
        assert len(plan["parts"]) == 2
        assert plan["pair_count"] == 2
        assert len(collection.objects) == 2
        for part in collection.objects:
            assert json.loads(part["HOU"])["schema"] == "housei-hou/1.0.0"
            pattern_attribute = part.data.attributes.get("housei_pattern_position")
            assert pattern_attribute is not None
            assert len(pattern_attribute.data) == len(part.data.vertices)
        pattern_x = sorted(
            round(float(item.vector.x), 4)
            for part in collection.objects
            for item in part.data.attributes["housei_pattern_position"].data
        )
        assert pattern_x[0] == 0.0
        assert pattern_x[-1] == 0.202
        assert len(source.data.vertices) == source_vertex_count
        assert len(source.data.edges) == source_edge_count

        # A second press replaces only the previous generated copy.
        assert bpy.ops.ominaeshi.create_hou() == {"FINISHED"}, props.parse_status
        assert props.hou_collection == collection
        assert len(collection.objects) == 2
        print(
            "Ominaeshi HOU smoke passed: "
            f"parts={len(plan['parts'])}, pairs={plan['pair_count']}, source_unchanged=1"
        )
    finally:
        ominaeshi.unregister()


if __name__ == "__main__":
    main()
