"""White body study; measured envelope, provisional internal geometry. Requires cadquery."""
import json
from pathlib import Path
import cadquery as cq

OUT = Path(__file__).resolve().parent
data = json.loads((OUT / 'dimensions.json').read_text())
m, e = data['measured'], data['estimated']
W, D, H, T = (m[k] for k in ('width', 'depth', 'height', 'base_thickness'))
t = m['side_wall_thickness']
front_t = m['front_wall_thickness']
rear_t = m['rear_wall_thickness']
rim_h, side_h = m['rim_height'], m['left_side_rear_height']
ledge_h = m['rear_ledge_height']
ledge_start = D - m['rear_overall_depth']
side_end = D - rear_t
ramp_start = m['side_ramp_start_from_front']
flat_length, flat_h = m['right_flat_length'], m['right_flat_height']
flat_start = m['right_flat_start']
flat_end = flat_start + flat_length
step_rise = m['right_vertical_rise']
step_top = flat_h + step_rise
right_end_h = e['right_rear_height']
A, B, X, Y = (m[k] for k in ('opening_width', 'opening_depth', 'opening_left', 'opening_front'))
R = e['opening_radius']
wing_extension = m['left_wing_extension']
wing_length = m['left_wing_root_length']
wing_t = m['left_wing_thickness']
assert data['constraints']['left_wing_bottom_aligned_to_left_ramp_top']
wing_bottom = side_h
wing_top = wing_bottom + wing_length
assert wing_extension > 0 and 0 < wing_t <= rear_t and 0 <= wing_bottom < wing_top <= H
assert abs(X + A + data['derived']['opening_right'] - W) < 1e-6
slots = data['features']['rear_slots']
slot_h = m['rear_slot_shoulder_height']
slot_center_h = m['rear_slot_center_height']
slot_center_w = m['rear_slot_center_width']
slot_depth = m['rear_slot_depth']
assert 0 < slot_h < slot_center_h < ledge_h
assert 0 < slot_depth < m['rear_overall_depth']
assert 0 < slot_center_w < min(m['rear_slot_widths'])
assert abs(slots[0]['left_offset'] - m['rear_first_slot_left_offset']) < 1e-6
for i, slot in enumerate(slots):
    assert abs(slot['width'] - m['rear_slot_widths'][i]) < 1e-6
    if i:
        assert abs(slot['left_offset'] - slots[i-1]['right_edge'] - m['rear_slot_gaps'][i-1]) < 1e-6
assert abs(W - slots[-1]['right_edge'] - data['derived']['rear_last_slot_right_margin']) < 1e-6
assert abs(Y + B + m['opening_rear'] - D) < 1e-6
assert 0 < T < H and 0 < t < min(W, D) / 2
assert 0 < rear_t < D
assert 0 < front_t < Y
assert t < X and X + A < W - t and Y + B < side_end
assert 0 < R < min(A, B) / 2
assert T < rim_h <= side_h <= H
assert 0 < ramp_start < side_end
assert ramp_start < flat_start < side_end and rim_h < flat_h <= side_h
assert flat_end < side_end and step_top < right_end_h < H
assert Y+B < ledge_start < side_end and T < ledge_h < H

def box(x, y, z, dx, dy, dz):
    return cq.Workplane('XY').box(dx, dy, dz, centered=(False, False, False)).translate((x, y, z))

# Origin: front-left underside. X right, Y rear, Z up. Overall dimensions include walls.
base = box(0, 0, 0, W, D, T)
opening = box(X, Y, -1, A, B, T + 2).edges('|Z').fillet(R)
body = base.cut(opening)
rear = box(0, side_end, T, W, rear_t, H - T)
front = box(0, 0, T, W, front_t, rim_h - T)
ledge = box(t, ledge_start, T, W-2*t, side_end-ledge_start, ledge_h-T)
body = body.union(rear).union(front).union(ledge)
# Left rises directly; right has a flat, vertical step, and a provisional final rise.
profile = [(0, T), (side_end, T), (side_end, side_h),
           (ramp_start, rim_h), (0, rim_h)]
side = cq.Workplane('YZ').polyline(profile).close().extrude(t)
right_profile = [(0, T), (side_end, T), (side_end, right_end_h),
                 (flat_end, step_top), (flat_end, flat_h),
                 (flat_start, flat_h), (ramp_start, rim_h), (0, rim_h)]
right_side = cq.Workplane('YZ').polyline(right_profile).close().extrude(t).translate((W-t, 0, 0))
assert abs(rear.val().BoundingBox().ylen - rear_t) < 1e-6
assert abs(front.val().BoundingBox().zmax - rim_h) < 1e-6
assert abs(front.val().BoundingBox().ylen - front_t) < 1e-6
assert abs(side.val().BoundingBox().zmax - side_h) < 1e-6
assert abs(side.val().BoundingBox().xlen - t) < 1e-6
assert any(abs(v.Y - ramp_start) < 1e-6 and abs(v.Z - rim_h) < 1e-6
           for v in side.val().Vertices())
assert abs(right_side.val().BoundingBox().zmax - right_end_h) < 1e-6
assert abs(ledge.val().BoundingBox().zmax - ledge_h) < 1e-6
assert abs(ledge.val().BoundingBox().ylen + rear_t - m['rear_overall_depth']) < 1e-6
assert any(abs(edge.Length() - flat_length) < 1e-6
           and all(abs(v.Z - flat_h) < 1e-6 for v in edge.Vertices())
           for edge in right_side.val().Edges())
body = body.union(side).union(right_side).clean()
# Inner lower edge aligned with the left ramp endpoint; root length remains measured.
wing = box(-wing_extension, D-wing_t, wing_bottom, wing_extension, wing_t, wing_length)
assert abs(wing.val().BoundingBox().zmin - side.val().BoundingBox().zmax) < 1e-6
body = body.union(wing).clean()
uncut_volume = body.val().Volume()
expected_removed = 0.0
for slot in slots:
    x, width = slot['left_offset'], slot['width']
    center_x = x + (width-slot_center_w)/2
    full_cut = box(x, D-slot_depth, -1, width, slot_depth+1, slot_h+1)
    middle_cut = box(center_x, D-slot_depth, slot_h, slot_center_w,
                     slot_depth+1, slot_center_h-slot_h)
    body = body.cut(full_cut.union(middle_cut))
    expected_removed += slot_depth*(width*slot_h+slot_center_w*(slot_center_h-slot_h))
body = body.clean()
solid = body.val()
assert abs(uncut_volume-solid.Volume()-expected_removed) < 1e-5
for slot in slots:
    x, width = slot['left_offset'], slot['width']
    center_x, mid_y = x+width/2, D-slot_depth/2
    # Both levels are empty; shoulders, inner floor and roof remain solid.
    assert not solid.isInside(cq.Vector(center_x, mid_y, slot_center_h-0.1), 1e-6)
    assert not solid.isInside(cq.Vector(x+0.5, mid_y, slot_h-0.1), 1e-6)
    assert solid.isInside(cq.Vector(x+0.5, mid_y, slot_h+0.1), 1e-6)
    assert solid.isInside(cq.Vector(center_x, D-slot_depth-0.1, slot_h/2), 1e-6)
    assert solid.isInside(cq.Vector(center_x, mid_y, slot_center_h+0.1), 1e-6)
assert any(abs(edge.Length()-step_rise) < 1e-6
           and all(abs(v.Y-flat_end) < 1e-6 and abs(v.X-W) < 1e-6 for v in edge.Vertices())
           for edge in solid.Edges())
assert solid.isValid() and len(body.solids().vals()) == 1
bb = solid.BoundingBox()
overall_width = W + wing_extension
assert all(abs(a - b) < 1e-6 for a, b in zip((bb.xlen, bb.ylen, bb.zlen), (overall_width, D, H)))
assert abs(bb.xmin + wing_extension) < 1e-6
assert abs(uncut_volume - (base.cut(opening).union(rear).union(front).union(ledge).union(side).union(right_side).val().Volume() + wing_extension*wing_t*wing_length)) < 1e-5
cq.exporters.export(body, str(OUT / 'body_draft.step'))
cq.exporters.export(body, str(OUT / 'body_draft.stl'), tolerance=0.05, angularTolerance=0.1)
cq.exporters.export(body, str(OUT / 'body_preview.svg'), opt={
    'width': 900, 'height': 650, 'marginLeft': 40, 'marginTop': 40,
    'projectionDir': (1, -1.6, 1.2), 'showAxes': False,
    'showHidden': False, 'strokeWidth': 0.7, 'strokeColor': (35, 58, 76)})
reloaded = cq.importers.importStep(str(OUT / 'body_draft.step'))
assert reloaded.val().isValid() and len(reloaded.solids().vals()) == 1
assert abs(reloaded.val().Volume()-solid.Volume()) < 1e-5
roundtrip_bb = reloaded.val().BoundingBox()
assert all(abs(a - b) < 1e-6 for a, b in zip(
    (roundtrip_bb.xlen, roundtrip_bb.ylen, roundtrip_bb.zlen), (overall_width, D, H)))
report = {'status': 'DRAFT: unmeasured features are estimates', 'units': 'mm',
          'revision': data['revision'], 'opening_mm': [A, B],
          'rear_slot_outlines_only': False,
          'rear_slots_cut': len(slots),
          'rear_slot_shoulder_height_center_height_center_width_depth_mm': [slot_h, slot_center_h, slot_center_w, slot_depth],
          'rear_slot_removed_volume_mm3': uncut_volume-solid.Volume(),
          'rear_slot_void_shoulders_and_inner_floor_verified': True,
          'rear_slot_x_ranges_mm': [[s['left_offset'], s['right_edge']] for s in slots],
          'body_width_excluding_wing_mm': W,
          'left_wing_extension_root_length_thickness_mm': [wing_extension, wing_length, wing_t],
          'left_wing_z_range_mm': [wing_bottom, wing_top],
          'left_wing_lower_edge_aligned_to_ramp': True,
          'left_wing_top_below_rear_top_mm': H-wing_top,
          'side_wall_thickness_mm': t,
          'front_wall_thickness_mm': front_t,
          'side_ramp_start_from_front_mm': ramp_start,
          'right_flat_length_mm': flat_length, 'right_flat_height_mm': flat_h,
          'right_flat_start_from_front_mm': flat_start,
          'right_vertical_rise_mm': step_rise, 'right_step_top_height_mm': step_top,
          'estimated_right_rear_height_mm': right_end_h,
          'rear_ledge_height_mm': ledge_h, 'rear_ledge_depth_mm': side_end-ledge_start,
          'assumed_side_ramp_end_from_front_mm': side_end,
          'rear_wall_thickness_mm': rear_t, 'front_rim_total_height_mm': rim_h,
          'left_side_max_total_height_mm': side_h,
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
fig.subplots_adjust(bottom=0.28, top=0.94)
ax = fig.add_subplot(111, projection='3d')
normals = np.cross(np.array(faces)[:, 1] - np.array(faces)[:, 0],
                   np.array(faces)[:, 2] - np.array(faces)[:, 0])
normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-12)
brightness = 0.60 + 0.38 * (np.abs(normals) @ np.array([0.35, 0.55, 0.76]))
colors = brightness[:, None] * np.array([0.86, 0.90, 0.93])
ax.add_collection3d(Poly3DCollection(faces, facecolors=colors, alpha=1))
ax.set(xlim=(-wing_extension, W), ylim=(0, D), zlim=(0, H), xlabel='Width X (mm)',
       ylabel='Depth Y (mm)', zlabel='Height Z (mm)')
ax.set_box_aspect((overall_width, D, H))
ax.view_init(elev=28, azim=-60)
fig.suptitle(f"WHITE BODY — DRAFT v{data['revision']}", fontsize=16)
fig.text(0.5, 0.05, f'Measured: W {W:.2f} / D {D:.2f} / H {H:.2f} / base T {T:.2f} mm\n'
         f'Opening {A:.2f} x {B:.2f} mm; left {X:.2f}, front {Y:.2f} mm\n'
         f'Rear plate {rear_t:.2f}; ledge height {ledge_h:.2f}; combined depth {m["rear_overall_depth"]:.2f} mm\n'
         f'Front/side thickness {front_t:.2f}/{t:.2f}; ramp starts at Y={ramp_start:.2f} mm\n'
         f'Right flat {flat_length:.2f} at Z={flat_h:.2f}, then rise {step_rise:.2f} mm\n'
         f'Left wing {wing_extension:.2f} x {wing_length:.2f} x {wing_t:.2f} mm; overall width {overall_width:.2f}\n'
         f'Wing lower edge aligned to left ramp at Z={wing_bottom:.2f}; final right ramp and radii provisional.',
         ha='center', fontsize=9)
fig.savefig(OUT / 'body_preview.png', bbox_inches='tight')
ax.view_init(elev=10, azim=90)
fig.savefig(OUT / 'rear_preview.png', bbox_inches='tight')
plt.close(fig)

fig, ax = plt.subplots(figsize=(9, 4), dpi=160)
ax.plot([0, ramp_start, side_end], [rim_h, rim_h, side_h],
        label='Left side', color='#2474a5', linewidth=2)
ax.plot([0, ramp_start, flat_start, flat_end, flat_end], [rim_h, rim_h, flat_h, flat_h, step_top],
        label='Right side', color='#d57322', linewidth=2)
ax.plot([flat_end, side_end], [step_top, right_end_h], '--', color='#d57322', label='Right final ramp: provisional')
ax.annotate('Flat 10.00 mm', xy=((flat_start+flat_end)/2, flat_h),
            xytext=(73, 25), arrowprops={'arrowstyle': '->'})
ax.annotate('Rise 10.00 mm', xy=(flat_end, (flat_h+step_top)/2), xytext=(80, 36), arrowprops={'arrowstyle':'->'})
ax.scatter([ramp_start, flat_start, flat_end, flat_end], [rim_h, flat_h, flat_h, step_top], color='#d57322')
ax.set(xlim=(55, D), ylim=(0, 40), xlabel='Distance from front (mm)',
       ylabel='Height from underside (mm)', title=f'v{data["revision"]} side-wall top profiles — rear region')
ax.axvline(side_end, color='gray', linestyle=':', label=f'Inner rear-plate face: {side_end:.2f} mm')
ax.grid(alpha=0.25)
ax.legend(loc='upper left', fontsize=8)
fig.tight_layout()
fig.savefig(OUT / 'side_profiles.png')
plt.close(fig)

# Keep the requested Tailscale preview in sync with subsequent CAD revisions.
preview_sync = OUT / 'preview' / 'sync_model.py'
if preview_sync.exists():
    import runpy
    runpy.run_path(str(preview_sync), run_name='__main__')
