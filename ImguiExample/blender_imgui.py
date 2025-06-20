# ##### BEGIN GPL LICENSE BLOCK #####
#
#    Copyright (c) 2020 Elie Michel
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

import bpy
from bpy.types import SpaceView3D
import gpu
from gpu_extras.batch import batch_for_shader

try:
    import imgui
except ModuleNotFoundError:
    print("ERROR: imgui was not found, run 'python -m pip install imgui' using Blender's Python.")
from imgui.integrations.base import BaseOpenGLRenderer

import numpy as np
import ctypes as C

class BlenderImguiRenderer(BaseOpenGLRenderer):
    """Integration of ImGui into Blender."""
    VERTEX_SHADER_SRC = """
        void main() {
            Frag_UV = UV;
            Frag_Color = Color;

            gl_Position = ProjMtx * vec4(Position.xy, 0, 1);
        }
        """

    FRAGMENT_SHADER_SRC = """
        vec4 linear_to_srgb(vec4 linear) {
            return mix(
                1.055 * pow(linear, vec4(1.0 / 2.4)) - 0.055,
                12.92 * linear,
                step(linear, vec4(0.00031308))
            );
        }

        vec4 srgb_to_linear(vec4 srgb) {
            return mix(
                pow((srgb + 0.055) / 1.055, vec4(2.4)),
                srgb / 12.92,
                step(srgb, vec4(0.04045))
            );
        }

        void main() {
            Out_Color = Frag_Color * texture(Texture, Frag_UV.st);
            Out_Color.rgba = srgb_to_linear(Out_Color.rgba);
        }
        """

    def __init__(self):
        self._shader_handle = None
        self._vert_handle = None
        self._fragment_handle = None

        self._attrib_location_tex = None
        self._attrib_proj_mtx = None
        self._attrib_location_position = None
        self._attrib_location_uv = None
        self._attrib_location_color = None

        self._vbo_handle = None
        self._elements_handle = None
        self._vao_handle = None

        self._texture : gpu.types.GPUTexture = None

        super().__init__()

    def refresh_font_texture(self):
        self.io.fonts.add_font_default()
        self.io.fonts.get_tex_data_as_rgba32()

        width, height, imgui_pixels = self.io.fonts.get_tex_data_as_rgba32()

        pixels_float = np.frombuffer(imgui_pixels, dtype=np.uint8)
        pixels_float = pixels_float.astype('f') / 255.0

        buffer = gpu.types.Buffer('FLOAT', 4 * width * height, pixels_float)
        self._texture = gpu.types.GPUTexture(size=(width, height), data=buffer, format='RGBA32F')

        self.io.fonts.texture_id = self._texture
        self.io.fonts.clear_tex_data()

    def _create_device_objects(self):
        shader_info = gpu.types.GPUShaderCreateInfo()
        shader_vertex_outs = gpu.types.GPUStageInterfaceInfo("imgui_shader_interface")
        shader_info.push_constant('MAT4', "ProjMtx")
        shader_info.vertex_in(0, 'VEC2', "Position")
        shader_info.vertex_in(1, 'VEC2', "UV")
        shader_info.vertex_in(2, 'VEC4', "Color")
        shader_vertex_outs.no_perspective('VEC2', "Frag_UV")
        shader_vertex_outs.no_perspective('VEC4', "Frag_Color")
        shader_info.vertex_out(shader_vertex_outs)
        shader_info.sampler(0,'FLOAT_2D', "Texture")
        shader_info.fragment_out(0,'VEC4', "Out_Color")

        shader_info.vertex_source(self.VERTEX_SHADER_SRC)
        shader_info.fragment_source(self.FRAGMENT_SHADER_SRC)

        shader = gpu.shader.create_from_info(shader_info)
        self._bl_shader = shader
        del shader_info
        del shader_vertex_outs

    def render(self, draw_data):
        io = self.io
        shader = self._bl_shader

        display_width, display_height = io.display_size
        fb_width = int(display_width * io.display_fb_scale[0])
        fb_height = int(display_height * io.display_fb_scale[1])

        if fb_width == 0 or fb_height == 0:
            return

        draw_data.scale_clip_rects(*io.display_fb_scale)

        last_blend = gpu.state.blend_get()

        gpu.state.blend_set('ALPHA')
        gpu.state.face_culling_set('NONE')
        gpu.state.scissor_test_set(True)

        gpu.state.viewport_set(0, 0, int(fb_width), int(fb_height))

        ortho_projection = (
             2.0/display_width, 0.0,                   0.0, 0.0,
             0.0,               2.0/-display_height,   0.0, 0.0,
             0.0,               0.0,                  -1.0, 0.0,
            -1.0,               1.0,                   0.0, 1.0
        )
        shader.bind()
        shader.uniform_float("ProjMtx", ortho_projection)
        shader.uniform_int("Texture", 0)

        for commands in draw_data.commands_lists:
            size = commands.idx_buffer_size * imgui.INDEX_SIZE // 4
            address = commands.idx_buffer_data
            ptr = C.cast(address, C.POINTER(C.c_int))
            idx_buffer_np = np.ctypeslib.as_array(ptr, shape=(size,))

            size = commands.vtx_buffer_size * imgui.VERTEX_SIZE // 4
            address = commands.vtx_buffer_data
            ptr = C.cast(address, C.POINTER(C.c_float))
            vtx_buffer_np = np.ctypeslib.as_array(ptr, shape=(size,))
            vtx_buffer_shaped = vtx_buffer_np.reshape(-1, imgui.VERTEX_SIZE // 4)

            idx_buffer_offset = 0
            for command in commands.commands:
                x, y, z, w = command.clip_rect
                gpu.state.scissor_set(int(x), int(fb_height - w), int(z - x), int(w - y))

                vertices = vtx_buffer_shaped[:, :2]
                uvs = vtx_buffer_shaped[:, 2:4]
                colors = vtx_buffer_shaped.view(np.uint8)[:, 4 * 4:]
                colors = colors.astype('f') / 255.0

                indices = idx_buffer_np[idx_buffer_offset:idx_buffer_offset + command.elem_count]

                shader.uniform_sampler("Texture", command.texture_id)

                batch = batch_for_shader(shader, 'TRIS', {
                    "Position": vertices,
                    "UV": uvs,
                    "Color": colors,
                }, indices=indices)
                batch.draw(shader)

                idx_buffer_offset += command.elem_count

        # restore modified gpu state
        gpu.state.blend_set(last_blend)
        gpu.state.scissor_test_set(False)

    # Blender's GPU api seems to manage state fine enough,
    # or at least much better than with BGL.
    # Until proven otherwise, texture cleanup and state backup are removed.
    def _invalidate_device_objects(self):
        pass

    def _backup_integers(self, *keys_and_lengths):
        pass


# -------------------------------------------------------------------

class GlobalImgui:
    # Simple Singleton pattern, use GlobalImgui.get() rather
    # than creating your own instances of this calss
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = GlobalImgui()
        return cls._instance

    def __init__(self):
        self.imgui_ctx = None

    def init_imgui(self):
        self.imgui_ctx = imgui.create_context()
        self.imgui_backend = BlenderImguiRenderer()
        self.setup_key_map()
        self.draw_handlers = {}
        self.callbacks = {}
        self.next_callback_id = 0

    def shutdown_imgui(self):
        for SpaceType, draw_handler in self.draw_handlers.items():
            SpaceType.draw_handler_remove(draw_handler, 'WINDOW')
        imgui.destroy_context(self.imgui_ctx)
        self.imgui_ctx = None

    def handler_add(self, callback, SpaceType):
        """
        @param callback The draw function to add
        @param SpaceType Can be any class deriving from bpy.types.Space
        @return An identifing handle that must be provided to handler_remove in
                order to remove this callback.
        """
        if self.imgui_ctx is None:
            self.init_imgui()

        if SpaceType not in self.draw_handlers:
            self.draw_handlers[SpaceType] = SpaceType.draw_handler_add(self.draw, (SpaceType,), 'WINDOW', 'POST_PIXEL')

        handle = self.next_callback_id
        self.next_callback_id += 1

        self.callbacks[handle] = (callback, SpaceType)

        return handle

    def handler_remove(self, handle):
        if handle not in self.callbacks:
            print(f"Error: invalid imgui callback handle: {handle}")
            return

        del self.callbacks[handle]
        if not self.callbacks:
            self.shutdown_imgui()

    def draw(self, CurrentSpaceType):
        context = bpy.context
        region = context.region
        io = imgui.get_io()
        io.display_size = region.width, region.height
        io.font_global_scale = context.preferences.view.ui_scale
        imgui.new_frame()

        for cb, SpaceType in self.callbacks.values():
            if SpaceType == CurrentSpaceType:
                cb(context)

        imgui.end_frame()
        imgui.render()
        self.imgui_backend.render(imgui.get_draw_data())

    def setup_key_map(self):
        io = imgui.get_io()
        keys = (
            imgui.KEY_TAB,
            imgui.KEY_LEFT_ARROW,
            imgui.KEY_RIGHT_ARROW,
            imgui.KEY_UP_ARROW,
            imgui.KEY_DOWN_ARROW,
            imgui.KEY_HOME,
            imgui.KEY_END,
            imgui.KEY_INSERT,
            imgui.KEY_DELETE,
            imgui.KEY_BACKSPACE,
            imgui.KEY_ENTER,
            imgui.KEY_ESCAPE,
            imgui.KEY_PAGE_UP,
            imgui.KEY_PAGE_DOWN,
            imgui.KEY_A,
            imgui.KEY_C,
            imgui.KEY_V,
            imgui.KEY_X,
            imgui.KEY_Y,
            imgui.KEY_Z,
        )
        for k in keys:
            # We don't directly bind Blender's event type identifiers
            # because imgui requires the key_map to contain integers only
            io.key_map[k] = k


# -------------------------------------------------------------------

def imgui_handler_add(callback, SpaceType):
    return GlobalImgui.get().handler_add(callback, SpaceType)


def imgui_handler_remove(handle):
    GlobalImgui.get().handler_remove(handle)


# -------------------------------------------------------------------

class ImguiBasedOperator:
    """Base class to derive from when writing an imgui-based operator"""
    key_map = {
        'TAB': imgui.KEY_TAB,
        'LEFT_ARROW': imgui.KEY_LEFT_ARROW,
        'RIGHT_ARROW': imgui.KEY_RIGHT_ARROW,
        'UP_ARROW': imgui.KEY_UP_ARROW,
        'DOWN_ARROW': imgui.KEY_DOWN_ARROW,
        'HOME': imgui.KEY_HOME,
        'END': imgui.KEY_END,
        'INSERT': imgui.KEY_INSERT,
        'DEL': imgui.KEY_DELETE,
        'BACK_SPACE': imgui.KEY_BACKSPACE,
        'RET': imgui.KEY_ENTER,
        'ESC': imgui.KEY_ESCAPE,
        'PAGE_UP': imgui.KEY_PAGE_UP,
        'PAGE_DOWN': imgui.KEY_PAGE_DOWN,
        'A': imgui.KEY_A,
        'C': imgui.KEY_C,
        'V': imgui.KEY_V,
        'X': imgui.KEY_X,
        'Y': imgui.KEY_Y,
        'Z': imgui.KEY_Z,
        'LEFT_CTRL': 128 + 1,
        'RIGHT_CTRL': 128 + 2,
        'LEFT_ALT': 128 + 3,
        'RIGHT_ALT': 128 + 4,
        'LEFT_SHIFT': 128 + 5,
        'RIGHT_SHIFT': 128 + 6,
        'OSKEY': 128 + 7,
    }

    def init_imgui(self, context):
        self.imgui_handle = imgui_handler_add(self.draw, SpaceView3D)

    def shutdown_imgui(self):
        imgui_handler_remove(self.imgui_handle)

    def draw(self, context):
        # This is where you can use any code from pyimgui's doc
        # see https://pyimgui.readthedocs.io/en/latest/
        pass

    def modal_imgui(self, context, event):
        region = context.region
        io = imgui.get_io()

        io.mouse_pos = (event.mouse_region_x, region.height - 1 - event.mouse_region_y)

        if event.type == 'LEFTMOUSE':
            io.mouse_down[0] = event.value == 'PRESS'

        elif event.type == 'RIGHTMOUSE':
            io.mouse_down[1] = event.value == 'PRESS'

        elif event.type == 'MIDDLEMOUSE':
            io.mouse_down[2] = event.value == 'PRESS'

        elif event.type == 'WHEELUPMOUSE':
            io.mouse_wheel = -1

        elif event.type == 'WHEELUPDOWN':
            io.mouse_wheel = +1

        # Enable this for debugging, otherwise it just floods the console and increases our memory footprint
        # print(f"Event type={event.type}, unicode={event.unicode}")

        if event.type in self.key_map:
            if event.value == 'PRESS':
                io.keys_down[self.key_map[event.type]] = True
            elif event.value == 'RELEASE':
                io.keys_down[self.key_map[event.type]] = False

        io.key_ctrl = (
            io.keys_down[self.key_map['LEFT_CTRL']] or
            io.keys_down[self.key_map['RIGHT_CTRL']]
        )

        io.key_alt = (
            io.keys_down[self.key_map['LEFT_ALT']] or
            io.keys_down[self.key_map['RIGHT_ALT']]
        )

        io.key_shift = (
            io.keys_down[self.key_map['LEFT_SHIFT']] or
            io.keys_down[self.key_map['RIGHT_SHIFT']]
        )

        io.key_super = io.keys_down[self.key_map['OSKEY']]

        if event.unicode:
            char = ord(event.unicode)
            if 0 < char < 0x10000:
                io.add_input_character(char)

# -------------------------------------------------------------------

class BlenderImguiOverlay:
    # Make sure this does not conflict with other addons
    bl_idname = "OVERRIDE ME!"

    def draw(self, context):
        # This is where you can use any code from pyimgui's doc
        # see https://pyimgui.readthedocs.io/en/latest/
        pass

# -------------------------------------------------------------------

def register_overlay(cls):
    # Use the driver_namespace to store and retrieve the handle, a bit
    # hacky but reliable.
    handle = imgui_handler_add(cls().draw, SpaceView3D)
    bpy.app.driver_namespace["_imgui_" + cls.bl_idname] = handle

def unregister_overlay(cls):
    handle = bpy.app.driver_namespace.get("_imgui_" + cls.bl_idname)
    if handle is not None:
        imgui_handler_remove(handle)

# -------------------------------------------------------------------
