"""Render an identity-concealing teaser from the existing Qubit model.

blender -b --python trailer/create-teaser.py -- --preview
blender -b --python trailer/create-teaser.py

No face, name, emote, or open-shell reveal appears in this film.
"""
import argparse
import math
import sys
import wave
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

BASE = Path(__file__).resolve().parent
OUT = BASE / 'output'
FRAMES = OUT / 'frames'
FPS, SECONDS = 24, 12
parser = argparse.ArgumentParser()
parser.add_argument('--preview', action='store_true')
parser.add_argument('--encode-only', action='store_true')
parser.add_argument('--width', type=int, default=1280)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
OUT.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(exist_ok=True)


def smooth(a, b, t):
    x = min(1., max(0., (t-a)/(b-a)))
    return x*x*(3-2*x)


def window(t, start, end, fade=.35):
    return smooth(start, start+fade, t)*(1-smooth(end-fade, end, t))


def emission(name, color, strength=1):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.node_tree.nodes.clear()
    node = material.node_tree.nodes.new('ShaderNodeEmission')
    node.inputs['Color'].default_value = (*color, 1)
    node.inputs['Strength'].default_value = strength
    output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(node.outputs[0], output.inputs['Surface'])
    return material, node


def sound_bed():
    """Original synthesized pulses, air swells, and a restrained closing chime."""
    rate = 48000
    t = np.arange(rate*SECONDS)/rate
    channels = np.zeros((len(t), 2), dtype=np.float64)
    rng = np.random.default_rng(742)

    def mix(signal, pan=0.):
        channels[:, 0] += signal*math.sqrt((1-pan)/2)
        channels[:, 1] += signal*math.sqrt((1+pan)/2)

    # A quiet, slowly opening fifth. No borrowed music or sampled audio.
    envelope = np.minimum(t/1.5, 1)*np.clip((12-t)/1.4, 0, 1)
    pad = (.033*np.sin(2*np.pi*55*t) + .015*np.sin(2*np.pi*82.41*t)
           + .009*np.sin(2*np.pi*110.15*t))
    mix(pad*envelope*(.75+.25*np.sin(2*np.pi*.09*t)))
    for start, gain in [(.45,.18),(1.08,.11),(3.25,.24),(6.05,.16),(6.7,.12),(8.25,.21)]:
        u = np.maximum(0, t-start)
        kick = np.sin(2*np.pi*(47*u+4.5*(1-np.exp(-u*22))))
        mix(gain*kick*(t>=start)*np.minimum(u/.008,1)*np.exp(-u*8))
    noise = rng.standard_normal(len(t))
    air = np.convolve(noise, np.ones(31)/31, mode='same')
    for start, end, gain, pan in [(2.75,3.65,.11,-.2),(7.35,8.65,.14,.2),(8.75,9.35,.09,0)]:
        x = np.clip((t-start)/(end-start), 0, 1)
        mix(air*np.sin(np.pi*x)**2*gain, pan)
    for start, frequency, gain, pan in [(9.12,329.63,.11,-.15),(9.26,493.88,.075,.15),(9.41,659.26,.047,0)]:
        u = np.maximum(0,t-start)
        bell = np.sin(2*np.pi*frequency*u)+.17*np.sin(2*np.pi*frequency*2.003*u)*np.exp(-u*3)
        note = bell*(t>=start)*np.minimum(u/.01,1)*np.exp(-u*1.4)*gain
        mix(note,pan)
        # Gentle alternating echoes, not a dense musical track.
        for delay, level in [(.21,.20),(.43,.11)]:
            n = int(delay*rate)
            mix(np.concatenate((np.zeros(n),note[:-n]))*level,-pan)
    channels *= np.clip((SECONDS-t)/.5,0,1)[:,None]
    peak = np.max(np.abs(channels))
    channels *= .58/max(peak,1e-6)
    samples = np.clip(channels*32767,-32768,32767).astype('<i2')
    with wave.open(str(OUT/'teaser-original-sound.wav'),'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(samples.tobytes())
    print(f'SOUND: original stereo synth, {SECONDS}s, peak {20*np.log10(np.max(np.abs(channels))):.1f} dBFS', flush=True)


sound_bed()
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.name = 'Qubit — Teaser photography'
scene.render.fps = FPS
bpy.ops.import_scene.gltf(filepath=str(BASE.parent/'public/smarter-orb.glb'), export_import_convert_lighting_mode='COMPAT')

# The imported asset is never edited on disk. Reveal-sensitive geometry is
# excluded from this scene altogether, even if the shell shifts a little.
plate_names = ['Crown','Chin','Side','RearCap']
kept_meshes = {p+'Armor' for p in plate_names} | {p+'Seam_default' for p in plate_names}
for obj in list(scene.objects):
    obj.animation_data_clear()
    if obj.type == 'MESH' and obj.data.shape_keys:
        obj.data.shape_keys.animation_data_clear()
    if obj.type == 'LIGHT' or (obj.type == 'MESH' and obj.name not in kept_meshes):
        bpy.data.objects.remove(obj, do_unlink=True)
for name in ['Body','ArmorRig',*plate_names]:
    obj = bpy.data.objects[name]
    obj.location = (0,0,0)
    obj.rotation_mode = 'QUATERNION'
    obj.rotation_quaternion = (1,0,0,0)
    obj.scale = (1,1,1)
for name in plate_names:
    bpy.data.objects[name+'Trim_default'].scale = (1,1,1)

hero = bpy.data.objects.new('Teaser turntable',None)
scene.collection.objects.link(hero)
bpy.data.objects['Smarter'].parent = hero
armor = [bpy.data.objects[p+'Armor'] for p in plate_names]
seams = [bpy.data.objects[p+'Seam_default'] for p in plate_names]
for obj in armor:
    material = obj.data.materials[0]
    principled = material.node_tree.nodes.get('Principled BSDF')
    principled.inputs['Roughness'].default_value = .42
    principled.inputs['Metallic'].default_value = .58
rim_material, rim_emission = emission('Signal / cyan',(.015,.85,1),2.5)
for obj in seams:
    obj.data.materials.clear()
    obj.data.materials.append(rim_material)

# Only a featureless interior glow is visible through the nearly closed seams.
bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=32, radius=1.01)
glow_core = bpy.context.object
glow_core.name = 'Unrevealed internal signal'
glow_core.parent = hero
glow_material, core_emission = emission('Hidden cyan energy',(.0,.7,1),1.3)
glow_core.data.materials.append(glow_material)
for polygon in glow_core.data.polygons:
    polygon.use_smooth = True

bpy.ops.mesh.primitive_plane_add(size=200, location=(0,0,-1.49))
floor = bpy.context.object
floor.name = 'Midnight studio floor'
floor_material = bpy.data.materials.new('Midnight graphite')
floor_material.use_nodes = True
bsdf = floor_material.node_tree.nodes.get('Principled BSDF')
bsdf.inputs['Base Color'].default_value = (.003,.008,.012,1)
bsdf.inputs['Roughness'].default_value = .45
bsdf.inputs['Metallic'].default_value = .2
floor.data.materials.append(floor_material)
scene.world.use_nodes = True
background = scene.world.node_tree.nodes.get('Background')
background.inputs[0].default_value = (.003,.008,.013,1)
background.inputs[1].default_value = .3

lamps = []
for name, position, power, color, size in [
    ('Soft edge',(-3,-4,5),100,(.62,.82,1),5),
    ('Cyan rim',(2.5,2,2.5),170,(.05,.75,1),2.5),
    ('Quiet fill',(-2,-2,.2),14,(.12,.7,.85),3),
]:
    bpy.ops.object.light_add(type='AREA', location=position)
    obj = bpy.context.object
    obj.name = name
    obj.data.energy = power
    obj.data.color = color
    obj.data.shape = 'DISK'
    obj.data.size = size
    obj.rotation_euler = (-obj.location).to_track_quat('-Z','Y').to_euler()
    lamps.append((obj,power))

bpy.ops.object.camera_add()
camera = bpy.context.object
camera.name = 'Teaser camera'
camera.data.type = 'ORTHO'
camera.data.clip_start = .01
camera.data.clip_end = 100
scene.camera = camera
font = bpy.data.fonts.load('/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf')
regular = bpy.data.fonts.load('/System/Library/Fonts/Supplemental/Arial.ttf')
labels = []


def label(name, body, u, v, size, start, end, color=(.78,.9,.93), align='LEFT', tracking=1., bold=True):
    data = bpy.data.curves.new(name,'FONT')
    data.body = body
    data.align_x = align
    data.align_y = 'CENTER'
    data.size = size
    data.space_character = tracking
    data.space_line = 1.18
    data.font = font if bold else regular
    obj = bpy.data.objects.new(name,data)
    scene.collection.objects.link(obj)
    obj.parent = camera
    material, node = emission(name+' / ink',color)
    data.materials.append(material)
    labels.append((obj,node,u,v,start,end))
    return obj


label('Opening whisper','A small signal.',.085,.19,.039,.65,2.9)
label('Studio signature','SMARTER DEV',.085,.865,.0105,.25,8.55,color=(.18,.7,.76),tracking=1.35)
label('The question','Something\nis waking up.',.085,.54,.060,3.25,8.4)
label('Transmission','TRANSMISSION  /  001',.087,.31,.0105,3.6,8.25,color=(.2,.57,.64),tracking=1.25,bold=False)
label('End eyebrow','FOR THE SMARTER DEV DISCORD',.5,.62,.011,9.15,12,color=(.16,.72,.79),align='CENTER',tracking=1.25)
label('End title','COMING SOON',.5,.48,.067,9.15,12,align='CENTER',tracking=1.08)
label('End promise','Stay curious.',.5,.335,.018,9.5,12,color=(.36,.52,.58),align='CENTER',bold=False)

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
scene.view_settings.exposure = -.3
scene.frame_start = 1
scene.frame_end = FPS*SECONDS

tree = bpy.data.node_groups.new('Teaser / optical bloom','CompositorNodeTree')
scene.compositing_node_group = tree
tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
layers = tree.nodes.new('CompositorNodeRLayers')
glow = tree.nodes.new('CompositorNodeGlare')
glow.inputs['Type'].default_value = 'Fog Glow'
glow.inputs['Quality'].default_value = 'High'
glow.inputs['Threshold'].default_value = 1.15
glow.inputs['Strength'].default_value = .4
glow.inputs['Size'].default_value = .5
output = tree.nodes.new('NodeGroupOutput')
tree.links.new(layers.outputs['Image'],glow.inputs['Image'])
tree.links.new(glow.outputs['Image'],output.inputs['Image'])


def update_frame(current, *_):
    if current != scene:
        return
    t = (current.frame_current-1)/FPS
    in_macro = t < 3.05
    if in_macro:
        x = smooth(0,3.05,t)
        hero.location = (.25,0,-.02)
        hero.rotation_euler = (.04, .1, -.34+.1*x)
        camera.location = (1.3,-6,2.4)
        target = Vector((.14,-.25,.63))
        camera.data.ortho_scale = 2.9-.20*x
    else:
        x = smooth(3.05,8.9,t)
        hero.location = (1.48,0,-.05+.035*math.sin(t*1.1))
        hero.rotation_euler = (.03, -.04, -.22+.48*x)
        camera.location = (0,-8,1.1)
        target = Vector((0,0,.1))
        camera.data.ortho_scale = 7.25-.30*x
    camera.rotation_euler = (target-camera.location).to_track_quat('-Z','Y').to_euler()
    pulse = sum(gain*math.exp(-((t-center)/width)**2) for center,width,gain in [(.45,.14,.9),(1.08,.12,.5),(3.25,.22,.8),(6.05,.17,.6),(6.7,.14,.4),(8.25,.25,1.8)])
    cut = 1-.92*math.exp(-((t-3.03)/.10)**2)
    visibility = (.30+.70*smooth(0,.85,t))*(1-smooth(8.55,9.05,t))*cut
    rim_emission.inputs['Strength'].default_value = (1.8+pulse*4)*visibility
    core_emission.inputs['Strength'].default_value = (1+pulse*2)*visibility
    for obj,power in lamps:
        obj.data.energy = power*visibility*(.55 if in_macro else .9)
    for obj in [*armor,*seams,glow_core]:
        obj.hide_render = t>=9.05
    # An almost imperceptible breath, never an opening that reveals a face.
    gap = .006+.016*math.exp(-((t-8.22)/.38)**2)
    for name in plate_names:
        plate = bpy.data.objects[name]
        direction = Vector(plate.get('openDirection',(0,0,0)))
        plate.location = direction*gap
    width = camera.data.ortho_scale
    for obj,node,u,v,start,end in labels:
        fade = window(t,start,end,.30)
        obj.hide_render = fade<=.0001
        obj.location = ((u-.5)*width,(v-.5)*width*9/16,-4)
        obj.scale = (width,width,width)
        node.inputs['Strength'].default_value = fade


bpy.app.handlers.frame_change_pre.append(update_frame)
scene.frame_set(1)
assert not any(o.name.startswith('Face') and o.type=='MESH' for o in scene.objects)
assert not any('QUBIT' in o.data.body.upper() for o in scene.objects if o.type=='FONT')
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'qubit-teaser.blend'))
if args.preview:
    for seconds in [.9,2.1,4.1,6.3,8.25,10.5]:
        scene.frame_set(round(seconds*FPS)+1)
        scene.render.filepath = str(OUT/f'preview-{seconds:.2f}.png')
        bpy.ops.render.render(write_still=True)
    print('PREVIEW COMPLETE',flush=True)
else:
    if not args.encode_only:
        scene.render.filepath = str(FRAMES/'frame-')
        bpy.ops.render.render(animation=True)
    assert all((FRAMES/f'frame-{frame:04d}.png').exists() for frame in range(1,FPS*SECONDS+1)), 'Missing rendered frames'
    # Encoding in the sequencer avoids any real-time/browser frame throttling.
    edit = bpy.data.scenes.new('Qubit — Teaser master')
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
    strip = sequence.strips.new_image('Picture',str(FRAMES/'frame-0001.png'),channel=1,frame_start=1)
    for frame in range(2,FPS*SECONDS+1):
        strip.elements.append(f'frame-{frame:04d}.png')
    sequence.strips.new_sound('Original sound design',str(OUT/'teaser-original-sound.wav'),channel=2,frame_start=1)
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
    edit.render.filepath = str(OUT/'smarter-dev-teaser.mp4')
    bpy.context.window.scene = edit
    bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'qubit-teaser.blend'))
    bpy.ops.render.render(animation=True,scene=edit.name)
    print(f'TEASER COMPLETE: {edit.render.filepath}',flush=True)
