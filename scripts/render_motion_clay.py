#!/usr/bin/env python3
"""Render measured body/hand joints as a new, fixed-proportion clay avatar.

blender -b --python scripts/render_motion_clay.py -- --input motion.json \
    --output-dir clay-frames
python3 scripts/render_motion_clay.py --input motion.json --validate-only

No source images, textures, faces, clothing, network, or paid APIs are used.
Monocular depth is an estimate; this is a 3D motion proxy, not calibrated mocap.
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

CORE = tuple(side + '_' + name for side in ('left', 'right')
             for name in ('shoulder', 'elbow', 'wrist'))
FINGERS = ((1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12),
           (13, 14, 15, 16), (17, 18, 19, 20))
LENGTHS = ((.033, .027, .022), (.041, .026, .020),
           (.045, .029, .022), (.042, .027, .021), (.034, .022, .018))
THRESHOLD = .20


def point(value):
    return (isinstance(value, list) and len(value) == 4
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    and math.isfinite(x) for x in value)
            and 0 <= value[3] <= 1 and all(abs(x) <= 10 for x in value[:3]))


def load_motion(path):
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Motion JSON exceeds 8 MiB.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema_version') != 1 or data.get('fps') != 24
            or data.get('duration') != 6 or data.get('width') != 576
            or data.get('height') != 1024):
        raise ValueError('Expected schema 1, 24 fps, 6 seconds, 576x1024.')
    frames = data.get('frames')
    if not isinstance(frames, list) or len(frames) != 144:
        raise ValueError('Exactly 144 measured frames are required.')
    missing = {name: 0 for name in CORE}
    hand_missing = {'left': 0, 'right': 0}
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict) or not isinstance(frame.get('body'), dict):
            raise ValueError('Each frame must contain body joints.')
        t = frame.get('t')
        if not isinstance(t, (int, float)) or not math.isfinite(t) or abs(t - i / 24) > .001:
            raise ValueError('Frame times must follow the exact 24 fps timeline.')
        for name, value in frame['body'].items():
            if value is not None and not point(value):
                raise ValueError('Invalid body joint.')
        for name in CORE:
            value = frame['body'].get(name)
            missing[name] += not (point(value) and value[3] >= THRESHOLD)
        hands = frame.get('hands')
        if not isinstance(hands, dict) or any(s not in hands for s in hand_missing):
            raise ValueError('Both hand slots must be present.')
        for side in hand_missing:
            hand = hands[side]
            if hand is not None and (not isinstance(hand, list) or len(hand) != 21
                                     or not all(point(v) for v in hand)):
                raise ValueError('Hands require 21 complete measured points or null.')
            hand_missing[side] += hand is None or any(v[3] < THRESHOLD for v in hand)
    if any(count > 36 for count in missing.values()):
        raise ValueError('Measured upper-body coverage is below 75%.')
    return data, missing, hand_missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', default='clay-frames')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--frame-limit', type=int, default=144,
                        help='Local QA only: render the first N frames; default 144.')
    args = parser.parse_args(argv)
    data, missing, hand_missing = load_motion(args.input)
    lower_body_observed = data.get('quality', {}).get('lower_body_observed') is True
    if not 1 <= args.frame_limit <= 144:
        raise ValueError('Frame limit must be 1..144.')
    summary = {'schema_version': 1, 'fps': 24, 'duration': 6,
               'width': 576, 'height': 1024, 'frames': args.frame_limit,
               'representation': 'fixed_proportion_3d_joint_proxy',
               'depth': 'monocular_estimate_not_metric',
               'source_pixels_used': False, 'face_or_clothing_geometry': False,
               'lower_body_observed': lower_body_observed,
               'core_missing_frames': missing, 'hand_missing_frames': hand_missing,
               'missing_hands': 'plain_palm_only_no_invented_finger_motion',
               'unobserved_legs': 'fixed_neutral_pose_not_measured_motion',
               'warnings': []}
    if any(missing.values()):
        summary['warnings'].append('Unmeasured arm segments are hidden for those frames.')
    if any(hand_missing.values()):
        summary['warnings'].append('Some hand frames lack measured finger motion.')
    if args.validate_only:
        print(json.dumps(summary, indent=2))
        return 0
    import bpy
    from mathutils import Matrix, Vector

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = 576
    scene.render.resolution_y = 1024
    scene.render.resolution_percentage = 100
    scene.render.fps = 24
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    scene.display.render_aa = '8'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'MATERIAL'
    shading.show_shadows = True
    shading.show_cavity = True
    shading.cavity_type = 'WORLD'
    shading.background_type = 'WORLD'
    scene.world.color = (.16, .18, .20)
    try:
        scene.view_settings.view_transform = 'Standard'
    except TypeError:
        pass

    def material(name, color):
        result = bpy.data.materials.new(name)
        result.diffuse_color = (*color, 1)
        return result

    clay = material('Neutral clay - no source appearance', (.70, .72, .75))
    floor_material = material('Plain floor', (.23, .26, .29))
    cache = {}

    def sphere(name, radius=.05):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=10, ring_count=6, radius=1)
        obj = bpy.context.object
        obj.name = name
        obj.data.materials.append(clay)
        obj.scale = (radius,) * 3
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
        cache[name] = obj
        return obj

    def bone(name, a, b, radius):
        if name not in cache:
            bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=1, depth=1)
            obj = bpy.context.object
            obj.name = name
            obj.data.materials.append(clay)
            for polygon in obj.data.polygons:
                polygon.use_smooth = True
            cache[name] = obj
        obj = cache[name]
        obj.hide_render = a is None or b is None
        if obj.hide_render:
            return
        delta = b - a
        obj.location = (a + b) / 2
        obj.rotation_mode = 'QUATERNION'
        obj.rotation_quaternion = delta.to_track_quat('Z', 'Y')
        obj.scale = (radius, radius, max(delta.length, .0001))

    def joint(name, p, radius):
        obj = cache.get(name) or sphere(name, radius)
        obj.hide_render = p is None
        if p is not None:
            obj.location = p

    def source(p):
        return Vector((p[0] * 576 / 1024, p[2] * 576 / 1024, -p[1]))

    def measured(frame, name):
        p = frame['body'].get(name)
        return source(p) if point(p) and p[3] >= THRESHOLD else None

    def unit(v, default=(0, 0, 1)):
        return v.normalized() if v.length > .00001 else Vector(default)

    def measured_direction(frame, a, b):
        pa, pb = measured(frame, a), measured(frame, b)
        return unit(pb - pa) if pa is not None and pb is not None else None

    valid = [f for f in data['frames']
             if measured(f, 'left_shoulder') is not None
             and measured(f, 'right_shoulder') is not None]
    if not valid:
        raise ValueError('No measured shoulder pair for avatar scale.')
    baseline = (measured(valid[0], 'left_shoulder') + measured(valid[0], 'right_shoulder')) / 2
    spans = [(measured(f, 'left_shoulder') - measured(f, 'right_shoulder')).length for f in valid]
    scale = .36 / max(statistics.median(spans), .04)
    shoulder_neutral = Vector((0, 0, 1.43))
    bounds = []

    def pose(frame):
        left, right = measured(frame, 'left_shoulder'), measured(frame, 'right_shoulder')
        center = (left + right) / 2 if left is not None and right is not None else baseline
        displacement = (center - baseline) * scale
        displacement.y = max(-.18, min(.18, displacement.y))
        shoulders = shoulder_neutral + displacement
        across = unit(left - right, (1, 0, 0)) if left is not None and right is not None else Vector((1, 0, 0))
        hips = [measured(frame, s + '_hip') for s in ('left', 'right')]
        up = unit(center - (hips[0] + hips[1]) / 2) if all(p is not None for p in hips) else Vector((0, 0, 1))
        pelvis = shoulders - up * .50
        result = {'shoulders': shoulders, 'pelvis': pelvis, 'up': up, 'across': across}
        head = measured(frame, 'head_center')
        result['head'] = shoulders + (unit(head - center) if head is not None else up) * .245
        for side, sign in (('left', 1), ('right', -1)):
            shoulder = shoulders + across * (.18 * sign)
            hip = pelvis + across * (.13 * sign)
            arm = measured_direction(frame, side + '_shoulder', side + '_elbow')
            forearm = measured_direction(frame, side + '_elbow', side + '_wrist')
            elbow = shoulder + arm * .285 if arm is not None else None
            wrist = elbow + forearm * .265 if elbow is not None and forearm is not None else None
            # Legs move only when all three leg joints have measured support.
            thigh = measured_direction(frame, side + '_hip', side + '_knee') if lower_body_observed else None
            shin = measured_direction(frame, side + '_knee', side + '_ankle') if lower_body_observed else None
            knee = hip + (thigh if thigh is not None else Vector((sign * .06, 0, -1))).normalized() * .43
            ankle = knee + (shin if shin is not None and thigh is not None else Vector((0, 0, -1))) * .42
            result[side] = {'shoulder': shoulder, 'elbow': elbow, 'wrist': wrist,
                            'hip': hip, 'knee': knee, 'ankle': ankle}
        return result

    poses = [pose(frame) for frame in data['frames']]
    for p in poses:
        bounds.extend([p['head'] + Vector((0, 0, .13)),
                       p['pelvis'] - Vector((0, 0, .25))])
        for side in ('left', 'right'):
            r = p[side]
            bounds.extend(r[key] for key in ('shoulder', 'elbow') if r[key] is not None)
            if r['wrist'] is not None:
                bounds.extend([r['wrist'] + Vector((.20, 0, .20)),
                               r['wrist'] - Vector((.20, 0, .20))])
    # Fixed medium shot: head to upper thighs, retaining every measured hand.
    # Neutral/inferred lower legs must never zoom the gesture reference out.
    xmin, xmax = min(v.x for v in bounds) - .05, max(v.x for v in bounds) + .05
    zmin, zmax = min(v.z for v in bounds) - .05, max(v.z for v in bounds) + .05
    ortho = max(zmax - zmin, (xmax - xmin) / (576 / 1024)) * 1.06
    camera_data = bpy.data.cameras.new('Fixed motion reference camera')
    camera = bpy.data.objects.new('Fixed motion reference camera', camera_data)
    scene.collection.objects.link(camera)
    camera.location = ((xmin + xmax) / 2, -6, (zmin + zmax) / 2)
    camera.rotation_euler = (Vector((camera.location.x, 0, camera.location.z)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = ortho
    scene.camera = camera
    bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, min(.04, zmin + .09)))
    bpy.context.object.data.materials.append(floor_material)
    torso = sphere('Fixed torso', 1)
    palm_objects = {side: sphere(side + ' plain palm', 1) for side in ('left', 'right')}

    def render_hand(frame, side, wrist):
        hand = frame['hands'][side]
        observed = wrist is not None and hand is not None and all(p[3] >= THRESHOLD for p in hand)
        palm = palm_objects[side]
        palm.hide_render = wrist is None
        for obj in cache.values():
            if obj.name.startswith(side + ' finger '):
                obj.hide_render = True
        if wrist is None:
            return
        if not observed:
            palm.location = wrist + Vector((0, 0, -.035))
            palm.rotation_mode = 'QUATERNION'
            palm.rotation_quaternion = Vector((0, 0, -1)).to_track_quat('Z', 'Y')
            palm.scale = (.034, .018, .045)
            return
        points = [source(p) for p in hand]
        forward = unit(points[9] - points[0])
        across = unit(points[5] - points[17], (1, 0, 0))
        across = unit(across - forward * across.dot(forward), (1, 0, 0))
        normal = unit(forward.cross(across), (0, -1, 0))
        across = unit(normal.cross(forward), (1, 0, 0))
        palm.location = wrist + forward * .039
        palm.rotation_mode = 'QUATERNION'
        palm.rotation_quaternion = Matrix((across, normal, forward)).transposed().to_quaternion()
        palm.scale = (.041, .017, .047)
        bases = [wrist + forward * .026 + across * .034,
                 wrist + forward * .073 + across * .030,
                 wrist + forward * .081 + across * .010,
                 wrist + forward * .076 - across * .013,
                 wrist + forward * .063 - across * .031]
        for f, ids in enumerate(FINGERS):
            current = bases[f]
            joint(side + ' finger %s base' % f, current, .010)
            for segment in range(3):
                direction = unit(points[ids[segment + 1]] - points[ids[segment]], tuple(forward))
                end = current + direction * LENGTHS[f][segment]
                bone(side + ' finger %s bone %s' % (f, segment), current, end, .0075)
                joint(side + ' finger %s joint %s' % (f, segment), end, .009)
                current = end

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.glob('frame-*.png')):
        raise ValueError('Output already contains frames; use a fresh directory.')
    for i, (frame, p) in enumerate(zip(data['frames'], poses)):
        if i >= args.frame_limit:
            break
        torso.location = (p['shoulders'] + p['pelvis']) / 2
        torso.rotation_mode = 'QUATERNION'
        torso.rotation_quaternion = p['up'].to_track_quat('Z', 'Y')
        torso.scale = (.145, .095, .29)
        joint('Plain featureless head', p['head'], .105)
        bone('Neck', p['shoulders'], p['head'], .044)
        for side in ('left', 'right'):
            r = p[side]
            for name, radius in (('shoulder', .057), ('elbow', .039), ('wrist', .026),
                                 ('hip', .065), ('knee', .052), ('ankle', .034)):
                joint(side + ' ' + name, r[name], radius)
            for a, b, radius in (('shoulder', 'elbow', .045), ('elbow', 'wrist', .034),
                                  ('hip', 'knee', .066), ('knee', 'ankle', .046)):
                bone(side + ' ' + a + '-' + b, r[a], r[b], radius)
            render_hand(frame, side, r['wrist'])
        scene.render.filepath = str(output / ('frame-%06d.png' % i))
        bpy.ops.render.render(write_still=True)
        if i % 24 == 0:
            print('Clay render: %s/%s frames' % (i + 1, args.frame_limit), flush=True)
    summary['engine'] = scene.render.engine
    summary['camera'] = 'fixed_orthographic_medium_shot_head_upper_thighs_with_hand_margin'
    (output / 'render-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print('Clay render completed: %s frames.' % args.frame_limit, flush=True)
    return 0


if __name__ == '__main__':
    try:
        arguments = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
        raise SystemExit(main(arguments))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('Clay render stopped: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
