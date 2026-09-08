"""The approved color-flight storyboard, built from the existing closed armor.

blender -b --python trailer/create-flight-teaser.py -- --preview
blender -b --python trailer/create-flight-teaser.py

This independent second cut leaves the first teaser and mascot asset untouched.
"""
import argparse
import json
import math
import sys
import wave
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

BASE = Path(__file__).resolve().parent
OUT = BASE / 'output' / 'color-flight'
FRAMES = OUT / 'frames'
FPS, SECONDS = 24, 14
parser = argparse.ArgumentParser()
parser.add_argument('--preview', action='store_true')
parser.add_argument('--encode-only', action='store_true')
parser.add_argument('--audio-only', action='store_true')
parser.add_argument('--width', type=int, default=1280)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
OUT.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(exist_ok=True)


def smooth(a, b, t):
    x = min(1., max(0., (t-a)/(b-a)))
    return x*x*(3-2*x)


def linear_color(hex_color):
    values = [int(hex_color[i:i+2], 16)/255 for i in (0, 2, 4)]
    return tuple(v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values)


COLORS = {name: linear_color(value) for name, value in {
    'cyan': '00dcef', 'mint': '48efb2', 'violet': 'b988ff',
    'pink': 'ff83b6', 'gold': 'ffd658',
}.items()}
FREQUENCIES = {'cyan': 220., 'mint': 293.66, 'violet': 146.83, 'pink': 369.99, 'gold': 329.63}


def flight(name, color, start, end, points, size, tail=.8, ease=False, hero=False):
    return dict(name=name, color=color, start=start, end=end,
                points=np.array(points, dtype=float), size=size*(1.60 if hero else 1.20), tail=tail,
                ease=ease, hero=hero)


# World coordinates: X across frame, Y depth (camera at -20), Z up.
# Crossing paths are separated in depth; no shells actually collide.
FLIGHTS = [
    flight('First signal', 'cyan', .22, 1.80,
           [(-12,5,-1.7),(-5,2,-.9),(3,-4,.9),(17,-7,.7)], .52, .85),
    flight('Violet answer', 'violet', 1.94, 4.30,
           [(-11,7,3.8),(-3,5,1.8),(3,4,2),(12,2,3.8)], .35, .9),
    flight('Mint crossing', 'mint', 2.20, 4.54,
           [(13,5,-2.8),(5,2,-2),(-3,0,1.6),(-13,-3,2.9)], .51, .8),
    flight('Gold ascent', 'gold', 2.65, 4.85,
           [(-4,9,-9),(-5,7,-3),(-1,5,1.6),(3,3,8)], .31, .9),
    flight('Cyan return', 'cyan', 2.95, 4.70,
           [(-13,0,-3.7),(-5,-1,-3.1),(3,-3,-1.1),(15,-5,-1.8)], .60, .7),
    flight('Cyan weave', 'cyan', 4.42, 7.12,
           [(-12,1,-3.5),(-1,-1,6.2),(-2,-2,-4.2),(13,-3,2.8)], .62, 1.0),
    flight('Pink mischief', 'pink', 4.54, 7.10,
           [(12,4,3.8),(0,4,-6.2),(1,3,5.5),(-13,1,-2.8)], .51, .95),
    flight('Gold loop', 'gold', 4.83, 7.25,
           [(-11,7,3.5),(-4,6,-5.8),(6,5,-4.2),(12,5,3.8)], .33, 1.0),
    flight('Mint dart', 'mint', 5.08, 6.83,
           [(10,9,-4),(4,8,-.8),(2,7,2.7),(-11,5,4.6)], .28, .65),
    flight('Rush violet', 'violet', 6.98, 8.89,
           [(-1.2,20,1.6),(-2,10,1.8),(-2.5,-3,1.4),(-7,-17,3.2)], .47, .9, True),
    flight('Rush gold', 'gold', 7.02, 8.72,
           [(1.8,25,1.9),(2.2,11,1.5),(2.9,-3,1.8),(6,-17,4.7)], .38, .9, True),
    flight('Rush pink', 'pink', 7.12, 8.92,
           [(.7,20,-1.8),(1.6,9,-1.3),(2.8,-4,-1.8),(6,-17,-4.8)], .49, .85, True),
    flight('Rush mint', 'mint', 7.21, 8.99,
           [(-2,24,-.9),(-2.7,11,-1.4),(-2.8,-3,-2),(-7,-17,-4.1)], .55, .85, True),
    flight('One stays', 'cyan', 7.14, 9.35,
           [(.1,23,.1),(-2.4,12,-.8),(-1.1,-1,-.5),(.35,-1,.05)], .80, .70, True, True),
]


def progress(item, t):
    q = np.clip((np.asarray(t)-item['start'])/(item['end']-item['start']), 0, 1)
    # Smoothstep gives the hero a real deceleration and the rush a shared launch.
    return q*q*(3-2*q) if item['ease'] else q


def position(item, t):
    q = progress(item, t)
    p = item['points']
    return ((1-q)[...,None]**3*p[0] + 3*((1-q)**2*q)[...,None]*p[1]
            + 3*((1-q)*q*q)[...,None]*p[2] + (q**3)[...,None]*p[3])


def make_sound():
    """One airy propulsion voice, spatialized using the actual flight paths.

    No beat bed, unrelated impacts, notification tones, samples, or closing jingle.
    The last shell's existing voice slows into a breath, then a residual tail.
    """
    rate = 48000
    time = np.arange(rate*SECONDS)/rate
    stereo = np.zeros((len(time), 2), dtype=np.float64)
    rng = np.random.default_rng(20260905)
    fft_freq = np.fft.rfftfreq(len(time), 1/rate)

    def air_texture(low, high):
        spectrum = np.fft.rfft(rng.standard_normal(len(time)))
        weight = (1-np.exp(-(fft_freq/low)**3))*np.exp(-(fft_freq/high)**2)
        noise = np.fft.irfft(spectrum*weight, n=len(time))
        return noise/max(np.std(noise), 1e-9)

    # Smooth moving air, with no periodic mechanical buzz or sharp hiss layer.
    low_air = air_texture(120, 1050)
    high_air = air_texture(850, 5400)
    cues = []
    for i, item in enumerate(FLIGHTS):
        p = position(item, time)
        speed = np.linalg.norm((position(item, time+.005)-position(item, time-.005))/.01, axis=1)
        q = np.clip((time-item['start'])/(item['end']-item['start']), 0, 1)
        active = ((time>=item['start']) & (time<=item['end'])).astype(float)
        envelope = np.sin(np.pi*q)**1.7*active
        distance = np.sqrt(p[:,0]**2+(p[:,1]+20)**2+p[:,2]**2)
        proximity = np.clip((20/np.maximum(distance, 3))**1.3, .25, 2.1)
        movement = np.clip(speed/16, .25, 1.7)
        pan = np.clip(p[:,0]/np.maximum((p[:,1]+20)*.27, 2), -.94, .94)
        doppler = 1.14-.26*q
        phase = 2*np.pi*np.cumsum(FREQUENCIES[item['color']]*doppler)/rate
        voice = np.sin(phase)+.20*np.sin(phase*2+.3)+.045*np.sin(phase*3)
        air = .70*np.roll(low_air, i*5927)+.27*np.roll(high_air, i*8143)
        # Color gives a hint of a shared pentatonic harmony, not a sequence of bells.
        signal = (air*.040+voice*.025)*envelope*proximity*movement
        signal *= .88 if item['name'].startswith('Rush') else 1.
        stereo[:,0] += signal*np.sqrt((1-pan)/2)
        stereo[:,1] += signal*np.sqrt((1+pan)/2)
        cues.append({'event': item['name'], 'start': item['start'], 'end': item['end'],
                     'color': item['color'], 'peak_motion_time': float(time[np.argmax(envelope*proximity*movement)])})

    # All voices share the same short acoustic space. No independent rhythm track.
    dry = stereo.copy()
    for delay, gain in [(.071,.16),(.137,.10),(.229,.06)]:
        n = round(delay*rate)
        stereo[n:] += dry[:-n,::-1]*gain
    # Deliberate silence after the last shell settles. Clear even on phone speakers.
    stop_fade = np.clip((9.38-time)/.15,0,1)
    stereo *= stop_fade[:,None]

    def breath(start, length, gain, tail=False):
        u = time-start
        q = np.clip(u/length,0,1)
        env = np.sin(np.pi*q)**2*((u>=0)&(u<=length))
        if tail:
            env *= np.exp(-q*2.8)
        phase = 2*np.pi*220*(time+.004*np.sin(time*1.4))
        voice = (.018*np.sin(phase)+.009*low_air)*env*gain
        stereo[:,0] += voice*.707
        stereo[:,1] += voice*.707

    breath(9.94,.92,.80)
    breath(11.05,2.58,.40,True)
    stereo *= np.clip(time/.10,0,1)[:,None]
    stereo *= np.clip((13.85-time)/.35,0,1)[:,None]
    # Gentle peak rounding, then a conservative ceiling before AAC encoding.
    active_rms = np.sqrt(np.mean(stereo[int(.22*rate):int(9.38*rate)]**2))
    stereo *= .105/max(active_rms,1e-9)
    stereo = .72*np.tanh(stereo/.72)
    stereo *= .67/max(float(np.max(np.abs(stereo))), 1e-9)
    assert np.all(np.isfinite(stereo))
    assert np.max(np.abs(stereo[int(9.42*rate):int(9.90*rate)])) == 0
    with wave.open(str(OUT/'flight-sound.wav'),'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes((stereo*32767).astype('<i2').tobytes())
    report = {'seconds': SECONDS, 'sampleRate': rate, 'channels': 2,
              'peakDBFS': float(20*np.log10(np.max(np.abs(stereo)))),
              'rmsDBFS': float(20*np.log10(np.sqrt(np.mean(stereo**2)))),
              'intentionalSilence': [9.42,9.90], 'flightCues': cues}
    (OUT/'sound-cues.json').write_text(json.dumps(report, indent=2)+'\n')
    print('SOUND:', json.dumps({k:v for k,v in report.items() if k!='flightCues'}), flush=True)


make_sound()
if args.audio_only:
    sys.exit(0)

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.name = 'Color flight — photography'
scene.render.fps = FPS
bpy.ops.import_scene.gltf(filepath=str(BASE.parent/'public/smarter-orb.glb'), export_import_convert_lighting_mode='COMPAT')
plate_names = ['Crown','Chin','Side','RearCap']
kept = {p+'Armor' for p in plate_names} | {p+'Seam_default' for p in plate_names}
for obj in list(scene.objects):
    obj.animation_data_clear()
    if obj.type=='MESH' and obj.data.shape_keys:
        obj.data.shape_keys.animation_data_clear()
    if obj.type=='LIGHT' or (obj.type=='MESH' and obj.name not in kept):
        bpy.data.objects.remove(obj, do_unlink=True)
for name in ['Smarter','Body','ArmorRig',*plate_names]:
    obj = bpy.data.objects[name]
    # Preserve the glTF root's coordinate conversion; clear only rig pose nodes.
    if name!='Smarter':
        obj.location = (0,0,0)
        obj.rotation_mode = 'QUATERNION'
        obj.rotation_quaternion = (1,0,0,0)
        obj.scale = (1,1,1)
for name in plate_names:
    bpy.data.objects[name+'Trim_default'].scale = (1,1,1)
bpy.context.view_layer.update()

# Bake just the closed shell meshes into world space. No face or identity-sensitive
# geometry survives in this scene, even for the close fly-bys.
templates = []
for name in sorted(kept):
    obj = bpy.data.objects[name]
    mesh = obj.data.copy()
    mesh.transform(obj.matrix_world)
    templates.append((name,mesh,'Seam' in name))
for obj in list(scene.objects):
    bpy.data.objects.remove(obj,do_unlink=True)


def emission(name, color, strength):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    node = mat.node_tree.nodes.new('ShaderNodeEmission')
    node.inputs['Color'].default_value = (*color,1)
    node.inputs['Strength'].default_value = strength
    output = mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
    mat.node_tree.links.new(node.outputs[0],output.inputs['Surface'])
    return mat,node


armor_material = bpy.data.materials.new('Soft graphite armor / flight only')
armor_material.use_nodes = True
bsdf = armor_material.node_tree.nodes.get('Principled BSDF')
bsdf.inputs['Base Color'].default_value = (.065,.078,.088,1)
bsdf.inputs['Metallic'].default_value = .48
bsdf.inputs['Roughness'].default_value = .38
for _,mesh,is_seam in templates:
    if not is_seam:
        mesh.materials.clear()
        mesh.materials.append(armor_material)


def light(name, parent, location, energy, color, size):
    data = bpy.data.lights.new(name,'AREA')
    data.energy = energy
    data.color = color
    data.shape = 'DISK'
    data.size = size
    data.use_shadow = False
    obj = bpy.data.objects.new(name,data)
    scene.collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    obj.rotation_euler = (-Vector(location)).to_track_quat('-Z','Y').to_euler()
    return obj


def trail_object(name, material, width):
    data = bpy.data.curves.new(name,'CURVE')
    data.dimensions = '3D'
    data.resolution_u = 1
    data.bevel_depth = width
    data.bevel_resolution = 3
    spline = data.splines.new('POLY')
    spline.points.add(95)
    data.materials.append(material)
    obj = bpy.data.objects.new(name,data)
    scene.collection.objects.link(obj)
    return obj,spline


actors = []
for index,item in enumerate(FLIGHTS):
    root = bpy.data.objects.new(item['name'],None)
    scene.collection.objects.link(root)
    root.scale = (item['size'],)*3
    mesh_objects = []
    colored_material, seam_node = emission(item['name']+' / seam',COLORS[item['color']],3.0)
    core_material, core_node = emission(item['name']+' / hidden core',COLORS[item['color']],1.65)
    trail_material, trail_node = emission(item['name']+' / trail',COLORS[item['color']],4.0)
    for name,mesh,is_seam in templates:
        if is_seam:
            mesh = mesh.copy()
            mesh.materials.clear()
            mesh.materials.append(colored_material)
        obj = bpy.data.objects.new(item['name']+' / '+name,mesh)
        scene.collection.objects.link(obj)
        obj.parent = root
        mesh_objects.append(obj)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32,ring_count=20,radius=1.01)
    core = bpy.context.object
    core.name = item['name']+' / featureless interior'
    core.parent = root
    core.data.materials.append(core_material)
    for polygon in core.data.polygons:
        polygon.use_smooth = True
    mesh_objects.append(core)
    lights = [light(item['name']+' / softbox',root,(-3,-4,4),110*item['size']**2,(.77,.87,1),4.5),
              light(item['name']+' / colored edge',root,(2.8,1.5,1.5),105*item['size']**2,COLORS[item['color']],3.2)]
    trails = [trail_object(item['name']+' / flowing streak',trail_material,.055*item['size']),
              trail_object(item['name']+' / fine filament',trail_material,.010*item['size'])]
    actors.append((item,root,mesh_objects,lights,trails,seam_node,core_node,trail_node))

scene.world.use_nodes = True
scene.world.node_tree.nodes.get('Background').inputs[0].default_value = (.002,.005,.009,1)
scene.world.node_tree.nodes.get('Background').inputs[1].default_value = .18
bpy.ops.object.camera_add(location=(0,-20,0))
camera = bpy.context.object
camera.name = 'Color flight camera'
camera.rotation_euler = (Vector((0,0,0))-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type = 'PERSP'
camera.data.lens = 43
camera.data.sensor_width = 36
camera.data.clip_start = .08
camera.data.clip_end = 200
scene.camera = camera

font = bpy.data.fonts.load('/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf')
regular = bpy.data.fonts.load('/System/Library/Fonts/Supplemental/Arial.ttf')
labels = []


def label(name, body, u, v, size, color, start, tracking=1., bold=True):
    data = bpy.data.curves.new(name,'FONT')
    data.body = body
    data.align_x = 'CENTER'
    data.align_y = 'CENTER'
    data.size = size
    data.space_character = tracking
    data.space_line = 1.12
    data.font = font if bold else regular
    obj = bpy.data.objects.new(name,data)
    scene.collection.objects.link(obj)
    obj.parent = camera
    mat,node = emission(name+' / ink',color,1)
    data.materials.append(mat)
    labels.append((obj,node,u,v,start))


label('The only title','Something bright\nis coming.',.5,.53,.052,(.72,.84,.87),11.22)
label('Community','SMARTER DEV DISCORD',.5,.30,.011,(.08,.53,.59),11.52,1.35,False)

scene.render.engine = 'BLENDER_EEVEE'
scene.eevee.taa_render_samples = 24
scene.eevee.use_raytracing = False
scene.render.resolution_x = args.width
scene.render.resolution_y = args.width*9//16
scene.render.resolution_percentage = 100
scene.render.film_transparent = False
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.compression = 15
scene.view_settings.view_transform = 'AgX'
scene.view_settings.look = 'AgX - Medium High Contrast'
scene.view_settings.exposure = -.2
scene.frame_start = 1
scene.frame_end = FPS*SECONDS
tree = bpy.data.node_groups.new('Color flight / lens diffusion','CompositorNodeTree')
scene.compositing_node_group = tree
tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
layers = tree.nodes.new('CompositorNodeRLayers')
glow = tree.nodes.new('CompositorNodeGlare')
glow.inputs['Type'].default_value = 'Fog Glow'
glow.inputs['Quality'].default_value = 'High'
glow.inputs['Threshold'].default_value = .65
glow.inputs['Strength'].default_value = 1.0
glow.inputs['Size'].default_value = .8
output = tree.nodes.new('NodeGroupOutput')
tree.links.new(layers.outputs['Image'],glow.inputs['Image'])
tree.links.new(glow.outputs['Image'],output.inputs['Image'])


def update_frame(current,*_):
    if current!=scene:
        return
    t = (current.frame_current-1)/FPS
    # A restrained push-in during the rush, then steady for the curious hold.
    camera.data.lens = 43+3*smooth(6.9,8.8,t)
    for i,(item,root,meshes,lights,trails,seam,core,streak) in enumerate(actors):
        hero = item['hero']
        end = 11.07 if hero else item['end']
        visible = item['start']<=t<end
        root.location = position(item,t)
        q = float(progress(item,t))
        root.rotation_euler = (.07*math.sin(t*1.6+i),.08*math.sin(t*1.3+i),-.35+q*.7+i*.37)
        if hero:
            hold = smooth(9.55,10.15,t)
            root.rotation_euler = (.06+.05*hold,-.02-.18*hold,-.20+.12*hold)
            root.location.z += .022*math.sin((t-9.35)*2.6)*smooth(9.35,9.6,t)
        pulse = 1+.50*math.exp(-((t-10.18)/.35)**2) if hero else 1
        fade = (1-smooth(10.78,11.07,t)) if hero else 1
        seam.inputs['Strength'].default_value = 3*pulse*fade
        core.inputs['Strength'].default_value = 1.65*pulse*fade
        for obj in meshes:
            obj.hide_render = not visible
        for lamp in lights:
            lamp.hide_render = not visible
        tail_visible = item['start']<t<item['end']+item['tail']
        tail_fade = 1-smooth(item['end'],item['end']+item['tail'],t)
        if hero:
            tail_fade *= 1-smooth(9.15,9.55,t)
        streak.inputs['Strength'].default_value = 4*tail_fade
        for j,(obj,spline) in enumerate(trails):
            obj.hide_render = not tail_visible or tail_fade<.001
            if obj.hide_render:
                continue
            oldest = max(item['start'],t-item['tail'])
            newest = min(t,item['end'])
            for k,point in enumerate(spline.points):
                f = k/(len(spline.points)-1)
                stamp = oldest+(newest-oldest)*f
                pos = position(item,stamp)
                if j:
                    separation = math.sin(math.pi*f)*item['size']*.11
                    pos = pos+np.array((0,separation, separation*math.sin(stamp*5)))
                point.co = (*pos,1)
                point.radius = max(.001,f**.85)*(1 if not j else .7)
    # Text exists only after the mystery beat; no overlays during flight.
    width_at_text = camera.data.sensor_width/camera.data.lens*4
    for obj,node,u,v,start in labels:
        fade = smooth(start,start+.38,t)*(1-smooth(13.55,13.98,t))
        obj.hide_render = fade<.001
        obj.location = ((u-.5)*width_at_text,(v-.5)*width_at_text*9/16,-4)
        obj.scale = (width_at_text,)*3
        node.inputs['Strength'].default_value = fade


bpy.app.handlers.frame_change_pre.append(update_frame)
scene.frame_set(1)
assert not any(o.type=='MESH' and ('Face' in o.name or 'Eye' in o.name) for o in scene.objects)
assert all('QUBIT' not in o.data.body.upper() for o in scene.objects if o.type=='FONT')
# All flight shells use the same closed geometry. Shared templates stay immutable.
assert len(actors)==14 and len(COLORS)==5
preview_times = [.90,3.35,5.72,7.85,9.96,12.25]
if not args.encode_only:
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'color-flight.blend'))
if args.preview:
    for seconds in preview_times:
        scene.frame_set(round(seconds*FPS)+1)
        scene.render.filepath = str(OUT/f'preview-{seconds:.2f}.png')
        bpy.ops.render.render(write_still=True)
    print('COLOR-FLIGHT PREVIEW COMPLETE',flush=True)
else:
    if not args.encode_only:
        scene.render.filepath = str(FRAMES/'frame-')
        bpy.ops.render.render(animation=True)
    assert all((FRAMES/f'frame-{frame:04d}.png').exists() for frame in range(1,FPS*SECONDS+1)), 'Missing frames'
    edit = bpy.data.scenes.new('Color flight — final edit')
    edit.render.fps = FPS
    edit.frame_start = 1
    edit.frame_end = FPS*SECONDS
    edit.render.resolution_x = args.width
    edit.render.resolution_y = args.width*9//16
    edit.render.resolution_percentage = 100
    edit.view_settings.view_transform = 'Standard'
    edit.view_settings.look = 'None'
    edit.sequencer_colorspace_settings.name = 'sRGB'
    sequence = edit.sequence_editor_create()
    strip = sequence.strips.new_image('Color flight picture',str(FRAMES/'frame-0001.png'),channel=1,frame_start=1)
    for frame in range(2,FPS*SECONDS+1):
        strip.elements.append(f'frame-{frame:04d}.png')
    sequence.strips.new_sound('Motion-driven original sound',str(OUT/'flight-sound.wav'),channel=2,frame_start=1)
    edit.render.image_settings.media_type = 'VIDEO'
    edit.render.image_settings.file_format = 'FFMPEG'
    edit.render.ffmpeg.format = 'MPEG4'
    edit.render.ffmpeg.codec = 'H264'
    edit.render.ffmpeg.constant_rate_factor = 'MEDIUM'
    edit.render.ffmpeg.ffmpeg_preset = 'GOOD'
    edit.render.ffmpeg.gopsize = FPS*2
    edit.render.ffmpeg.audio_codec = 'AAC'
    edit.render.ffmpeg.audio_bitrate = 192
    edit.render.ffmpeg.audio_mixrate = 48000
    edit.render.ffmpeg.audio_channels = 'STEREO'
    edit.render.filepath = str(OUT/'smarter-dev-color-flight-teaser.mp4')
    bpy.context.window.scene = edit
    bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'color-flight.blend'))
    bpy.ops.render.render(animation=True,scene=edit.name)
    print(f'COLOR-FLIGHT TEASER COMPLETE: {edit.render.filepath}',flush=True)
