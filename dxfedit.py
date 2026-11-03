"""Safe DXF INSERT instance editing for the TELETECH As-Built pipeline.

Only simple, static INSERT chains are edited. Nested edits use copy-on-write
block definitions so sibling INSERT instances and the original drawing remain
unchanged. Multiple edits under the same root INSERT are rejected explicitly.
"""
from __future__ import annotations
import uuid
import ezdxf
from ezdxf.math import Matrix44, Vec3

ENDPOINT_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'ES-cctv'}

def _handle(entity):
    return str(entity.dxf.handle or '')

def _is_static_insert(ins):
    if ins.dxftype() != 'INSERT' or ins.doc is None:
        return False
    name = str(ins.dxf.name or '')
    if not name or name.startswith('*') or ins.attribs:
        return False
    block = ins.doc.blocks.get(name)
    flags = int(block.block.dxf.get('flags', 0)) if block else 0
    return not (flags & (1 | 4 | 8 | 16))

class Dxf:
    def __init__(self, path):
        self.path = str(path)
        self.doc = ezdxf.readfile(self.path)
        self.msp = self.doc.modelspace()

    def instances(self):
        found = []
        def walk(layout, parent_m, path, depth):
            for ent in layout:
                if ent.dxftype() != 'INSERT' or not _is_static_insert(ent):
                    continue
                try:
                    world_m = parent_m @ ent.matrix44()
                    p = world_m.transform(Vec3(0, 0, 0))
                except Exception:
                    continue
                rec_path = path + [{'start': _handle(ent), 'name': str(ent.dxf.name)}]
                name = str(ent.dxf.name)
                if name.upper() in ENDPOINT_BLOCKS:
                    found.append({
                        'name': name, 'world': [float(p.x), float(p.y)],
                        'depth': depth, 'path': rec_path, 'handle': _handle(ent),
                        'local': [float(ent.dxf.insert.x), float(ent.dxf.insert.y)],
                        'root_handle': rec_path[0]['start'], 'entity': ent,
                    })
                try:
                    walk(self.doc.blocks.get(name), world_m, rec_path, depth + 1)
                except Exception:
                    pass
        walk(self.msp, Matrix44(), [], 0)
        return found

class Editor:
    def __init__(self, drawing):
        self.drawing = drawing
        self.doc = drawing.doc
        self._nested_roots = set()

    def _clone_nested_path(self, target, action, destination=None):
        path = list(target.get('path') or [])
        if len(path) < 2:
            raise ValueError('Nested target path is incomplete.')
        root_handle = path[0]['start']
        if root_handle in self._nested_roots:
            raise ValueError(f'Multiple nested edits under root INSERT {root_handle} are not combined safely; review/export separately.')
        root = self.doc.entitydb.get(root_handle)
        if root is None or root.dxftype() != 'INSERT' or not _is_static_insert(root):
            raise ValueError('Nested root INSERT is missing or unsupported.')

        # path[i] is an INSERT inside block path[i-1]. Clone containing blocks
        # from deepest parent to root and repoint only this root INSERT.
        child_new_name = None
        n = len(path)
        for level in range(n - 2, -1, -1):
            source_name = path[level]['name']
            source_block = self.doc.blocks.get(source_name)
            if source_block is None:
                raise ValueError(f'Block {source_name} is unavailable.')
            new_name = f'TTAB_{uuid.uuid4().hex[:12]}'
            clone = self.doc.blocks.new(name=new_name)
            child_handle = path[level + 1]['start']
            found = False
            for ent in source_block:
                if _handle(ent) == child_handle:
                    found = True
                    if level == n - 2 and action == 'DELETE':
                        continue
                    new_ent = ent.copy()
                    if level == n - 2 and action == 'MOVE':
                        parent_m = Matrix44()
                        for ancestor in path[:n-1]:
                            original = self.doc.entitydb.get(ancestor['start'])
                            if original is None or original.dxftype() != 'INSERT':
                                raise ValueError('Nested ancestor disappeared during clone.')
                            parent_m = parent_m @ original.matrix44()
                        inv = parent_m.copy()
                        inv.inverse()
                        new_ent.dxf.insert = inv.transform(Vec3(float(destination[0]), float(destination[1]), 0))
                    elif child_new_name and new_ent.dxftype() == 'INSERT':
                        new_ent.dxf.name = child_new_name
                    clone.add_entity(new_ent)
                else:
                    clone.add_entity(ent.copy())
            if not found:
                raise ValueError(f'Nested INSERT handle {child_handle} not found in block {source_name}.')
            child_new_name = new_name
        root.dxf.name = child_new_name
        self._nested_roots.add(root_handle)

    def delete(self, target):
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            self.drawing.msp.delete_entity(ent)
        else:
            self._clone_nested_path(target, 'DELETE')

    def move(self, target, destination):
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            ent.dxf.insert = Vec3(float(destination[0]), float(destination[1]), 0)
        else:
            self._clone_nested_path(target, 'MOVE', destination)

    def write(self, path):
        self.doc.saveas(str(path))
