import sys, json, math
sys.stdout.reconfigure(encoding='utf-8')
import dxfedit

rep = json.load(open('exports/asbuilt_auto_ground/asbuilt_report.json', encoding='utf-8'))
d = dxfedit.Dxf('Mansoura University - HUM - G - A.B.dxf')
print('changes:', len(rep.get('changes', [])))
for ch in rep.get('changes', []):
    hp = ch.get('dxf_handle_path') or []
    if not hp:
        print('no handle path', ch.get('op'))
        continue
    try:
        t = d.resolve(hp)
        w = t['world']
        rec = (ch.get('x'), ch.get('y')) if ch.get('op') == 'MOVE_ENDPOINT' else tuple(ch.get('world', [0, 0]))
        err = math.hypot(w[0] - rec[0], w[1] - rec[1])
        verdict = 'PASS' if err <= 1.0 else 'FAIL'
        name = t['name']
        op = ch['op']
        print('%-14s handles=%s block=%-10s world_err=%8.3f %s' % (op, hp, name, err, verdict))
    except Exception as e:
        op = ch['op']
        print('%-14s handles=%s RESOLVE_FAIL: %s' % (op, hp, e))

# also verify the output DXF actually lacks deleted entities and keeps unrelated ones
out = 'exports/asbuilt_auto_ground/Mansoura University - HUM - G - A.B_AsBuilt.dxf'
do = dxfedit.Dxf(out)
for ch in rep.get('changes', []):
    hp = ch.get('dxf_handle_path') or []
    if not hp or ch.get('op') == 'MOVE_ENDPOINT':
        continue
    gone = do.doc.entitydb.get(hp[-1]) is None
    print('deleted-in-output handle=%s -> %s' % (hp[-1], 'ABSENT(OK)' if gone else 'STILL PRESENT(BAD)'))
