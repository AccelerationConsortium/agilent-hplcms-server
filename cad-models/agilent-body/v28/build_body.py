"""White body study; measured envelope, provisional internal geometry. Requires cadquery."""
import json
import math
from pathlib import Path
import cadquery as cq

OUT = Path(__file__).resolve().parent
data = json.loads((OUT / 'dimensions.json').read_text())
m, e = data['measured'], data['estimated']
W, D, H, T = (m[k] for k in ('width', 'depth', 'height', 'base_thickness'))
t = m['side_wall_thickness']
front_t = m['front_wall_thickness']
rear_t = m['rear_wall_thickness']
rim_h = m['rim_height']
side_h = rim_h + (D-rear_t-m['side_ramp_start_from_front'])*math.tan(math.radians(m['left_slope_angle_deg']))
ledge_h = m['rear_ledge_height']
ledge_start = D - m['rear_overall_depth']
side_end = D - rear_t
ramp_start = m['side_ramp_start_from_front']
flat_length, flat_h = m['right_flat_length'], m['right_flat_height']
flat_start = m['right_flat_start']
assert data['constraints']['right_vertical_step_flush_with_rear_ledge_front']
flat_end = ledge_start
assert abs(flat_end-flat_start-flat_length)<1e-6
step_rise = m['right_vertical_rise']
step_top = flat_h + step_rise
right_end_h = m['right_rear_height']
right_join_y = D-m['right_return_front_offset_from_rear']
assert flat_end < right_join_y < side_end
A, B, X, Y = (m[k] for k in ('opening_width', 'opening_depth', 'opening_left', 'opening_front'))
R = e['opening_radius']
wing_extension = m['left_wing_extension']
wing_length = m['left_wing_root_length']
wing_t = m['left_wing_thickness']
wing_shift = m['left_wing_forward_shift']
wing_y = D-wing_t-wing_shift
assert data['constraints']['left_wing_top_flush_with_rear_frame']
wing_bottom = H-wing_length
assert abs(wing_bottom-data['design']['left_wing_bottom_z'])<1e-6
wing_top = wing_bottom + wing_length
assert wing_extension > 0 and 0 < wing_t <= rear_t and 0 <= wing_bottom < wing_top <= H
assert abs(X + A + data['derived']['opening_right'] - W) < 1e-6
slots = data['features']['rear_slots']
slot_h = m['rear_slot_shoulder_height']
slot_center_h = m['rear_slot_center_height']
slot_center_w = m['rear_slot_center_width']
slot_depth = m['rear_slot_depth']
slot_lip = m['rear_slot_outer_lip_thickness']
slot_inner_h = m['rear_slot_inner_height']
bevel_angle = data['design']['rear_tab_inner_bevel_angle_deg']
bevel_run = slot_lip / math.tan(math.radians(bevel_angle))
assert 0 < slot_lip < slot_depth and slot_inner_h == slot_center_h
assert 0 < slot_h < slot_center_h < ledge_h
assert 0 < slot_depth < m['rear_overall_depth']
assert 0 < slot_center_w < min(m['rear_slot_widths'])
assert abs(slots[0]['left_offset'] - m['rear_first_slot_left_offset']) < 1e-6
for i, slot in enumerate(slots):
    assert abs(slot['width'] - m['rear_slot_widths'][i]) < 1e-6
    assert abs(slot['bottom_width'] - m['rear_slot_bottom_widths'][i]) < 1e-6
    assert slot_center_w < slot['bottom_width'] < slot['width']
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
# Join front and side rims with an inner diagonal parallel to each outer chamfer.
assert data['constraints']['front_wall_follows_corner_chamfers']
front_corner=data['features']['outer_corner_chamfers']
cf,cl=front_corner['front_edge_mm'],front_corner['front_side_mm']
normal_t=front_corner['front_wall_normal_thickness_mm']
# Outer left line: cl*x+cf*y=cl*cf; parallel inner line offset normal_t.
inner_constant=cl*cf+normal_t*math.hypot(cl,cf)
inner_x=(inner_constant-cf*front_t)/cl
inner_y=(inner_constant-cl*t)/cf
assert inner_x>t and inner_y>front_t
for triangle in ([(t,front_t),(inner_x,front_t),(t,inner_y)],
                 [(W-t,front_t),(W-inner_x,front_t),(W-t,inner_y)]):
    fill=cq.Workplane('XY',origin=(0,0,T)).polyline(triangle).close().extrude(rim_h-T)
    front=front.union(fill)

ledge = box(t, ledge_start, T, W-2*t, side_end-ledge_start, ledge_h-T)
body = body.union(rear).union(front).union(ledge)
# Right final rise joins the measured forward return of the rear frame.
profile = [(0, T), (side_end, T), (side_end, side_h),
           (ramp_start, rim_h), (0, rim_h)]
side = cq.Workplane('YZ').polyline(profile).close().extrude(t)
right_profile = [(0, T), (side_end, T), (side_end, H),
                 (right_join_y,H), (right_join_y,right_end_h),
                 (flat_end, step_top), (flat_end, flat_h),
                 (flat_start, flat_h), (ramp_start, rim_h), (0, rim_h)]
right_side = cq.Workplane('YZ').polyline(right_profile).close().extrude(t).translate((W-t, 0, 0))
assert abs(rear.val().BoundingBox().ylen - rear_t) < 1e-6
assert abs(front.val().BoundingBox().zmax - rim_h) < 1e-6
assert abs((inner_constant-cl*cf)/math.hypot(cl,cf)-normal_t)<1e-6
assert abs(side.val().BoundingBox().zmax - side_h) < 1e-6
assert abs(side.val().BoundingBox().xlen - t) < 1e-6
assert any(abs(v.Y - ramp_start) < 1e-6 and abs(v.Z - rim_h) < 1e-6
           for v in side.val().Vertices())
assert abs(right_side.val().BoundingBox().zmax-H)<1e-6
assert any(abs(v.Y-right_join_y)<1e-6 and abs(v.Z-right_end_h)<1e-6 for v in right_side.val().Vertices())
assert right_side.val().isInside(cq.Vector(W-t/2,right_join_y+0.05,H-0.1),1e-6)
assert not right_side.val().isInside(cq.Vector(W-t/2,right_join_y-0.05,right_end_h+0.1),1e-6)
assert abs(ledge.val().BoundingBox().ymin-flat_end)<1e-6
assert abs(ledge.val().BoundingBox().zmax - ledge_h) < 1e-6
assert abs(ledge.val().BoundingBox().ylen + rear_t - m['rear_overall_depth']) < 1e-6
assert any(abs(edge.Length() - flat_length) < 1e-6
           and all(abs(v.Z - flat_h) < 1e-6 for v in edge.Vertices())
           for edge in right_side.val().Edges())
body = body.union(side).union(right_side).clean()
# Keep measured wing height, top flush with rear frame. Photo shows a return.
wing = box(-wing_extension, wing_y, wing_bottom, wing_extension, wing_t, wing_length)
wing_return = box(0,wing_y,wing_bottom,data['design']['left_wing_return_thickness'],side_end-wing_y,wing_length)
assert abs(wing.val().BoundingBox().zmax-rear.val().BoundingBox().zmax)<1e-6
assert abs(wing_return.val().BoundingBox().zmax-H)<1e-6
body = body.union(wing_return).union(wing).clean()
assert abs(wing.val().BoundingBox().ymax-(D-wing_shift)) < 1e-6
assert len(body.solids().vals()) == 1
# Check actual exposed slope edge, not merely the intended input angle.
ramp_edges=[]
for edge in body.val().Edges():
    vs=edge.Vertices()
    if len(vs)==2 and all(abs(v.X)<1e-6 for v in vs):
        dy,dz=abs(vs[1].Y-vs[0].Y),abs(vs[1].Z-vs[0].Z)
        if dy>10 and dz>10 and min(v.Y for v in vs)>=ramp_start-1e-6:
            assert abs(math.degrees(math.atan2(dz,dy))-45)<1e-6
            ramp_edges.append(edge)
assert ramp_edges
uncut_volume = body.val().Volume()
expected_removed = 0.0
for slot in slots:
    x, width = slot['left_offset'], slot['width']
    center_x = x + (width-slot_center_w)/2
    inset = (width-slot['bottom_width'])/2
    # Trapezoid in XY: full width at rear face, narrow at inward depth.
    outline = [(x, D+1), (x+width, D+1), (x+width, D),
               (x+width-inset, D-slot_depth), (x+inset, D-slot_depth), (x, D)]
    full_cut = cq.Workplane('XY', origin=(0,0,-1)).polyline(outline).close().extrude(slot_h+1)
    middle_cut = box(center_x, D-slot_depth, slot_h, slot_center_w,
                     slot_depth+1, slot_center_h-slot_h)
    upper_taper = cq.Workplane('XY', origin=(0,0,slot_h)).polyline(outline).close().extrude(slot_inner_h-slot_h)
    inner_limit = box(x-1, D-slot_depth-1, slot_h, width+2,
                      slot_depth-slot_lip+1, slot_inner_h-slot_h)
    inner_upper_cut = upper_taper.intersect(inner_limit)
    body = body.cut(full_cut.union(middle_cut).union(inner_upper_cut))
    # Left/right chamfers in plan, extruded vertically without changing tab heights.
    for side_sign in (-1, 1):
        edge_x = x+width/2+side_sign*slot_center_w/2
        bevel_outline = [(edge_x,D), (edge_x,D-slot_lip),
                         (edge_x+side_sign*bevel_run,D-slot_lip)]
        bevel = cq.Workplane('XY',origin=(0,0,slot_h)).polyline(bevel_outline).close().extrude(slot_inner_h-slot_h)
        body = body.cut(bevel)
    width_at_lip = width-(width-slot['bottom_width'])*slot_lip/slot_depth
    inner_area = (width_at_lip+slot['bottom_width'])/2*(slot_depth-slot_lip)
    expected_removed += slot_depth*(width+slot['bottom_width'])/2*slot_h
    expected_removed += (inner_area+slot_center_w*slot_lip)*(slot_inner_h-slot_h)
    assert slot_center_w+2*bevel_run < width_at_lip
    expected_removed += slot_lip*bevel_run*(slot_inner_h-slot_h)
rear_groove_removed = expected_removed
end_slots = data['features']['base_end_slots']
end_y = D-end_slots['rear_edge_offset_mm']-end_slots['length_y_mm']
for left, right in end_slots['x_ranges_mm']:
    body = body.cut(box(left,end_y,-1,right-left,end_slots['length_y_mm'],H+2))
    expected_removed += (right-left)*end_slots['length_y_mm']*ledge_h
front_holes = data['features']['front_through_holes']
fh_w,fh_d = front_holes['width_x_mm'],front_holes['length_y_mm']
fh_y,fh_x = front_holes['front_edge_offset_mm'],front_holes['side_edge_offset_mm']
front_hole_xs = [fh_x,W-fh_x-fh_w]
assert fh_y >= front_t and fh_x > t
for hole_x in front_hole_xs:
    body = body.cut(box(hole_x,fh_y,-1,fh_w,fh_d,T+2))
    expected_removed += fh_w*fh_d*T
# Blind magnet pocket opens at underside; local boss provides a floor beyond ledge.
magnet = data['features']['magnet_replacing_B']
mx, my = magnet['center_x_mm'], D-magnet['center_rear_offset_mm']
mr, md = magnet['diameter_mm']/2, magnet['pocket_depth_mm']
br, floor = magnet['support_boss_diameter_mm']/2, magnet['retained_floor_mm']
volume_before_boss = body.val().Volume()
boss = cq.Workplane('XY').center(mx,my).circle(br).extrude(md+floor)
body = body.union(boss)
boss_added_volume = body.val().Volume()-volume_before_boss
pocket = cq.Workplane('XY',origin=(0,0,-1)).center(mx,my).circle(mr).extrude(md+1)
body = body.cut(pocket)
magnet_removed_volume = math.pi*mr*mr*md
expected_removed += magnet_removed_volume
# Outer corner chamfers: unequal setbacks in XY, cut through local full height.
ch = data['features']['outer_corner_chamfers']
cb,cs,ff,fs = (ch[k] for k in ('rear_right_back_mm','rear_right_side_mm','front_edge_mm','front_side_mm'))
corner_triangles = [[(W,D),(W-cb,D),(W,D-cs)],[(0,0),(ff,0),(0,fs)],[(W,0),(W-ff,0),(W,fs)]]
for triangle in corner_triangles:
    cutter=cq.Workplane('XY',origin=(0,0,-1)).polyline(triangle).close().extrude(H+2)
    body=body.cut(cutter)
corner_removed_volume=cb*cs/2*H+2*ff*fs/2*rim_h
expected_removed += corner_removed_volume
# Rear side openings: measured side profile; base continuation awaits dimensions.
so=data['features']['rear_side_wall_openings']
sy1=D-so['rear_outer_face_to_nearest_edge_mm']
sy0=sy1-so['front_to_back_length_mm']
syt=sy1-so['flat_top_length_mm']
sz=so['flat_top_z_from_underside_mm']; sf=so['front_vertical_top_z_mm']; sb=so['bottom_z_mm']
side_opening_profile=[(sy0,sb),(sy1,sb),(sy1,sz),(syt,sz),(sy0,sf)]
for ox in (0,W-t):
    cutter=cq.Workplane('YZ',origin=(ox,0,0)).polyline(side_opening_profile).close().extrude(t)
    body=body.cut(cutter)
side_openings_removed=2*t*((sy1-sy0)*(sz-sb)-(syt-sy0)*(sz-sf)/2)
expected_removed+=side_openings_removed
bc=data['features']['rear_side_base_cutouts']
bdepth,bflat=bc['inward_depth_mm'],bc['inner_flat_length_mm']
bmid=(sy0+sy1)/2
base_cut_outline=[(0,sy0),(bdepth,bmid-bflat/2),(bdepth,bmid+bflat/2),(0,sy1)]
for mirrored in (False,True):
    outline=[(W-x if mirrored else x,y) for x,y in base_cut_outline]
    cutter=cq.Workplane('XY',origin=(0,0,-1)).polyline(outline).close().extrude(T+1)
    body=body.cut(cutter)
base_cut_removed=2*bdepth*(sy1-sy0+bflat)/2*T
expected_removed+=base_cut_removed
notch=data['features']['rear_base_cutout_notches']
nlen,ndep=notch['rearward_length_mm'],notch['inward_depth_mm']
for nx0 in (0,W-ndep):
    body=body.cut(box(nx0,sy1,-1,ndep,nlen,T+1))
notch_removed_volume=2*nlen*ndep*T
expected_removed+=notch_removed_volume
body = body.clean()
solid = body.val()
for nx0 in (0,W-ndep):
    for zz in (.01,T-.01):
        assert not solid.isInside(cq.Vector(nx0+ndep/2,sy1+nlen/2,zz),1e-6)
        assert solid.isInside(cq.Vector(nx0+ndep/2,sy1+nlen+.02,zz),1e-6)
    assert solid.isInside(cq.Vector(nx0+ndep/2,sy1+nlen/2,T+.02),1e-6)
for ox in (t/2,W-t/2):
    assert not solid.isInside(cq.Vector(ox,(syt+sy1)/2,sz-0.05),1e-6)
    assert solid.isInside(cq.Vector(ox,(syt+sy1)/2,sz+0.05),1e-6)
    assert not solid.isInside(cq.Vector(ox,bmid,sb-0.05),1e-6)
    for frac in (.1,.5,.9):
        yy=sy0+(syt-sy0)*frac; zz=sf+(sz-sf)*frac
        assert not solid.isInside(cq.Vector(ox,yy,zz-0.05),1e-6)
        assert solid.isInside(cq.Vector(ox,yy,zz+0.05),1e-6)

for mirrored in (False,True):
    for frac in (.05,.5,.95):
        xx=bdepth*frac; low=sy0+(bmid-bflat/2-sy0)*frac; high=sy1+(bmid+bflat/2-sy1)*frac
        if mirrored: xx=W-xx
        for yy in (low+.02,bmid,high-.02):
            for zz in (.01,T-.01):
                assert not solid.isInside(cq.Vector(xx,yy,zz),1e-6)
        for yy in (low-.02,high+.02):
            assert solid.isInside(cq.Vector(xx,yy,T/2),1e-6)
    xx=bdepth+.02
    if mirrored: xx=W-xx
    assert solid.isInside(cq.Vector(xx,bmid,T/2),1e-6)
for triangle,local_h in zip(corner_triangles,(H,rim_h,rim_h)):
    px=sum(pt[0] for pt in triangle)/3
    py=sum(pt[1] for pt in triangle)/3
    for z in (0.01,local_h/2,local_h-0.01):
        assert not solid.isInside(cq.Vector(px,py,z),1e-6)
    ax,ay=triangle[1]; bx,by=triangle[2]
    midx,midy=(ax+bx)/2,(ay+by)/2
    # Verify preserved material just inside each new diagonal face.
    dx,dy=midx-triangle[0][0],midy-triangle[0][1]
    assert solid.isInside(cq.Vector(midx+dx*.02,midy+dy*.02,local_h/2),1e-6)

nx,ny=cl/math.hypot(cl,cf),cf/math.hypot(cl,cf)
for mirror in (False,True):
    for offset,inside in ((normal_t-0.02,True),(normal_t+0.02,False)):
        px=cf/2+nx*offset
        py=cl/2+ny*offset
        if mirror: px=W-px
        assert solid.isInside(cq.Vector(px,py,(T+rim_h)/2),1e-6)==inside
for hole_x in front_hole_xs:
    for z in (0.01,T/2,T-0.01):
        assert not solid.isInside(cq.Vector(hole_x+fh_w/2,fh_y+fh_d/2,z),1e-6)
        assert solid.isInside(cq.Vector(hole_x-0.05,fh_y+fh_d/2,z),1e-6)
        assert solid.isInside(cq.Vector(hole_x+fh_w/2,fh_y+fh_d+0.05,z),1e-6)
# Probe the complete floor including the portion forward of the rear ledge.
for radius in (0,mr-0.05):
    for angle in range(0,360,15):
        px, py = mx+radius*math.cos(math.radians(angle)), my+radius*math.sin(math.radians(angle))
        assert not solid.isInside(cq.Vector(px,py,md-0.02),1e-6)
        assert solid.isInside(cq.Vector(px,py,md+0.02),1e-6)
        assert solid.isInside(cq.Vector(px,py,md+floor-0.02),1e-6)

for left, right in end_slots['x_ranges_mm']:
    for z in (0.5,ledge_h/2,ledge_h-0.1):
        assert not solid.isInside(cq.Vector((left+right)/2,end_y+1.5,z),1e-6)
        assert solid.isInside(cq.Vector(left-0.1,end_y+1.5,z),1e-6)
assert abs(uncut_volume+boss_added_volume-solid.Volume()-expected_removed) < 1e-5
for slot in slots:
    x, width = slot['left_offset'], slot['width']
    center_x, mid_y = x+width/2, D-slot_depth/2
    # Both levels are empty; shoulders, inner floor and roof remain solid.
    assert not solid.isInside(cq.Vector(center_x, mid_y, slot_center_h-0.1), 1e-6)
    assert not solid.isInside(cq.Vector(center_x, mid_y, slot_h-0.1), 1e-6)
    assert solid.isInside(cq.Vector(center_x, D-slot_depth-0.1, slot_h/2), 1e-6)
    assert solid.isInside(cq.Vector(center_x, mid_y, slot_center_h+0.1), 1e-6)
    for fraction in (0.01, 0.5, 0.99):
        y = D-slot_depth*fraction
        inset = (width-slot['bottom_width'])/2*fraction
        for boundary, direction in ((x+inset,1),(x+width-inset,-1)):
            assert not solid.isInside(cq.Vector(boundary+direction*0.03,y,slot_h/2),1e-6)
            assert solid.isInside(cq.Vector(boundary-direction*0.03,y,slot_h/2),1e-6)
    # Above Z17, the side tabs exist only in the outermost 2 mm.
    for depth, material_expected in ((slot_lip/2,True),(slot_lip-0.02,True),
                                     (slot_lip+0.02,False),(slot_depth-0.02,False)):
        inset = (width-slot['bottom_width'])/2*depth/slot_depth
        for x_probe in (x+inset+0.15, x+width-inset-0.15):
            assert solid.isInside(cq.Vector(x_probe,D-depth,slot_h+1.0),1e-6) == material_expected
    # At three depths, verify each slanted vertical edge and intact Z17 underside.
    for fraction in (0.1,0.5,0.9):
        y = D-slot_lip*fraction
        for side_sign in (-1,1):
            edge_x = center_x+side_sign*(slot_center_w/2+bevel_run*fraction)
            for z_probe in (slot_h+0.1,slot_inner_h-0.1):
                assert not solid.isInside(cq.Vector(edge_x-side_sign*0.02,y,z_probe),1e-6)
                assert solid.isInside(cq.Vector(edge_x+side_sign*0.02,y,z_probe),1e-6)
            assert not solid.isInside(cq.Vector(edge_x+side_sign*0.02,y,slot_h-0.02),1e-6)
bevel_faces = []
for face in solid.Faces():
    if face.geomType() != 'PLANE':
        continue
    normal = face.normalAt()
    angle_in_plan = math.degrees(math.acos(min(1.0,abs(normal.y))))
    if abs(normal.z)<1e-7 and abs(angle_in_plan-bevel_angle)<1e-5:
        bevel_faces.append(face)
assert len(bevel_faces) == 2*len(slots)
assert any(abs(edge.Length()-step_rise) < 1e-6
           and all(abs(v.Y-flat_end) < 1e-6 and abs(v.X-W) < 1e-6 for v in edge.Vertices())
           for edge in solid.Edges())
assert solid.isValid() and len(body.solids().vals()) == 1
bb = solid.BoundingBox()
overall_width = W + wing_extension
assert all(abs(a - b) < 1e-6 for a, b in zip((bb.xlen, bb.ylen, bb.zlen), (overall_width, D, H)))
assert abs(bb.xmin + wing_extension) < 1e-6
assert abs(uncut_volume - (base.cut(opening).union(rear).union(front).union(ledge).union(side).union(right_side).union(wing_return).val().Volume() + wing_extension*wing_t*wing_length)) < 1e-5
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
          'rear_slot_removed_volume_mm3': rear_groove_removed,
          'base_end_slots': end_slots,
          'front_through_holes': front_holes,
          'front_hole_x_ranges_mm':[[x,x+fh_w] for x in front_hole_xs],
          'front_hole_y_range_mm':[fh_y,fh_y+fh_d],
          'front_through_holes_verified':True,
          'total_removed_volume_mm3': expected_removed,
          'outer_corner_chamfers':ch,
          'corner_chamfer_removed_volume_mm3':corner_removed_volume,
          'corner_chamfers_verified':True,
          'front_wall_follows_chamfer':True,
          'front_corner_wall_normal_thickness_mm':normal_t,
          'rear_side_wall_openings':so,
          'rear_side_opening_profile_yz_mm':side_opening_profile,
          'rear_side_opening_removed_volume_mm3':side_openings_removed,
          'rear_side_openings_verified':True,
          'rear_base_cutouts':bc,
          'rear_base_cutout_left_outline_xy_mm':base_cut_outline,
          'rear_base_cutout_removed_volume_mm3':base_cut_removed,
          'rear_base_cutouts_verified':True,
          'rear_base_cutout_notches':notch,
          'rear_base_notch_removed_volume_mm3':notch_removed_volume,
          'rear_base_notches_verified':True,
          'magnet_pocket': magnet,
          'magnet_floor_verified': True,
          'magnet_boss_added_volume_mm3': boss_added_volume,
          'rear_slot_mouth_widths_mm': [s['width'] for s in slots],
          'rear_slot_inner_bottom_widths_mm': [s['bottom_width'] for s in slots],
          'rear_slot_inward_taper_verified': True,
          'rear_slot_outer_lip_thickness_mm': slot_lip,
          'rear_slot_inner_height_mm': slot_inner_h,
          'rear_slot_lip_and_inner_clearance_verified': True,
          'rear_tab_bevel_angle_in_plan_to_x_deg': bevel_angle,
          'rear_tab_bevel_direction': 'left-right; no vertical bevel',
          'rear_tab_bevel_lateral_run_each_side_mm': bevel_run,
          'rear_tab_central_gap_outer_inner_mm': [slot_center_w,slot_center_w+2*bevel_run],
          'rear_tab_bevel_faces_verified': len(bevel_faces),
          'rear_slot_void_shoulders_and_inner_floor_verified': True,
          'rear_slot_x_ranges_mm': [[s['left_offset'], s['right_edge']] for s in slots],
          'body_width_excluding_wing_mm': W,
          'left_wing_extension_root_length_thickness_mm': [wing_extension, wing_length, wing_t],
          'left_wing_z_range_mm': [wing_bottom, wing_top],
          'left_wing_lower_edge_aligned_to_ramp': False,
          'left_slope_angle_deg_verified':45.0,
          'left_slope_visible_endpoint_y_z_mm':[wing_y,rim_h+wing_y-ramp_start],
          'left_wing_y_range_mm':[wing_y,wing_y+wing_t],
          'left_wing_forward_shift_mm':wing_shift,
          'left_wing_return_thickness_mm':data['design']['left_wing_return_thickness'],
          'left_wing_top_below_rear_top_mm': H-wing_top,
          'side_wall_thickness_mm': t,
          'front_wall_thickness_mm': front_t,
          'side_ramp_start_from_front_mm': ramp_start,
          'right_step_flush_with_rear_ledge_front':True,
          'right_step_y_mm':flat_end,
          'right_flat_length_mm': flat_length, 'right_flat_height_mm': flat_h,
          'right_flat_start_from_front_mm': flat_start,
          'right_vertical_rise_mm': step_rise, 'right_step_top_height_mm': step_top,
          'measured_right_rear_height_mm': right_end_h,
          'right_ramp_endpoint_y_mm':right_join_y,
          'right_return_front_offset_from_rear_mm':D-right_join_y,
          'right_return_wall_thickness_mm':t,
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
         f'Left slope45deg; wing forward shift{wing_shift:.2f}; return geometry provisional.',
         ha='center', fontsize=9)
fig.savefig(OUT / 'body_preview.png', bbox_inches='tight')
ax.view_init(elev=10, azim=90)
fig.savefig(OUT / 'rear_preview.png', bbox_inches='tight')
plt.close(fig)

fig, ax = plt.subplots(figsize=(9, 4), dpi=160)
ax.plot([0, ramp_start, wing_y, wing_y, side_end], [rim_h, rim_h, rim_h+wing_y-ramp_start, wing_top, wing_top],
        label='Left side', color='#2474a5', linewidth=2)
ax.plot([0, ramp_start, flat_start, flat_end, flat_end], [rim_h, rim_h, flat_h, flat_h, step_top],
        label='Right side', color='#d57322', linewidth=2)
ax.plot([flat_end,right_join_y,right_join_y,side_end], [step_top,right_end_h,H,H], color='#d57322', label='Right final ramp and rear return')
ax.annotate(f'Flat {flat_length:.2f} mm', xy=((flat_start+flat_end)/2, flat_h),
            xytext=(73, 25), arrowprops={'arrowstyle': '->'})
ax.annotate('Rise 10.00 mm', xy=(flat_end, (flat_h+step_top)/2), xytext=(80, 36), arrowprops={'arrowstyle':'->'})
ax.scatter([ramp_start, flat_start, flat_end, flat_end], [rim_h, flat_h, flat_h, step_top], color='#d57322')
ax.set(xlim=(55, D), ylim=(0, 50), xlabel='Distance from front (mm)',
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
