"""Safe DXF INSERT instance editing for the TELETECH As-Built pipeline.

Only simple, static INSERT chains are edited. Nested edits use copy-on-write
block definitions so sibling INSERT instances and the original drawing remain
unchanged. Multiple nested edits under one root are grouped into one clone.
"""
from __future__ import annotations
import uuid
import ezdxf
from ezdxf.math import Matrix44, Vec3

ENDPOINT_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'D·V BOX', 'D.V BOX', 'DV BOX', 'EC-TP', 'ES-CCTV'}

def _handle(entity):
    return str(entity.dxf.handle or '')

def _is_static_insert(ins):
    if ins.dxftype() != 'INSERT' or ins.doc is None:
        return False
    name = str(ins.dxf.name or '')
    if not name or name.startswith('*'):
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
            if depth > 16:
                return
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
                    found.append({'name': name, 'world': [float(p.x), float(p.y)],
                                  'depth': depth, 'path': rec_path, 'handle': _handle(ent),
                                  'local': [float(ent.dxf.insert.x), float(ent.dxf.insert.y)],
                                  'root_handle': rec_path[0]['start'], 'entity': ent})
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
        self._nested_ops = {}

    def _queue_nested(self, target, action, destination=None):
        path = list(target.get('path') or [])
        if len(path) < 2:
            raise ValueError('Nested target path is incomplete.')
        root_handle = path[0]['start']
        root = self.doc.entitydb.get(root_handle)
        if root is None or root.dxftype() != 'INSERT' or not _is_static_insert(root):
            raise ValueError('Nested root INSERT is missing or unsupported.')
        self._nested_ops.setdefault(root_handle, []).append(
            {'path': path, 'action': action, 'destination': destination})

    def _apply_nested_group(self, root_handle, operations):
        root = self.doc.entitydb.get(root_handle)
        if root is None or root.dxftype() != 'INSERT':
            raise ValueError(f'Nested root INSERT {root_handle} disappeared before export.')
        actions = {}
        needed_children = {}
        for op in operations:
            path = op['path']
            parent_name = path[-2]['name']
            target_handle = path[-1]['start']
            key = (parent_name, target_handle)
            if key in actions:
                raise ValueError(f'Conflicting nested actions for handle {target_handle}.')
            local_destination = None
            if op['action'] == 'MOVE':
                parent_m = Matrix44()
                for ancestor in path[:-1]:
                    ent = self.doc.entitydb.get(ancestor['start'])
                    if ent is None or ent.dxftype() != 'INSERT':
                        raise ValueError('Nested ancestor disappeared during export.')
                    parent_m = parent_m @ ent.matrix44()
                inv = parent_m.copy()
                inv.inverse()
                local_destination = inv.transform(Vec3(float(op['destination'][0]), float(op['destination'][1]), 0))
            actions[key] = (op['action'], local_destination)
            for idx in range(len(path)-1):
                needed_children[(path[idx]['name'], path[idx+1]['start'])] = path[idx+1]['name']

        def clone_block(source_name, depth=0):
            if depth > 16:
                raise ValueError('Nested block depth exceeds safe clone limit.')
            source = self.doc.blocks.get(source_name)
            if source is None:
                raise ValueError(f'Block {source_name} is unavailable.')
            new_name = f'TTAB_{uuid.uuid4().hex[:12]}'
            clone = self.doc.blocks.new(name=new_name)
            for ent in source:
                key = (source_name, _handle(ent))
                if key in actions:
                    action, local_dest = actions[key]
                    if action == 'DELETE':
                        continue
                    copied = ent.copy()
                    copied.dxf.insert = local_dest
                    clone.add_entity(copied)
                    continue
                copied = ent.copy()
                child_name = needed_children.get(key)
                if child_name and copied.dxftype() == 'INSERT':
                    copied.dxf.name = clone_block(child_name, depth + 1)
                clone.add_entity(copied)
            return new_name

        root.dxf.name = clone_block(str(root.dxf.name))

    def delete(self, target):
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            self.drawing.msp.delete_entity(ent)
        else:
            self._queue_nested(target, 'DELETE')

    def move(self, target, destination):
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            ent.dxf.insert = Vec3(float(destination[0]), float(destination[1]), 0)
        else:
            self._queue_nested(target, 'MOVE', destination)

    def write(self, path):
        for root_handle, operations in self._nested_ops.items():
            self._apply_nested_group(root_handle, operations)
        self.doc.saveas(str(path))
