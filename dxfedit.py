"""Safe DXF INSERT instance editing for the TELETECH As-Built pipeline.

Only simple, static INSERT chains are edited.  Nested edits use copy-on-write
block definitions so sibling INSERT instances and the original drawing remain
unchanged.  Multiple nested edits under one root are grouped into one clone.

Coordinate conventions (important - this is where earlier versions were wrong)
    DXF INSERT:   world = T(insert) . R(rot) . S(scale) . T(-block_base_point) . local
    ezdxf uses row vectors (v' = v @ M), so a child's matrix must be applied
    BEFORE its parent's:  M_world = M_child @ M_parent.
    ``Insert.matrix44()`` does not include the block base point; ``_insert_m``
    adds it so rotated / scaled / based blocks resolve to the correct world XY.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from collections import Counter

import ezdxf
from ezdxf.math import Matrix44, Vec3

ENDPOINT_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'D·V BOX', 'D.V BOX', 'DV BOX', 'EC-TP', 'ES-CCTV', 'ES-cctv'}   # legacy default for CLI listing only


def _handle(entity):
    return str(entity.dxf.handle or '')


def _block_base(doc, name):
    try:
        blk = doc.blocks.get(name)
        bp = blk.block.dxf.get('base_point', (0, 0, 0)) if blk is not None else (0, 0, 0)
        return Vec3(bp)
    except Exception:
        return Vec3(0, 0, 0)


def _insert_m(ent):
    """local(block coords) -> parent coords, including the block base point."""
    base = _block_base(ent.doc, str(ent.dxf.name))
    return Matrix44.translate(-base.x, -base.y, -base.z) @ ent.matrix44()


def _is_static_insert(ins):
    if ins.dxftype() != 'INSERT' or ins.doc is None:
        return False
    name = str(ins.dxf.name or '')
    if not name or name.startswith('*'):
        return False
    block = ins.doc.blocks.get(name)
    if block is None:
        return False
    flags = int(block.block.dxf.get('flags', 0))
    return not (flags & (1 | 4 | 8 | 16))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class Dxf:
    def __init__(self, path):
        self.path = str(path)
        self.doc = ezdxf.readfile(self.path)
        self.msp = self.doc.modelspace()

    # -- enumeration -----------------------------------------------------------
    def instances(self, names=ENDPOINT_BLOCKS):
        """Every static INSERT reachable from model space.

        names: set of block names (case-insensitive) to keep (default ENDPOINT_BLOCKS);
        pass '*' for every INSERT.
        Record: name, depth, path[{start,name}], handle, world (insert point XY),
        local, root_handle, entity.
        """
        want = None if (names is None or names == '*') else {n.upper() for n in names}
        found = []

        def walk(layout, parent_m, path, depth):
            if depth > 16:
                return
            for ent in layout:
                if ent.dxftype() != 'INSERT' or not _is_static_insert(ent):
                    continue
                try:
                    world_m = _insert_m(ent) @ parent_m
                    p = _insert_point_world(ent, parent_m)
                except Exception:
                    continue
                name = str(ent.dxf.name)
                rec_path = path + [{'start': _handle(ent), 'name': name}]
                if want is None or name.upper() in want:
                    found.append({'name': name, 'world': [float(p.x), float(p.y)], 'depth': depth,
                                  'path': rec_path, 'handle': _handle(ent),
                                  'local': [float(ent.dxf.insert.x), float(ent.dxf.insert.y)],
                                  'root_handle': rec_path[0]['start'], 'entity': ent})
                try:
                    walk(self.doc.blocks.get(name), world_m, rec_path, depth + 1)
                except Exception:
                    pass

        walk(self.msp, Matrix44(), [], 0)
        return found

    def resolve(self, handles):
        """Build an edit target from a handle chain [root, ..., target].

        Verifies each entity is a static INSERT and that each one lives inside the
        previous one's block definition.  Raises ValueError otherwise.
        """
        handles = [str(h) for h in handles if str(h)]
        if not handles:
            raise ValueError('Empty handle path.')
        parent_m = Matrix44()
        path = []
        prev_block = None
        p = None
        for depth, h in enumerate(handles):
            ent = self.doc.entitydb.get(h)
            if ent is None or ent.dxftype() != 'INSERT':
                raise ValueError(f'Handle {h} is not an INSERT in this drawing.')
            if not _is_static_insert(ent):
                raise ValueError(f'INSERT {h} ({ent.dxf.name}) is dynamic/anonymous/xref; not editable.')
            if depth == 0:
                if ent.get_layout() is None or ent.get_layout().name.lower() != 'model':
                    raise ValueError(f'Root INSERT {h} is not in model space.')
            else:
                blk = self.doc.blocks.get(prev_block)
                if blk is None or ent.dxf.owner != blk.block_record_handle:
                    raise ValueError(f'INSERT {h} does not belong to block {prev_block}.')
            p = _insert_point_world(ent, parent_m)
            parent_m_next = _insert_m(ent) @ parent_m
            path.append({'start': h, 'name': str(ent.dxf.name)})
            prev_block = str(ent.dxf.name)
            parent_m = parent_m_next
        ent = self.doc.entitydb.get(handles[-1])
        return {'name': str(ent.dxf.name), 'world': [float(p.x), float(p.y)], 'depth': len(handles) - 1,
                'path': path, 'handle': handles[-1], 'root_handle': handles[0],
                'local': [float(ent.dxf.insert.x), float(ent.dxf.insert.y)], 'entity': ent}


def _insert_point_world(ent, parent_m):
    """World position of the INSERT's insertion point (not the block base)."""
    return parent_m.transform(Vec3(float(ent.dxf.insert.x), float(ent.dxf.insert.y), 0.0))


class Editor:
    def __init__(self, drawing):
        self.drawing = drawing
        self.doc = drawing.doc
        self._nested_ops = {}
        self._applied = False

    # -- queueing ----------------------------------------------------------------
    def _queue_nested(self, target, action, destination=None):
        path = list(target.get('path') or [])
        if len(path) < 2:
            raise ValueError('Nested target path is incomplete.')
        root_handle = path[0]['start']
        root = self.doc.entitydb.get(root_handle)
        if root is None or root.dxftype() != 'INSERT' or not _is_static_insert(root):
            raise ValueError('Nested root INSERT is missing or unsupported.')
        for op in self._nested_ops.get(root_handle, []):
            if op['path'][-1]['start'] == path[-1]['start']:
                raise ValueError(f"Conflicting edits for INSERT {path[-1]['start']}.")
        self._nested_ops.setdefault(root_handle, []).append(
            {'path': path, 'action': action, 'destination': destination})

    def delete(self, target):
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            self.drawing.msp.delete_entity(ent)
        else:
            self._queue_nested(target, 'DELETE')

    def move(self, target, destination):
        """Move the INSERT so its *insertion point* lands on ``destination`` (world XY)."""
        if int(target.get('depth', 0)) == 0:
            ent = self.doc.entitydb.get(str(target.get('handle') or ''))
            if ent is None:
                raise ValueError('Target INSERT handle not found.')
            ent.dxf.insert = Vec3(float(destination[0]), float(destination[1]), float(ent.dxf.insert.z))
        else:
            self._queue_nested(target, 'MOVE', destination)

    # -- nested copy-on-write ------------------------------------------------------
    def _apply_nested_group(self, root_handle, operations):
        root = self.doc.entitydb.get(root_handle)
        if root is None or root.dxftype() != 'INSERT':
            raise ValueError(f'Nested root INSERT {root_handle} disappeared before export.')
        actions, needed_children = {}, {}
        for op in operations:
            path = op['path']
            key = (path[-2]['name'], path[-1]['start'])
            if key in actions:
                raise ValueError(f"Conflicting nested actions for handle {path[-1]['start']}.")
            local_destination = None
            if op['action'] == 'MOVE':
                # world -> coordinates of the parent block of the target
                parent_m = Matrix44()
                for anc in path[:-1]:
                    ent = self.doc.entitydb.get(anc['start'])
                    if ent is None or ent.dxftype() != 'INSERT':
                        raise ValueError('Nested ancestor disappeared during export.')
                    parent_m = _insert_m(ent) @ parent_m
                inv = parent_m.copy()
                inv.inverse()
                local_destination = inv.transform(Vec3(float(op['destination'][0]), float(op['destination'][1]), 0.0))
            actions[key] = (op['action'], local_destination)
            for i in range(len(path) - 1):
                needed_children[(path[i]['name'], path[i + 1]['start'])] = path[i + 1]['name']

        def clone_block(source_name, depth=0):
            if depth > 16:
                raise ValueError('Nested block depth exceeds safe clone limit.')
            source = self.doc.blocks.get(source_name)
            if source is None:
                raise ValueError(f'Block {source_name} is unavailable.')
            new_name = f'TTAB_{uuid.uuid4().hex[:12]}'
            clone = self.doc.blocks.new(name=new_name, base_point=source.block.dxf.get('base_point', (0, 0, 0)))
            for ent in source:
                key = (source_name, _handle(ent))
                if key in actions:
                    action, local_dest = actions[key]
                    if action == 'DELETE':
                        continue
                    copied = ent.copy()
                    copied.dxf.insert = Vec3(local_dest.x, local_dest.y, float(ent.dxf.insert.z))
                    clone.add_entity(copied)
                    continue
                copied = ent.copy()
                child_name = needed_children.get(key)
                if child_name and copied.dxftype() == 'INSERT':
                    copied.dxf.name = clone_block(child_name, depth + 1)
                clone.add_entity(copied)
            return new_name

        root.dxf.name = clone_block(str(root.dxf.name))

    def write(self, path):
        path = os.path.abspath(str(path))
        if os.path.abspath(self.drawing.path) == path:
            raise ValueError('Refusing to overwrite the original DXF.')
        if not self._applied:
            for root_handle, operations in self._nested_ops.items():
                self._apply_nested_group(root_handle, operations)
            self._applied = True
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.doc.saveas(path)


# ------------------------------------------------------------------------------
# post-write verification
# ------------------------------------------------------------------------------
def _signature(inst):
    return (inst['name'], round(inst['world'][0], 1), round(inst['world'][1], 1))


def instance_multiset(path):
    return Counter(_signature(i) for i in Dxf(path).instances('*'))


def _block_fingerprint(doc):
    return {b.name: len(list(b)) for b in doc.blocks if not b.name.startswith('*')}


def verify_output(original_path, output_path, deletes, moves, tol=0.2):
    """Independently re-read both files and prove the edit is exactly what was asked.

    deletes: [{'path': [handle, ...]}]               INSERT chains removed
    moves:   [{'path': [handle, ...], 'delta': [dx, dy]}]   world shift of that INSERT
    Expected output instances = original - (deleted INSERTs and ALL their
    descendants) with moved INSERTs (and their descendants) shifted by delta.
    Every other INSERT must be identical, no other instance may appear, every
    original block definition must be untouched, and model-space geometry
    (non-INSERT entities) must be unchanged.
    """
    problems = []
    o = Dxf(original_path)
    orig = o.instances('*')
    del_paths = [tuple(str(h) for h in d['path']) for d in deletes]
    mv = [(tuple(str(h) for h in m['path']), m['delta']) for m in moves]
    edit_paths = del_paths + [m for m, _ in mv]
    expect = Counter()
    for inst in orig:
        p = tuple(x['start'] for x in inst['path'])
        if any(p[:len(dp)] == dp for dp in del_paths):
            continue
        dx = dy = 0.0
        for mp, delta in mv:
            if p[:len(mp)] == mp:
                dx, dy = delta
        # ancestors of an edited INSERT are re-pointed to private TTAB_ clones by design
        is_anc = any(len(p) < len(ep) and ep[:len(p)] == p for ep in edit_paths)
        expect[('*TTAB*' if is_anc else inst['name'],
                round(inst['world'][0] + dx, 1), round(inst['world'][1] + dy, 1))] += 1
    out = Dxf(output_path)
    after = Counter(('*TTAB*' if i['name'].startswith('TTAB_') else i['name'],
                     round(i['world'][0], 1), round(i['world'][1], 1)) for i in out.instances('*'))
    missing, extra = expect - after, after - expect
    for sig, n in list(missing.items()):          # tolerate rounding at the 0.1 boundary
        for sig2, n2 in list(extra.items()):
            if sig2[0] == sig[0] and abs(sig2[1] - sig[1]) <= tol and abs(sig2[2] - sig[2]) <= tol:
                k = min(missing[sig], extra[sig2])
                missing[sig] -= k
                extra[sig2] -= k
    for sig, n in (+missing).items():
        problems.append(f'expected INSERT missing from output: {sig} x{n}')
    for sig, n in (+extra).items():
        problems.append(f'unexpected INSERT in output: {sig} x{n}')
    # original block definitions untouched (clones are NEW blocks named TTAB_*)
    fo, fa = _block_fingerprint(o.doc), _block_fingerprint(out.doc)
    changed = [n for n, c in fo.items() if fa.get(n) != c]
    if changed:
        problems.append(f'original block definitions were modified: {changed[:5]}')
    # model-space geometry other than INSERTs unchanged
    def geo(d):
        return Counter(e.dxftype() for e in d.msp if e.dxftype() != 'INSERT')
    if geo(o) != geo(out):
        problems.append('model-space geometry (non-INSERT) changed')
    return {'ok': not problems, 'problems': problems[:40], 'original_count': len(orig),
            'output_count': sum(after.values()), 'deleted_paths': len(del_paths), 'moved_paths': len(mv)}


# ------------------------------------------------------------------------------
# symbol footprint (used to decide which symbol a highlighter really covers)
# ------------------------------------------------------------------------------
def footprint_points(drawing, handles, max_points=160):
    """World XY sample points of the drawn geometry of one INSERT (nested ok)."""
    from ezdxf import disassemble
    import numpy as np
    handles = [str(h) for h in handles if str(h)]
    ent = drawing.doc.entitydb.get(handles[-1])
    if ent is None or ent.dxftype() != 'INSERT':
        return np.zeros((0, 2))
    parent_m = Matrix44()
    for h in handles[:-1]:
        a = drawing.doc.entitydb.get(h)
        if a is None or a.dxftype() != 'INSERT':
            return np.zeros((0, 2))
        parent_m = _insert_m(a) @ parent_m
    pts = []
    try:
        for prim in disassemble.to_primitives(disassemble.recursive_decompose(ent.virtual_entities())):
            if prim.is_empty:
                continue
            for v in prim.vertices():
                w = parent_m.transform(Vec3(v.x, v.y, 0.0))
                pts.append((w.x, w.y))
    except Exception:
        pass
    if not pts:
        return np.zeros((0, 2))
    arr = np.asarray(pts, float)
    if len(arr) > max_points:
        arr = arr[np.linspace(0, len(arr) - 1, max_points).astype(int)]
    return arr
