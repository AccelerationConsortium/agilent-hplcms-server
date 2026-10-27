"""White body study; measured envelope, provisional internal geometry. Requires cadquery."""
import json
from pathlib import Path
import cadquery as cq

OUT = Path(__file__).resolve().parent
data = json.loads((OUT / 'dimensions.json').read_text())
m, e = data['measured'], data['estimated']
W, D, H, T = (m[k] for k in ('width', 'depth', 'height', 'base_thickness'))
t = e['wall_thickness']
A, B, X, Y = (m[k] for k in ('opening_width', 'opening_depth', 'opening_left', 'opening_front'))
R = e['opening_radius']
assert abs(X + A + m['opening_right'] - W) < 1e-6
assert abs(Y + B + m['opening_rear'] - D) < 1e-6
assert 0 < T < H and 0 < t < min(W, D) / 2
assert t < X and X + A < W - t and t < Y and Y + B < D - t
assert 0 < R < min(A, B) / 2
assert T < e['rim_height'] <= e['side_rear_height'] <= H
assert 0 < e['side_ramp_depth'] < D - t

def box(x, y, z, dx, dy, dz):
    return cq.Workplane('XY').box(dx, dy, dz, centered=(False, False, False)).translate((x, y, z))

# Origin: front-left underside. X right, Y rear, Z up. Overall dimensions include walls.
base = box(0, 0, 0, W, D, T)
opening = box(X, Y, -1, A, B, T + 2).edges('|Z').fillet(R)
body = base.cut(opening)
body = body.union(box(0, D - t, T, W, t, H - T))
body = body.union(box(0, 0, T, W, t, e['rim_height'] - T))
profile = [(0, T), (D, T), (D, e['side_rear_height']),
           (D - e['side_ramp_depth'], e['rim_height']), (0, e['rim_height'])]
side = cq.Workplane('YZ').polyline(profile).close().extrude(t)
body = body.union(side).union(side.translate((W - t, 0, 0))).clean()
solid = body.val()
assert solid.isValid() and len(body.solids().vals()) == 1
bb = solid.BoundingBox()
assert all(abs(a - b) < 1e-6 for a, b in zip((bb.xlen, bb.ylen, bb.zlen), (W, D, H)))
cq.exporters.export(body, str(OUT / 'body_draft.step'))
cq.exporters.export(body, str(OUT / 'body_draft.stl'), tolerance=0.05, angularTolerance=0.1)
cq.exporters.export(body, str(OUT / 'body_preview.svg'), opt={
    'width': 900, 'height': 650, 'marginLeft': 40, 'marginTop': 40,
    'projectionDir': (1, -1.6, 1.2), 'showAxes': False,
    'showHidden': False, 'strokeWidth': 0.7, 'strokeColor': (35, 58, 76)})
reloaded = cq.importers.importStep(str(OUT / 'body_draft.step'))
assert reloaded.val().isValid() and len(reloaded.solids().vals()) == 1
report = {'status': 'DRAFT: unmeasured features are estimates', 'units': 'mm',
          'revision': data['revision'], 'opening_mm': [A, B],
          'margins_left_right_front_rear_mm': [X, W-X-A, Y, D-Y-B],
          'bounding_box': [bb.xlen, bb.ylen, bb.zlen], 'valid_solid': True,
          'solid_count': 1, 'step_roundtrip_valid': True, 'volume_mm3': solid.Volume()}
(OUT / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))

# Render the actual tessellated CAD solid for visual review.
import os
os.environ.setdefault('MPLCONFIGDIR', '/tmp/agilent-body-mpl')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
vertices, triangles = solid.tessellate(0.1)
points = [v.toTuple() for v in vertices]
faces = [[points[i] for i in tri] for tri in triangles]
fig = plt.figure(figsize=(10, 7), dpi=160)
ax = fig.add_subplot(111, projection='3d')
normals = np.cross(np.array(faces)[:, 1] - np.array(faces)[:, 0],
                   np.array(faces)[:, 2] - np.array(faces)[:, 0])
normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-12)
brightness = 0.60 + 0.38 * (np.abs(normals) @ np.array([0.35, 0.55, 0.76]))
colors = brightness[:, None] * np.array([0.86, 0.90, 0.93])
ax.add_collection3d(Poly3DCollection(faces, facecolors=colors, alpha=1))
ax.set(xlim=(0, W), ylim=(0, D), zlim=(0, H), xlabel='Width X (mm)',
       ylabel='Depth Y (mm)', zlabel='Height Z (mm)')
ax.set_box_aspect((W, D, H))
ax.view_init(elev=28, azim=-60)
fig.suptitle(f"WHITE BODY — DRAFT v{data['revision']}", fontsize=16)
fig.text(0.5, 0.05, f'Measured: W {W:.2f} / D {D:.2f} / H {H:.2f} / base T {T:.2f} mm\n'
         f'Opening {A:.2f} x {B:.2f} mm; left {X:.2f}, front {Y:.2f} mm\n'
         'Corner radii and side-wall geometry remain provisional.',
         ha='center', fontsize=11)
fig.savefig(OUT / 'body_preview.png', bbox_inches='tight')
plt.close(fig)
