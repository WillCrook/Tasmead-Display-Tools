import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.kml_export import (
    KML_NAMESPACE,
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlPolygon,
    KmlStyle,
    export_kml,
    render_kml,
)


NAMESPACE = {"kml": KML_NAMESPACE}


def track_document(*, name="A&B <Display>", polygon=False):
    placemarks = [
        KmlPlacemark(
            name="Path",
            style_url="#magentaTrackLine",
            geometry=KmlLineString(
                coordinates=(
                    KmlCoordinate(-0.0, 51.23456789, 100.12356),
                    KmlCoordinate(-0.70000001, 51.3, 125.0),
                ),
                altitude_mode="relativeToGround",
                extrude_to_ground=True,
            ),
        )
    ]
    if polygon:
        placemarks.append(
            KmlPlacemark(
                name="Zone",
                style_url="#zone",
                geometry=KmlPolygon(
                    outer_ring=(
                        KmlCoordinate(0, 50, 0),
                        KmlCoordinate(1, 50, 0),
                        KmlCoordinate(1, 51, 0),
                    ),
                    altitude_mode="clampToGround",
                ),
            )
        )
    styles = [
        KmlStyle("magentaTrackLine", "aaff00ff", 6, "33ff00ff"),
    ]
    if polygon:
        styles.append(KmlStyle("zone", "aaff00ff", 6, "7f0000ff"))
    return KmlDocument(name=name, styles=tuple(styles), placemarks=tuple(placemarks))


class KmlExportTests(unittest.TestCase):
    def test_renders_xml_safe_atr_style_and_fixed_coordinates(self):
        root = ET.fromstring(render_kml(track_document()))
        self.assertEqual(root.tag, f"{{{KML_NAMESPACE}}}kml")
        self.assertEqual(
            root.find("kml:Document/kml:name", NAMESPACE).text,
            "A&B <Display>",
        )
        style = root.find("kml:Document/kml:Style", NAMESPACE)
        self.assertEqual(style.get("id"), "magentaTrackLine")
        self.assertEqual(style.find("kml:LineStyle/kml:color", NAMESPACE).text, "aaff00ff")
        self.assertEqual(style.find("kml:LineStyle/kml:width", NAMESPACE).text, "6")
        self.assertEqual(style.find("kml:PolyStyle/kml:color", NAMESPACE).text, "33ff00ff")
        line = root.find("kml:Document/kml:Placemark/kml:LineString", NAMESPACE)
        self.assertEqual(line.find("kml:extrude", NAMESPACE).text, "1")
        self.assertEqual(line.find("kml:tessellate", NAMESPACE).text, "0")
        self.assertEqual(line.find("kml:coordinates", NAMESPACE).text.splitlines()[0], "0.0000000,51.2345679,100.124")

    def test_optional_placemark_description_is_escaped_and_omitted_by_default(self):
        document = track_document()
        described = KmlDocument(
            name=document.name,
            styles=document.styles,
            placemarks=(
                KmlPlacemark(
                    name=document.placemarks[0].name,
                    style_url=document.placemarks[0].style_url,
                    geometry=document.placemarks[0].geometry,
                    description="Processing warnings:\n- Omitted 2 <source> coordinate(s) & continued.",
                ),
            ),
        )

        described_root = ET.fromstring(render_kml(described))
        plain_root = ET.fromstring(render_kml(document))
        self.assertEqual(
            described_root.find(
                "kml:Document/kml:Placemark/kml:description",
                NAMESPACE,
            ).text,
            "Processing warnings:\n- Omitted 2 <source> coordinate(s) & continued.",
        )
        self.assertIsNone(
            plain_root.find("kml:Document/kml:Placemark/kml:description", NAMESPACE)
        )

    def test_polygon_ring_is_closed_once(self):
        root = ET.fromstring(render_kml(track_document(polygon=True)))
        coordinates = root.find(
            "kml:Document/kml:Placemark[2]/kml:Polygon/kml:outerBoundaryIs/kml:LinearRing/kml:coordinates",
            NAMESPACE,
        ).text.splitlines()
        self.assertEqual(len(coordinates), 4)
        self.assertEqual(coordinates[0], coordinates[-1])

    def test_invalid_values_fail_before_replacing_existing_output(self):
        document = track_document()
        invalid = KmlDocument(
            name=document.name,
            styles=document.styles,
            placemarks=(
                KmlPlacemark(
                    name="Path",
                    style_url="#magentaTrackLine",
                    geometry=KmlLineString(
                        coordinates=(KmlCoordinate(float("nan"), 50, 0), KmlCoordinate(1, 51, 0)),
                        altitude_mode="absolute",
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "output.kml"
            output.write_text("existing", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "finite"):
                export_kml(output, invalid, overwrite=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing")

    def test_invalid_xml_text_and_coordinate_range_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "forbidden by XML"):
            render_kml(track_document(name="unsafe\x00name"))

        document = track_document()
        out_of_range = KmlDocument(
            name=document.name,
            styles=document.styles,
            placemarks=(
                KmlPlacemark(
                    name="Path",
                    style_url="#magentaTrackLine",
                    geometry=KmlLineString(
                        coordinates=(KmlCoordinate(181, 50, 0), KmlCoordinate(1, 51, 0)),
                        altitude_mode="absolute",
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "longitude"):
            render_kml(out_of_range)

    def test_create_only_collision_preserves_existing_output_and_cleans_staging_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "output.kml"
            output.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                export_kml(output, track_document(), overwrite=False)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_cancellation_leaves_existing_output_untouched(self):
        called = False

        def cancellation_check():
            return called

        def coordinate_callback():
            nonlocal called
            called = True

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "output.kml"
            output.write_text("existing", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                export_kml(
                    output,
                    track_document(),
                    overwrite=True,
                    cancellation_check=cancellation_check,
                    coordinate_callback=coordinate_callback,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "existing")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
