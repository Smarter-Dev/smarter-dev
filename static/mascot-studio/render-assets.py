"""Blender batch render: blender -b --python render-assets.py -- [output-directory]."""
import bpy
import argparse
import sys
from pathlib import Path
from mathutils import Vector

base = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('output_directory', nargs='?', default=str(base / 'renders'))
parser.add_argument('--mood', action='append', choices=['default', 'happy', 'playful', 'excited', 'working', 'eureka', 'sad', 'mad'])
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
out = Path(args.output_directory)
out.mkdir(parents=True, exist_ok=True)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
# glTF seconds are converted to Blender frame numbers during import.
# Set the rate first, otherwise a four-second clip imports as 96 frames.
bpy.context.scene.render.fps = 30
# Match the web renderer's lighting scale. Blender's default SPEC conversion
# divides imported light energy by 683, drowning the character's underlights
# beneath the studio setup. COMPAT preserves their intended relative output.
bpy.ops.import_scene.gltf(filepath=str(base / 'public/smarter-orb.glb'), export_import_convert_lighting_mode='COMPAT')
for obj in bpy.context.scene.objects:
    if obj.type == 'LIGHT':
        obj.data.shadow_soft_size = .12
        # For engines that honor this multiplier, favor diffuse illumination:
        # the punctual lights stand in for broad under-shell emitters.
        obj.data.specular_factor = .04
# Keep facial graphics readable through the glass without secondary reflected
# faces. Their direct camera visibility and shadows remain intact.
for obj in bpy.data.objects['Face'].children_recursive:
    obj.visible_glossy = False
    obj.visible_transmission = False
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 32
scene.cycles.use_denoising = True
scene.render.resolution_x = scene.render.resolution_y = 768
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.film_transparent = True
scene.view_settings.view_transform = 'Standard'
scene.view_settings.look = 'None'
scene.view_settings.exposure = -.7
tree = bpy.data.node_groups.new('Smarter Glow', 'CompositorNodeTree')
scene.compositing_node_group = tree
tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
layers = tree.nodes.new('CompositorNodeRLayers')
glow = tree.nodes.new('CompositorNodeGlare')
glow.inputs['Type'].default_value = 'Fog Glow'
glow.inputs['Quality'].default_value = 'High'
glow.inputs['Threshold'].default_value = 1.0
glow.inputs['Strength'].default_value = .45
glow.inputs['Size'].default_value = .5
output = tree.nodes.new('NodeGroupOutput')
tree.links.new(layers.outputs['Image'], glow.inputs['Image'])
tree.links.new(glow.outputs['Image'], output.inputs['Image'])
scene.world.color = (.18, .18, .18)
scene.render.fps = 30
scene.frame_end = 120
bpy.ops.object.camera_add(location=(1.8, -6.4, 1.5))
camera = bpy.context.object
camera.rotation_euler = (Vector((0, 0, .25)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 4.3
scene.camera = camera
for name, location, energy, color, size in [
    ('Softbox', (2, -4, 5), 120, (.9, .95, 1), 4),
    ('Neutral rim', (-3, 1, 2), 150, (.9, .95, 1), 3),
    ('Edge', (3, 2, 4), 150, (.9, .95, 1), 3),
    ('Face fill', (-2, -4, 0), 30, (1, 1, 1), 3),
]:
    bpy.ops.object.light_add(type='AREA', location=location)
    light = bpy.context.object
    light.name = name
    light.data.energy = energy
    light.data.color = color
    light.data.shape = 'DISK'
    light.data.size = size
    light.rotation_euler = (-light.location).to_track_quat('-Z', 'Y').to_euler()

# glTF importer stores each clip in named NLA tracks on its animated objects.
animated = [obj for obj in scene.objects if obj.animation_data]
animated += [obj.data.shape_keys for obj in scene.objects if obj.type == 'MESH' and obj.data.shape_keys and obj.data.shape_keys.animation_data]
for track in bpy.data.objects['Body'].animation_data.nla_tracks:
    if track.name in ['default', 'happy', 'playful', 'excited', 'working', 'eureka', 'sad', 'mad']:
        assert abs((track.strips[0].frame_end - track.strips[0].frame_start) / scene.render.fps - 4) < .001, f'Incorrect imported duration: {track.name}'
def select_mood(mood, frame):
    for obj in animated:
        obj.animation_data.action = None
        for track in obj.animation_data.nla_tracks:
            track.mute = track.name != mood
    scene.frame_set(frame)

for mood in args.mood or ['default', 'happy', 'playful', 'excited', 'working', 'eureka', 'sad', 'mad']:
    select_mood(mood, {'playful': 52, 'eureka': 25, 'excited': 31, 'working': 61}.get(mood, 25))
    if mood == 'playful':
        keys = bpy.data.objects['playfulRightEye'].data.shape_keys.key_blocks
        assert sum(i * key.value for i, key in enumerate(keys)) / 8 > .9, 'Playful wink did not survive import'
    scene.render.filepath = str(out / f'smarter-{mood}.png')
    bpy.ops.render.render(write_still=True)
select_mood('default', 25)
bpy.ops.wm.save_as_mainfile(filepath=str(out / 'smarter-studio.blend'))
