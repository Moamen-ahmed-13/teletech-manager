"""Write reviewed As-Built changes into a NEW DXF and prove what changed.

* The original DXF is never modified (SHA-256 is compared before/after).
* Direct (model-space) endpoints are deleted / moved in place on the copy.
* Nested endpoints (INSERT inside a block definition) use copy-on-write block
  clones (dxfedit.Editor) so other INSERTs sharing the same definition are not
  touched; several edits under one root INSERT share a single clone.
* Every failure is isolated and reported; nothing is silently skipped.
* verify_changes() recomputes the full recursive INSERT inventory of the original
  and of the output and checks that they differ by exactly the intended edits.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf.math import Matrix44, Vec3

import dxfedit

DELETE_OPS = {'DELETE_ENDPOINT', 'SITE_REMOVE'}
MOVE_OPS = {'MOVE_ENDPOINT'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


CLONE_NAME = 'TTAB_CLONE'


def _key(name, x, y):
    """Multiset key for an INSERT.  Private copy-on-write clones (TTAB_*) are folded
    into one canonical name: the rename itself is an intended, verified change."""
    name = str(name)
    if name.startswith('TTAB_'):
        name = CLONE_NAME
    return (name, round(float(x), 3), round(float(y), 3))


def enumerate_inserts(doc, max_depth=16):
    """Every INSERT reachable from model space with its world position.

    Returns {handle_path(tuple): (block_name, x, y)}.  Handle paths identify an
    instance (root handle, nested handle, ...); they differ after cloning, so the
    verification below compares multisets of (name, x, y) instead.
    """
    out = {}

    def walk(layout, parent_m, path, depth):
        if depth > max_depth:
            return
        for ent in layout:
            if ent.dxftype() != 'INSERT':
                continue
            try:
                m = ent.matrix44() @ parent_m   # row vectors: child first, then parent
                p = m.transform(Vec3(0, 0, 0))
            except Exception:
                continue
            hp = path + (str(ent.dxf.handle),)
            name = str(ent.dxf.name)
            out[hp] = (name, float(p.x), float(p.y))
            blk = doc.blocks.get(name) if name in doc.blocks else None
            if blk is not None:
                walk(blk, m, hp, depth + 1)

    walk(doc.modelspace(), Matrix44(), (), 0)
    return out


def _to_dxfedit_target(t):
    """Server target record -> dxfedit target (handle, depth, path)."""
    depth = int(t.get('depth') or 0)
    handle = str(t.get('handle') or '')
    tgt = {'handle': handle, 'depth': depth, 'name': t.get('block') or ''}
    if depth >= 1:
        ah = [str(h) for h in (t.get('ancestor_handles') or [])]
        ab = [str(b) for b in (t.get('ancestor_blocks') or t.get('ancestors') or [])]
        if len(ah) != depth or len(ab) != depth:
            raise ValueError(f'Incomplete ancestor chain for nested target {handle} '
                             f'(depth {depth}, {len(ah)} handles, {len(ab)} names).')
        tgt['path'] = [{'start': h, 'name': n} for h, n in zip(ah, ab)] + \
                      [{'start': handle, 'name': str(t.get('block') or '')}]
    return tgt


def apply_changes_to_dxf(src_dxf, out_dxf, items):
    """items: [{'op','target':<server target>,'x','y' (MOVE only),'label','color'}]

    Returns a report dict; see keys below.
    """
    src_dxf, out_dxf = str(src_dxf), str(out_dxf)
    if Path(src_dxf).resolve() == Path(out_dxf).resolve():
        raise ValueError('Refusing to overwrite the original DXF.')
    h_before = sha256(src_dxf)
    drawing = dxfedit.Dxf(src_dxf)
    doc = drawing.doc
    editor = dxfedit.Editor(drawing)
    before = enumerate_inserts(doc)

    report = {'source': src_dxf, 'output': out_dxf, 'applied': [], 'failed': [], 'superseded': [],
              'duplicates': []}
    seen = set()
    plan = []
    for it in items:
        op = str(it.get('op') or '').upper()
        t = it.get('target') or {}
        label = it.get('label') or t.get('label') or t.get('block') or t.get('target_id') or ''
        base = {'op': op, 'label': label, 'target_id': t.get('target_id'), 'handle': t.get('handle')}
        try:
            tgt = _to_dxfedit_target(t)
        except Exception as exc:
            report['failed'].append({**base, 'reason': str(exc)})
            continue
        ident = (tuple(p['start'] for p in tgt.get('path') or []) or (tgt['handle'],))
        if ident in seen:
            report['duplicates'].append({**base, 'reason': 'Same endpoint instance was requested twice.'})
            continue
        seen.add(ident)
        if op in MOVE_OPS:
            try:
                dest = (float(it['x']), float(it['y']))
            except Exception:
                report['failed'].append({**base, 'reason': 'MOVE needs numeric X/Y.'})
                continue
            plan.append((op, tgt, dest, base, ident))
            # where the symbol is DRAWN now (analysis x,y); falls back to the insertion point
            try:
                base['_center'] = (float(t['x']), float(t['y']))
            except Exception:
                base['_center'] = None
        elif op in DELETE_OPS:
            plan.append((op, tgt, None, base, ident))
        else:
            report['failed'].append({**base, 'reason': f'Unsupported operation {op}.'})

    # a deleted root INSERT makes nested edits under it moot (and would break the clone)
    deleted_roots = {ident[0] for op, tgt, dest, base, ident in plan
                     if op in DELETE_OPS and len(ident) == 1}
    final = []
    for entry in plan:
        op, tgt, dest, base, ident = entry
        if len(ident) > 1 and ident[0] in deleted_roots:
            report['superseded'].append({**base, 'reason': 'Its parent INSERT is deleted in this package.'})
            continue
        final.append(entry)

    expected_removed, expected_added = Counter(), Counter()
    ok_ops = []
    for op, tgt, dest, base, ident in final:
        try:
            if op in DELETE_OPS:
                editor.delete(tgt)
            else:
                wx0, wy0 = before[ident][1], before[ident][2] if ident in before else (None, None)
                cx, cy = (base.get('_center') or (wx0, wy0))
                if cx is None:
                    raise ValueError('Target instance not found in the original drawing.')
                # translate by (destination - current symbol position): independent of block
                # base points / internal offsets, so the SYMBOL lands on the destination.
                delta = (dest[0] - cx, dest[1] - cy)
                editor.move_by(tgt, delta)
                dest = delta           # from here on `dest` carries the displacement vector
            ok_ops.append((op, tgt, dest, base, ident))
        except Exception as exc:
            report['failed'].append({**base, 'reason': f'{type(exc).__name__}: {exc}'})

    # apply nested groups one root at a time so one bad group cannot sink the rest
    failed_roots = {}
    for root_handle, operations in list(editor._nested_ops.items()):
        try:
            editor._apply_nested_group(root_handle, operations)
        except Exception as exc:
            failed_roots[root_handle] = f'{type(exc).__name__}: {exc}'
    editor._nested_ops = {}
    cloned_prefixes = set()
    for op, tgt, dest, base, ident in ok_ops:
        if len(ident) > 1 and ident[0] in failed_roots:
            report['failed'].append({**base, 'reason': 'Nested clone failed: ' + failed_roots[ident[0]]})
            continue
        # expected effect on the recursive inventory
        subtree = [(p, v) for p, v in before.items() if p[:len(ident)] == ident]
        if not subtree:
            report['failed'].append({**base, 'reason': 'Target instance not found in the original drawing.'})
            continue
        wx, wy = before[ident][1], before[ident][2]
        for p, (nm, x, y) in subtree:
            expected_removed[_key(nm, x, y)] += 1
            if op in MOVE_OPS:
                expected_added[_key(nm, x + dest[0], y + dest[1])] += 1
        for k in range(1, len(ident)):          # every ancestor INSERT is re-pointed at a clone
            cloned_prefixes.add(ident[:k])
        entry = {**base, 'depth': len(ident) - 1}
        if op in MOVE_OPS:
            cxy = base.get('_center') or (wx, wy)
            entry.update(from_xy=[round(cxy[0], 3), round(cxy[1], 3)],
                         to_xy=[round(cxy[0] + dest[0], 3), round(cxy[1] + dest[1], 3)],
                         displacement=[round(dest[0], 3), round(dest[1], 3)])
        else:
            entry.update(at_xy=[round(wx, 3), round(wy, 3)])
        report['applied'].append(entry)

    for pref in cloned_prefixes:
        if pref in before:
            nm, x, y = before[pref]
            expected_removed[_key(nm, x, y)] += 1
            expected_added[_key(CLONE_NAME, x, y)] += 1
    report['cloned_instances'] = len(cloned_prefixes)

    doc.saveas(out_dxf)

    # ---- independent verification ----------------------------------------
    after = enumerate_inserts(ezdxf.readfile(out_dxf))
    exp = Counter(_key(*v) for v in before.values())
    exp.subtract(expected_removed)
    exp.update(expected_added)
    got = Counter(_key(*v) for v in after.values())
    diff_missing = {k: n for k, n in (exp - got).items() if n > 0}
    diff_extra = {k: n for k, n in (got - exp).items() if n > 0}
    h_after = sha256(src_dxf)
    report['verification'] = {
        'ok': (not diff_missing and not diff_extra and h_before == h_after),
        'original_unchanged': h_before == h_after,
        'original_sha256': h_before,
        'inserts_before': len(before), 'inserts_after': len(after),
        'unexpected_missing': [list(k) + [n] for k, n in list(diff_missing.items())[:20]],
        'unexpected_extra': [list(k) + [n] for k, n in list(diff_extra.items())[:20]],
    }
    for lst in ('applied', 'failed', 'superseded', 'duplicates'):
        for e in report[lst]:
            e.pop('_center', None)
    report['summary'] = {'applied': len(report['applied']), 'failed': len(report['failed']),
                         'superseded': len(report['superseded']), 'duplicates': len(report['duplicates'])}
    return report
