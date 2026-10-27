"""White body study; measured envelope, provisional internal geometry. Requires cadquery."""
import json
from pathlib import Path
import cadquery as cq

OUT = Path(__file__).resolve().parent
data = json.loads((OUT / 'dimensions.json').read_text())
m, e = data['measured'], data['estimated']
W, D, H, T = (m[k] for k in ('width', 'depth', 'height', 'base_thickness'))
t = m['side_wall_thickness']
front_t = e['front_wall_thickness']
rear_t = m['rear_wall_thickness']
rim_h, side_h = m['rim_height'], m['side_rear_height']
side_end = D - rear_t
A, B, X, Y = (m[k] for k in ('opening_width', 'opening_depth', 'opening_left', 'opening_front'))
R = e['opening_radius']
assert abs(X + A + m['opening_right'] - W) < 1e-6
assert abs(Y + B + m['opening_rear'] - D) < 1e-6
assert 0 < T < H and 0 < t < min(W, D) / 2
assert 0 < rear_t < D
assert 0 < front_t < Y
assert t < X and X + A < W - t and Y + B < side_end
assert 0 < R < min(A, B) / 2
assert T < rim_h <= side_h <= H
assert 0 < e['side_ramp_depth'] < side_end

def box(x, y, z, dx, dy, dz):
    return cq.Workplane('XY').box(dx, dy, dz, centered=(False, False, False)).translate((x, y, z))

# Origin: front-left underside. X right, Y rear, Z up. Overall dimensions include walls.
base = box(0, 0, 0, W, D, T)
opening = box(X, Y, -1, A, B, T + 2).edges('|Z').fillet(R)
body = base.cut(opening)
rear = box(0, side_end, T, W, rear_t, H - T)
front = box(0, 0, T, W, front_t, rim_h - T)
body = body.union(rear).union(front)
# Provisional ramp length measured forward from the inner rear-wall face.
profile = [(0, T), (side_end, T), (side_end, side_h),
           (side_end - e['side_ramp_depth'], rim_h), (0, rim_h)]
side = cq.Workplane('YZ').polyline(profile).close().extrude(t)
assert abs(rear.val().BoundingBox().ylen - rear_t) < 1e-6
assert abs(front.val().BoundingBox().zmax - rim_h) < 1e-6
assert abs(side.val().BoundingBox().zmax - side_h) < 1e-6
assert abs(side.val().BoundingBox().xlen - t) < 1e-6
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
roundtrip_bb = reloaded.val().BoundingBox()
assert all(abs(a - b) < 1e-6 for a, b in zip(
    (roundtrip_bb.xlen, roundtrip_bb.ylen, roundtrip_bb.zlen), (W, D, H)))
report = {'status': 'DRAFT: unmeasured features are estimates', 'units': 'mm',
          'revision': data['revision'], 'opening_mm': [A, B],
          'side_wall_thickness_mm': t,
          'rear_wall_thickness_mm': rear_t, 'front_rim_total_height_mm': rim_h,
          'side_max_total_height_mm': side_h,
          'opening_to_rear_inner_face_mm': side_end - Y - B,
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
         f'Rear thickness {rear_t:.2f}; front height {rim_h:.2f}; side max {side_h:.2f} mm\n'
         f'Side thickness {t:.2f} mm; radii, front thickness and ramp length are provisional.',
         ha='center', fontsize=11)
fig.savefig(OUT / 'body_preview.png', bbox_inches='tight')
plt.close(fig)
