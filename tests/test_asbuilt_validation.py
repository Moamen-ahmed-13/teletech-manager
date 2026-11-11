import tempfile
import unittest
from pathlib import Path

import ezdxf

from asbuilt_apply import file_sha256
from asbuilt_validation import validate_export
from dxfedit import Dxf, Editor


class AsBuiltExportValidationTests(unittest.TestCase):
    def make_source(self, path, points):
        doc = ezdxf.new()
        doc.blocks.new('D-V')
        for point in points:
            doc.modelspace().add_blockref('D-V', point)
        doc.saveas(path)

    def test_delete_is_verified_and_original_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'original.dxf'
            output = Path(td) / 'updated.dxf'
            self.make_source(source, [(100, 100), (300, 300)])
            original_hash = file_sha256(source)
            drawing = Dxf(source)
            target = drawing.instances()[0]
            editor = Editor(drawing)
            editor.delete(target)
            editor.write(output)

            result = validate_export(source, output, [{
                'op': 'DELETE', 'block': target['name'], 'world': target['world']
            }], original_sha256=original_hash)

            self.assertTrue(result['ok'], result['errors'])
            self.assertEqual(result['verified_deletes'], 1)
            self.assertEqual(file_sha256(source), original_hash)

    def test_move_is_verified_at_arrow_tip_coordinates(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'original.dxf'
            output = Path(td) / 'updated.dxf'
            self.make_source(source, [(100, 100), (500, 500)])
            original_hash = file_sha256(source)
            drawing = Dxf(source)
            target = drawing.instances()[0]
            destination = [250.0, 275.0]
            editor = Editor(drawing)
            editor.move(target, destination)
            editor.write(output)

            result = validate_export(source, output, [{
                'op': 'MOVE', 'block': target['name'],
                'from': target['world'], 'to': destination
            }], original_sha256=original_hash)

            self.assertTrue(result['ok'], result['errors'])
            self.assertEqual(result['verified_moves'], 1)
            self.assertEqual(file_sha256(source), original_hash)


if __name__ == '__main__':
    unittest.main()
