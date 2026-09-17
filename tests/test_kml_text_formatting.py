import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.kml_text_formatting import format_kml_coordinates


def line_string_document(body: str, namespace: str = "") -> str:
    declaration = f' xmlns="{namespace}"' if namespace else ""
    return (
        f"<kml{declaration}><Placemark><LineString>"
        f"<coordinates>{body}</coordinates>"
        "</LineString></Placemark></kml>"
    )


def coordinate_lines(contents: str) -> list[str]:
    body = contents.split("<coordinates>", 1)[1].split("</coordinates>", 1)[0]
    return [line.strip() for line in body.splitlines() if line.strip()]


class KmlTextFormattingTests(unittest.TestCase):
    def test_invalid_tokens_stay_with_following_coordinate_and_tokens_are_preserved(self):
        source = line_string_document("68 1,2,3 broken 4,5,6 7,8,9")
        result = format_kml_coordinates(source)

        self.assertTrue(result.changed)
        self.assertEqual(
            coordinate_lines(result.contents),
            ["68 1,2,3", "broken 4,5,6", "7,8,9"],
        )
        original_tokens = source.split("<coordinates>", 1)[1].split("</coordinates>", 1)[0].split()
        formatted_tokens = result.contents.split("<coordinates>", 1)[1].split("</coordinates>", 1)[0].split()
        self.assertEqual(formatted_tokens, original_tokens)
        self.assertEqual(result.valid_coordinate_count, 3)
        self.assertEqual(result.invalid_token_count, 2)

    def test_trailing_and_entirely_invalid_sequences_remain_visibly_grouped(self):
        trailing = format_kml_coordinates(line_string_document("1,2 3,4 bad worse"))
        self.assertEqual(coordinate_lines(trailing.contents), ["1,2", "3,4 bad worse"])

        invalid = format_kml_coordinates(line_string_document("68 bad 1,2,3,4"))
        self.assertEqual(coordinate_lines(invalid.contents), ["68 bad 1,2,3,4"])
        self.assertEqual(invalid.valid_coordinate_count, 0)
        self.assertEqual(invalid.invalid_token_count, 3)

    def test_non_numeric_non_finite_and_out_of_range_tokens_use_parser_rules(self):
        source = line_string_document(
            "x,2 1,nan 181,2 1,91 1,2,inf 1,2 3,4,5"
        )
        result = format_kml_coordinates(source)
        self.assertEqual(
            coordinate_lines(result.contents),
            ["x,2 1,nan 181,2 1,91 1,2,inf 1,2", "3,4,5"],
        )
        self.assertEqual(result.invalid_token_count, 5)

    def test_namespace_free_ogc_legacy_and_arbitrary_prefixes_are_supported(self):
        namespaces = (
            "",
            "http://www.opengis.net/kml/2.2",
            "http://earth.google.com/kml/2.1",
        )
        for namespace in namespaces:
            with self.subTest(namespace=namespace):
                result = format_kml_coordinates(line_string_document("1,2 3,4", namespace))
                self.assertEqual(coordinate_lines(result.contents), ["1,2", "3,4"])

        prefixed = (
            '<earth:kml xmlns:earth="http://www.opengis.net/kml/2.2">'
            "<earth:Placemark><earth:LineString>"
            "<earth:coordinates>1,2 3,4</earth:coordinates>"
            "</earth:LineString></earth:Placemark></earth:kml>"
        )
        formatted = format_kml_coordinates(prefixed).contents
        self.assertIn("<earth:coordinates>\n    1,2\n    3,4\n</earth:coordinates>", formatted)

    def test_foreign_malformed_comments_and_cdata_are_not_rewritten(self):
        foreign = line_string_document("1,2 3,4", "https://example.test/not-kml")
        foreign_result = format_kml_coordinates(foreign)
        self.assertFalse(foreign_result.changed)
        self.assertEqual(foreign_result.contents, foreign)
        self.assertIn("unsupported", foreign_result.message)

        malformed = "<kml><coordinates>1,2 3,4</kml>"
        malformed_result = format_kml_coordinates(malformed)
        self.assertFalse(malformed_result.changed)
        self.assertEqual(malformed_result.contents, malformed)

        cdata = (
            "<kml><LineString><coordinates><![CDATA[1,2 3,4]]></coordinates>"
            "</LineString></kml>"
        )
        self.assertEqual(format_kml_coordinates(cdata).contents, cdata)

        entity = "<kml><LineString><coordinates>1&#44;2 3,4</coordinates></LineString></kml>"
        self.assertEqual(format_kml_coordinates(entity).contents, entity)

        commented = (
            "<kml><!-- <coordinates>9,9 8,8</coordinates> -->"
            "<Placemark><LineString><coordinates>1,2 3,4</coordinates>"
            "</LineString></Placemark></kml>"
        )
        formatted = format_kml_coordinates(commented).contents
        self.assertIn("<!-- <coordinates>9,9 8,8</coordinates> -->", formatted)
        self.assertIn("<coordinates>\n    1,2\n    3,4\n</coordinates>", formatted)

    def test_utf8_text_before_coordinates_does_not_shift_source_replacements(self):
        source = (
            "<kml><Placemark><name>é 😀</name><LineString>"
            "<coordinates>68 1,2 3,4</coordinates>"
            "</LineString></Placemark></kml>"
        )
        result = format_kml_coordinates(source)
        self.assertTrue(result.changed)
        self.assertIn("<name>é 😀</name>", result.contents)
        self.assertEqual(coordinate_lines(result.contents), ["68 1,2", "3,4"])

    def test_gx_coordinates_are_put_on_separate_lines_without_value_changes(self):
        source = (
            '<k:kml xmlns:k="http://earth.google.com/kml/2.1" '
            'xmlns:g="http://www.google.com/kml/ext/2.2">'
            "<k:Placemark><g:Track><k:when>one</k:when>"
            "<g:coord>1 2 3</g:coord><g:coord>4 5 6</g:coord>"
            "</g:Track></k:Placemark></k:kml>"
        )
        result = format_kml_coordinates(source)
        self.assertTrue(result.changed)
        lines = result.contents.splitlines()
        self.assertTrue(any(line.strip() == "<g:coord>1 2 3</g:coord>" for line in lines))
        self.assertTrue(any(line.strip() == "<g:coord>4 5 6</g:coord>" for line in lines))
        self.assertEqual(result.gx_coordinate_count, 2)

    def test_formatting_is_idempotent_and_self_closing_coordinates_are_safe(self):
        first = format_kml_coordinates(line_string_document("68 1,2 3,4"))
        second = format_kml_coordinates(first.contents)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.contents, first.contents)

        self_closing = "<kml><Placemark><LineString><coordinates/></LineString></Placemark></kml>"
        result = format_kml_coordinates(self_closing)
        self.assertFalse(result.changed)
        self.assertEqual(result.contents, self_closing)


if __name__ == "__main__":
    unittest.main()
