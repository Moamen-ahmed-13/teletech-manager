import tempfile
import unittest
from pathlib import Path
import ezdxf
from dxfedit import Dxf, Editor

class DxfEditTests(unittest.TestCase):
    def make_nested(self, path):
        doc = ezdxf.new()
        doc.blocks.new('D-V')
        inner = doc.blocks.new('INNER')
        inner.add_blockref('D-V', (10, 20))
        root = doc.blocks.new('ROOT')
        root.add_blockref('INNER', (100, 200))
        msp = doc.modelspace()
        msp.add_blockref('ROOT', (1000, 2000))
        msp.add_blockref('ROOT', (5000, 6000))
        doc.saveas(path)

    def test_nested_move_is_instance_local(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.dxf'; out = Path(td) / 'out.dxf'
            self.make_nested(src)
            ed = Editor(Dxf(src)); ed.move(ed.drawing.instances()[0], (1500, 2600)); ed.write(out)
            after = sorted(tuple(round(v, 2) for v in x['world']) for x in Dxf(out).instances())
            self.assertEqual(after, [(1500.0, 2600.0), (5110.0, 6220.0)])

    def test_nested_delete_is_instance_local(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.dxf'; out = Path(td) / 'out.dxf'
            self.make_nested(src)
            ed = Editor(Dxf(src)); ed.delete(ed.drawing.instances()[0]); ed.write(out)
            after = Dxf(out).instances()
            self.assertEqual(len(after), 1)
            self.assertEqual([round(v, 2) for v in after[0]['world']], [5110.0, 6220.0])

    def test_multiple_nested_edits_under_same_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.dxf'; self.make_nested(src)
            ed = Editor(Dxf(src)); targets = ed.drawing.instances()
            ed.delete(targets[0])
            with self.assertRaisesRegex(ValueError, 'Multiple nested edits'):
                ed.delete(targets[0])

if __name__ == '__main__':
    unittest.main()
