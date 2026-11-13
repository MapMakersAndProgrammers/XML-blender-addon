bl_info = {
    "name": "Tanki XML Map Importer/Exporter",
    "author": "MapMakers Conglomerate",
    "version": (6, 7, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > Tanki Map (.xml)",
    "description": "load and export tanki maps",
    "warning": "turn on the 3ds importer addon",
    "category": "Import-Export",
}

import bpy
import os
import re
import math
import struct
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from mathutils import Vector, Matrix

from bpy.props import (
    StringProperty, BoolProperty, EnumProperty, FloatProperty,
    PointerProperty,
)
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.types import Operator, PropertyGroup, AddonPreferences


BONUS_TYPES = ["damageup", "armorup", "nitro", "medkit", "crystal", "crystal_100", "overdrive"]
GAME_MODES = ["dm", "tdm", "ctf", "dom", "asl", "sge", "jgr", "rgb"]
CONTROL_POINT_MODES = ["dom", "sge"]
SPAWN_TYPES = ["dm", "blue", "red", "dom", "dom-red", "dom-blue"]
KILL_ACTIONS = ["kill", "kick", "block"]
LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

FUNC_LIBRARY = "Functional"
GRP_SPAWN_POINTS = "Spawn Points"
GRP_BONUS_REGIONS = "Bonus Regions"
GRP_FLAGS = "Flags"
GRP_DOMINATION = "Domination"
KILL_GEOMETRY = "special-geometry"
KILL_BOX_NAME = "special-box"
FREE_BONUS_NAME = "free-bonus"

COL_RED = (0.894, 0.278, 0.0, 1.0)
COL_BLUE = (0.047, 0.004, 1.0, 1.0)
COL_YELLOW = (0.886, 0.749, 0.004, 1.0)
COL_DOM = (1.0, 0.0, 1.0, 0.6)
COL_COLLISION = (0.2, 1.0, 0.3, 1.0)

SPAWN_COLORS = {
    "dm": COL_YELLOW, "dom": COL_YELLOW,
    "blue": COL_BLUE, "dom-blue": COL_BLUE,
    "red": COL_RED, "dom-red": COL_RED,
}

COLLISION_PREFIXES = ("box", "plane", "tri")
SKIP_PREFIXES = ("occ",)


_LIB_CACHE = {}
_ASSET_CACHE = {}
_MAT_CACHE = {}
_MESH_VARIANTS = {}


def clear_caches():
    _LIB_CACHE.clear()
    _ASSET_CACHE.clear()
    _MAT_CACHE.clear()
    _MESH_VARIANTS.clear()


def fnum(value, precision=3):
    s = f"{value:.{precision}f}"
    if s.startswith("-") and float(s) == 0.0:
        s = s[1:]
    return s


def rawnum(value):
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def norm_name(name):
    return name.lower().replace(" ", "_").replace("-", "_")


def get_prefs(context):
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def elem_float(parent, tag, default=0.0):
    el = parent.find(tag)
    if el is not None and el.text:
        try:
            return float(el.text)
        except ValueError:
            pass
    return default


def read_position(elem):
    if elem is None:
        return None
    if elem.get("x") is not None:
        return Vector((float(elem.get("x", 0)), float(elem.get("y", 0)), float(elem.get("z", 0))))
    return Vector((elem_float(elem, "x"), elem_float(elem, "y"), elem_float(elem, "z")))


def find_position(parent):
    return read_position(parent.find("position"))


class LibraryDef:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.props = {}
        self.image_redirect = {}
        self._dir_listing = None

    def resolve_file(self, filename):
        if not filename:
            return None
        cand = os.path.join(self.path, filename)
        if os.path.exists(cand):
            return cand
        cand = os.path.join(self.path, filename.lower())
        if os.path.exists(cand):
            return cand
        if self._dir_listing is None:
            try:
                self._dir_listing = {f.lower(): f for f in os.listdir(self.path)}
            except OSError:
                self._dir_listing = {}
        real = self._dir_listing.get(filename.lower())
        if real:
            return os.path.join(self.path, real)
        return None

    def find_prop(self, group, prop):
        d = self.props.get((group, prop))
        if d is not None:
            return d
        key = (group.lower(), prop.lower())
        for (g, p), pd in self.props.items():
            if (g.lower(), p.lower()) == key:
                return pd
        return None


def parse_library(lib_dir):
    lib_xml_path = os.path.join(lib_dir, "library.xml")
    if not os.path.exists(lib_xml_path):
        return None
    try:
        root = ET.parse(lib_xml_path).getroot()
    except Exception as e:
        print(f"[Tanki] cant read {lib_xml_path}: {e}")
        return None

    lib = LibraryDef(root.get("name") or os.path.basename(lib_dir), lib_dir)

    images_path = os.path.join(lib_dir, "images.xml")
    if os.path.exists(images_path):
        try:
            iroot = ET.parse(images_path).getroot()
            for image in iroot.iter("image"):
                orig = (image.get("name") or "").lower()
                if orig:
                    lib.image_redirect[orig] = (image.get("new-name"), image.get("alpha"))
        except Exception as e:
            print(f"[Tanki] cant read {images_path}: {e}")

    def parse_prop(prop_elem, group_name):
        pname = prop_elem.get("name")
        if not pname:
            return
        mesh = prop_elem.find("mesh")
        sprite = prop_elem.find("sprite")
        if mesh is not None:
            textures = {}
            for tex in mesh.findall("texture"):
                tname = tex.get("name")
                if tname:
                    textures[tname] = (tex.get("diffuse-map"), tex.get("opacity-map"))
            lib.props[(group_name, pname)] = {
                "kind": "mesh",
                "file": mesh.get("file"),
                "object": mesh.get("object"),
                "textures": textures,
            }
        elif sprite is not None:
            lib.props[(group_name, pname)] = {
                "kind": "sprite",
                "file": sprite.get("file"),
                "alpha": sprite.get("alpha"),
                "origin_x": float(sprite.get("origin-x", 0.5)),
                "origin_y": float(sprite.get("origin-y", 1.0)),
                "scale": float(sprite.get("scale", 1.0)),
            }

    def walk_group(group_elem, group_name):
        for prop_elem in group_elem.findall("prop"):
            parse_prop(prop_elem, group_name)
        for sub in group_elem.findall("prop-group"):
            walk_group(sub, sub.get("name") or group_name)

    walk_group(root, lib.name)
    return lib


def get_library(context, lib_name):
    key = norm_name(lib_name)
    if key in _LIB_CACHE:
        return _LIB_CACHE[key]

    prefs = get_prefs(context)
    base_dir = prefs.prop_libs_directory if prefs else ""
    lib = None
    if base_dir and os.path.isdir(base_dir):
        cand = os.path.join(base_dir, lib_name)
        if os.path.isdir(cand):
            lib = parse_library(cand)
        if lib is None:
            try:
                for f in os.listdir(base_dir):
                    full = os.path.join(base_dir, f)
                    if os.path.isdir(full) and norm_name(f) == key:
                        lib = parse_library(full)
                        if lib:
                            break
            except OSError:
                pass
        if lib is None:
            try:
                for f in os.listdir(base_dir):
                    full = os.path.join(base_dir, f)
                    if os.path.isdir(full) and os.path.exists(os.path.join(full, "library.xml")):
                        parsed = parse_library(full)
                        if parsed and norm_name(parsed.name) == key:
                            lib = parsed
                            break
            except OSError:
                pass
    if lib is None:
        print(f"[Tanki] no library called {lib_name}")
    _LIB_CACHE[key] = lib
    return lib


def _import_3ds(context, filepath):
    pre = set(bpy.data.objects)
    ok = False
    for op_call in ("max3ds", "autodesk_3ds"):
        op = getattr(bpy.ops.import_scene, op_call, None)
        if op is not None:
            try:
                op(filepath=filepath)
                ok = True
                break
            except Exception as e:
                print(f"[Tanki] 3ds load broke for {filepath}: {e}")
                break
    if not ok and not (set(bpy.data.objects) - pre):
        raise RuntimeError(
            "3ds importer isnt working, turn on the Autodesk 3DS addon"
        )
    return [o for o in bpy.data.objects if o not in pre]


def _strip_suffix(name):
    return re.sub(r"\.\d+$", "", name)


def _collision_kind(obj_name):
    lname = _strip_suffix(obj_name).lower()
    for p in SKIP_PREFIXES:
        if lname.startswith(p):
            return None
    for p in COLLISION_PREFIXES:
        if lname.startswith(p):
            return "plane" if p == "plane" else ("box" if p == "box" else "tri")
    return None


def _make_collision_source(obj):

    kind = _collision_kind(obj.name)
    if kind is None or obj.type != 'MESH':
        return None
    me = obj.data
    if not me.vertices:
        return None
    loc, rot_q, _scale = obj.matrix_basis.decompose()
    rot = rot_q.to_matrix()
    verts = [v.co.copy() for v in me.vertices]
    tri = None
    if kind in ("plane", "tri"):
        me.calc_loop_triangles()
        if not me.loop_triangles:
            return None
        tri = tuple(me.loop_triangles[0].vertices)
    return {"kind": kind, "verts": verts, "tri": tri, "rot": rot.copy(), "loc": loc.copy()}


def _pick_main_object(mesh_objects, object_name):
    if object_name:
        wanted = object_name.lower()
        for obj in mesh_objects:
            n = _strip_suffix(obj.name).lower()
            if n == wanted or n.startswith(wanted) or wanted.startswith(n):
                return obj
    candidates = [o for o in mesh_objects
                  if _collision_kind(o.name) is None
                  and not _strip_suffix(o.name).lower().startswith(SKIP_PREFIXES)]
    if not candidates:
        candidates = list(mesh_objects)
    roots = [o for o in candidates if o.parent is None]
    if roots:
        candidates = roots
    with_mats = [o for o in candidates if o.data.materials and len(o.data.materials) > 0]
    pool = with_mats if with_mats else candidates
    pool.sort(key=lambda o: len(o.data.vertices), reverse=True)
    return pool[0] if pool else None


def ensure_asset(context, lib_name, group, prop):
    key = (norm_name(lib_name), group, prop)
    if key in _ASSET_CACHE:
        asset = _ASSET_CACHE[key]
        if asset is None:
            return None
        try:
            if asset.get("mesh") is not None:
                _ = asset["mesh"].name
            return asset
        except ReferenceError:
            del _ASSET_CACHE[key]

    lib = get_library(context, lib_name)
    if lib is None:
        _ASSET_CACHE[key] = None
        return None
    pdef = lib.find_prop(group, prop)
    if pdef is None:
        print(f"[Tanki] prop not in library: {lib_name}/{group}/{prop}")
        _ASSET_CACHE[key] = None
        return None

    asset = {"kind": pdef["kind"], "lib": lib, "def": pdef,
             "mesh": None, "collision": [], "prims": None}

    if pdef["kind"] == "sprite":
        asset["mesh"] = _build_sprite_mesh(lib, prop, pdef)
        _ASSET_CACHE[key] = asset
        return asset

    mesh_path = lib.resolve_file(pdef["file"])
    if mesh_path is None:
        print(f"[Tanki] missing 3ds file: {pdef['file']} in {lib.path}")
        _ASSET_CACHE[key] = None
        return None

    try:
        imported = _import_3ds(context, mesh_path)
    except RuntimeError as e:
        print(f"[Tanki] {e}")
        _ASSET_CACHE[key] = None
        return None
    try:
        mesh_objects = [o for o in imported if o.type == 'MESH']
        main = _pick_main_object(mesh_objects, pdef.get("object"))
        if main is None:
            print(f"[Tanki] nothing usable in {mesh_path}")
            _ASSET_CACHE[key] = None
            return None

        mesh_data = main.data.copy()
        basis = main.matrix_basis.copy()
        basis.translation = Vector((0.0, 0.0, 0.0))
        if basis != Matrix.Identity(4):
            mesh_data.transform(basis)
        mesh_data.name = f"{lib.name}_{group}_{prop}"
        mesh_data.use_fake_user = True
        asset["mesh"] = mesh_data

        for obj in mesh_objects:
            if obj is main:
                continue
            src = _make_collision_source(obj)
            if src:
                asset["collision"].append(src)
    finally:
        for obj in imported:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass

    _ASSET_CACHE[key] = asset
    return asset


def _gif_lzw(data, min_code, count):
    clear = 1 << min_code
    end = clear + 1
    size = min_code + 1
    base = [bytes([i]) for i in range(clear)] + [b"", b""]
    table = list(base)
    out = bytearray()
    prev = None
    bit = 0
    total = len(data) * 8
    while bit + size <= total and len(out) < count:
        i = bit >> 3
        code = (int.from_bytes(data[i:i + 3], "little") >> (bit & 7)) & ((1 << size) - 1)
        bit += size
        if code == clear:
            table = list(base)
            size = min_code + 1
            prev = None
            continue
        if code == end:
            break
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        if len(table) == (1 << size) and size < 12:
            size += 1
    return out[:count]


def _read_gif(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] != b"GIF":
        return None
    flags = data[10]
    pos = 13
    gct = None
    if flags & 0x80:
        n = 3 * (2 << (flags & 7))
        gct = data[pos:pos + n]
        pos += n
    trans = None
    while pos < len(data):
        b = data[pos]
        pos += 1
        if b == 0x21:
            label = data[pos]
            pos += 1
            while True:
                n = data[pos]
                pos += 1
                if n == 0:
                    break
                if label == 0xF9 and n >= 4 and data[pos] & 1:
                    trans = data[pos + 3]
                pos += n
        elif b == 0x2C:
            iw, ih, iflags = struct.unpack("<HHB", data[pos + 4:pos + 9])
            pos += 9
            ct = gct
            if iflags & 0x80:
                n = 3 * (2 << (iflags & 7))
                ct = data[pos:pos + n]
                pos += n
            min_code = data[pos]
            pos += 1
            chunks = bytearray()
            while True:
                n = data[pos]
                pos += 1
                if n == 0:
                    break
                chunks += data[pos:pos + n]
                pos += n
            idx = _gif_lzw(bytes(chunks), min_code, iw * ih)
            idx = idx + bytes(iw * ih - len(idx))
            if iflags & 0x40:
                order = []
                for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)):
                    order.extend(range(start, ih, step))
                rows = [None] * ih
                for k, y in enumerate(order):
                    rows[y] = idx[k * iw:(k + 1) * iw]
                idx = b"".join(rows)
            return iw, ih, idx, ct, trans
        else:
            break
    return None


def _load_gif(path):
    for img in bpy.data.images:
        if img.get("tanki_src") == os.path.normpath(path):
            return img
    try:
        gif = _read_gif(path)
    except Exception as e:
        print(f"[Tanki] cant read gif {path}: {e}")
        return None
    if gif is None or gif[3] is None:
        return None
    w, h, idx, ct, trans = gif
    grey = []
    for c in range(len(ct) // 3):
        r, g, b = ct[c * 3], ct[c * 3 + 1], ct[c * 3 + 2]
        grey.append(0.0 if c == trans else (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0)
    while len(grey) < 256:
        grey.append(0.0)
    px = []
    for y in range(h - 1, -1, -1):
        for v in idx[y * w:(y + 1) * w]:
            gv = grey[v]
            px.extend((gv, gv, gv, 1.0))
    img = bpy.data.images.new(os.path.basename(path), w, h, alpha=False)
    img.pixels.foreach_set(px)
    img["tanki_src"] = os.path.normpath(path)
    try:
        img.pack()
    except Exception:
        pass
    img.use_fake_user = True
    try:
        img.colorspace_settings.name = 'Non-Color'
    except Exception:
        pass
    return img


def _load_image(path):
    if not path or not os.path.exists(path):
        return None
    for img in bpy.data.images:
        try:
            if img.filepath and os.path.normpath(bpy.path.abspath(img.filepath)) == os.path.normpath(path):
                return img
        except Exception:
            continue
    if path.lower().endswith(".gif"):
        return _load_gif(path)
    try:
        img = bpy.data.images.load(path)
        img.use_fake_user = True
    except Exception:
        return None
    if tuple(img.size) == (0, 0):
        print(f"[Tanki] blender cant open {path}")
        bpy.data.images.remove(img)
        return None
    return img


def _full_roughness(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return
    for node in mat.node_tree.nodes:
        if node.type != 'BSDF_PRINCIPLED':
            continue
        for key, value in (("Roughness", 1.0), ("Specular IOR Level", 0.0), ("Specular", 0.0)):
            sock = node.inputs.get(key)
            if sock is not None and not sock.is_linked:
                sock.default_value = value


def _setup_texture_nodes(mat, img, aimg, use_image_alpha):
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (300, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tex_node = nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    tex_node.location = (-500, 200)
    links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])

    alpha_out = None
    if aimg is not None:
        anode = nodes.new("ShaderNodeTexImage")
        anode.image = aimg
        anode.location = (-500, -150)
        try:
            aimg.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
        alpha_out = anode.outputs["Color"]
    elif use_image_alpha:
        try:
            img.alpha_mode = 'STRAIGHT'
        except Exception:
            pass
        alpha_out = tex_node.outputs["Alpha"]

    if alpha_out is not None:
        cut = nodes.new("ShaderNodeMath")
        cut.operation = 'GREATER_THAN'
        cut.inputs[1].default_value = 0.5
        cut.location = (-200, -150)
        links.new(alpha_out, cut.inputs[0])
        links.new(cut.outputs[0], bsdf.inputs["Alpha"])
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'DITHERED'
        else:
            try:
                mat.blend_method = 'CLIP'
                mat.alpha_threshold = 0.5
            except Exception:
                pass
        try:
            mat.shadow_method = 'CLIP'
        except Exception:
            pass
    else:
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'DITHERED'
        else:
            try:
                mat.blend_method = 'OPAQUE'
            except Exception:
                pass
    mat.use_backface_culling = False
    _full_roughness(mat)
    try:
        mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    except Exception:
        pass


def _build_texture_material(lib, mat_name, diffuse_file, opacity_file, use_image_alpha=False):
    cache_key = (norm_name(lib.name), mat_name, diffuse_file or "", opacity_file or "")
    mat = _MAT_CACHE.get(cache_key)
    if mat is not None:
        try:
            _ = mat.name
            return mat
        except ReferenceError:
            del _MAT_CACHE[cache_key]

    if diffuse_file:
        redirect = lib.image_redirect.get(diffuse_file.lower())
        if redirect:
            diffuse_file, opacity_file = redirect[0], redirect[1]

    diffuse_path = lib.resolve_file(diffuse_file) if diffuse_file else None
    if diffuse_path is None:
        return None
    img = _load_image(diffuse_path)
    if img is None:
        return None
    opacity_path = lib.resolve_file(opacity_file) if opacity_file else None
    aimg = _load_image(opacity_path) if opacity_path else None

    full_name = f"{lib.name}_{mat_name}"
    mat = bpy.data.materials.get(full_name)
    if mat is None:
        mat = bpy.data.materials.new(full_name)
        mat.use_fake_user = True
    _setup_texture_nodes(mat, img, aimg, use_image_alpha)

    _MAT_CACHE[cache_key] = mat
    return mat


def get_prop_material(asset, texture_name):
    pdef = asset["def"]
    lib = asset["lib"]
    textures = pdef.get("textures") or {}
    name = texture_name or ""
    entry = None
    used_name = None
    if name and name in textures:
        entry = textures[name]
        used_name = name
    elif name:
        for tn, te in textures.items():
            if tn.lower() == name.lower():
                entry, used_name = te, tn
                break
    if entry is None and not name:
        if "DEFAULT" in textures:
            entry, used_name = textures["DEFAULT"], "DEFAULT"
        elif len(textures) == 1:
            used_name, entry = next(iter(textures.items()))
    if entry is None and name and textures:
        used_name, entry = next(iter(textures.items()))
        print(f"[Tanki] no texture '{name}' for {pdef.get('file')}, using '{used_name}' instead")
    if entry is None:
        return None
    return _build_texture_material(lib, used_name, entry[0], entry[1])


def get_mesh_variant(context, lib_name, group, prop, texture_name):
    asset = ensure_asset(context, lib_name, group, prop)
    if asset is None or asset["mesh"] is None:
        return None, None
    key = (norm_name(lib_name), group, prop, texture_name)
    cached = _MESH_VARIANTS.get(key)
    if cached is not None:
        try:
            _ = cached.name
            return cached, asset
        except ReferenceError:
            del _MESH_VARIANTS[key]

    base = asset["mesh"]
    if asset["kind"] == "sprite":
        material = _sprite_material(asset)
    else:
        material = get_prop_material(asset, texture_name)

    if material is None:
        _MESH_VARIANTS[key] = base
        return base, asset

    if base.materials and len(base.materials) == 1 and base.materials[0] == material:
        variant = base
    else:
        variant = base.copy()
        variant.use_fake_user = True
        variant.materials.clear()
        variant.materials.append(material)
    _MESH_VARIANTS[key] = variant
    return variant, asset


def _build_sprite_mesh(lib, prop_name, pdef):
    name = f"{lib.name}_{prop_name}_sprite"
    existing = bpy.data.meshes.get(name)
    if existing is not None:
        return existing
    scale = pdef.get("scale", 1.0)
    ox = pdef.get("origin_x", 0.5)
    oy = pdef.get("origin_y", 1.0)
    w = 100.0 * scale
    h = 100.0 * scale
    x0, x1 = -ox * w, (1.0 - ox) * w
    z0, z1 = (oy - 1.0) * h, oy * h
    verts = [(x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)]
    faces = [(0, 1, 2, 3)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    uv = me.uv_layers.new(name="UVMap")
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for loop_idx, loop in enumerate(me.polygons[0].loop_indices):
        uv.data[loop].uv = uvs[loop_idx]
    me.use_fake_user = True
    return me


def _sprite_material(asset):
    pdef = asset["def"]
    lib = asset["lib"]
    diffuse = pdef.get("file")
    alpha = pdef.get("alpha")
    return _build_texture_material(lib, f"sprite_{pdef.get('file')}", diffuse, alpha,
                                   use_image_alpha=(alpha is None))


def _frame_matrix(x_axis, y_axis, z_axis, translation):
    m = Matrix((
        (x_axis.x, y_axis.x, z_axis.x, translation.x),
        (x_axis.y, y_axis.y, z_axis.y, translation.y),
        (x_axis.z, y_axis.z, z_axis.z, translation.z),
        (0.0, 0.0, 0.0, 1.0),
    ))
    return m


def _prim_box(src):
    verts = src["verts"]
    if not verts:
        return None
    mn = Vector((min(v.x for v in verts), min(v.y for v in verts), min(v.z for v in verts)))
    mx = Vector((max(v.x for v in verts), max(v.y for v in verts), max(v.z for v in verts)))
    size = mx - mn
    center = (mn + mx) * 0.5
    world_c = src["rot"] @ center + src["loc"]
    mat = src["rot"].to_4x4()
    mat.translation = world_c
    return {"kind": "box", "matrix": mat, "size": size}


def _prim_plane(src):
    tri = src["tri"]
    if tri is None:
        return None
    v = [src["verts"][i] for i in tri]
    e = [v[1] - v[0], v[2] - v[1], v[0] - v[2]]
    lengths = [x.length for x in e]
    m = lengths.index(max(lengths))
    ia = (m + 2) % 3
    ib = (m + 1) % 3
    xv = e[ia].copy()
    yv = -e[ib]
    width = lengths[ia]
    length = lengths[ib]
    center = v[ia] + 0.5 * (xv + yv)
    if xv.length == 0 or yv.length == 0:
        return None
    xh = xv.normalized()
    yh = yv.normalized()
    zh = xh.cross(yh)
    rot = src["rot"] @ _frame_matrix(xh, yh, zh, Vector()).to_3x3()
    mat = rot.to_4x4()
    mat.translation = src["rot"] @ center + src["loc"]
    return {"kind": "plane", "matrix": mat, "width": width, "length": length}


def _prim_tri(src):
    tri = src["tri"]
    if tri is None:
        return None
    v0, v1, v2 = (src["verts"][i].copy() for i in tri)
    centroid = (v0 + v1 + v2) / 3.0
    d10 = v1 - v0
    length10 = d10.length
    if length10 == 0:
        return None
    xh = d10 / length10
    d20 = v2 - v0
    dproj = d20.dot(xh)
    h_sq = d20.length_squared - dproj * dproj
    h = math.sqrt(max(h_sq, 0.0))
    n = xh.cross(d20)
    if n.length == 0:
        return None
    n.normalize()
    yh = n.cross(xh).normalized()
    rot = src["rot"] @ _frame_matrix(xh, yh, n, Vector()).to_3x3()
    mat = rot.to_4x4()
    mat.translation = src["rot"] @ centroid + src["loc"]
    p1 = Vector((length10, 0.0))
    p2 = Vector((dproj, h))
    cx = (p1.x + p2.x) / 3.0
    cy = (p1.y + p2.y) / 3.0
    return {
        "kind": "tri", "matrix": mat,
        "v": [(-cx, -cy), (p1.x - cx, p1.y - cy), (p2.x - cx, p2.y - cy)],
    }


def get_collision_primitives(asset):
    if asset is None:
        return []
    if asset["prims"] is None:
        prims = []
        for src in asset["collision"]:
            fn = {"box": _prim_box, "plane": _prim_plane, "tri": _prim_tri}[src["kind"]]
            p = fn(src)
            if p is not None:
                prims.append(p)
        asset["prims"] = prims
    return asset["prims"]


def _xml_vec3_children(parent, tag, x, y, z, precision=3):
    el = ET.SubElement(parent, tag)
    ET.SubElement(el, "x").text = fnum(x, precision)
    ET.SubElement(el, "y").text = fnum(y, precision)
    ET.SubElement(el, "z").text = fnum(z, precision)
    return el


def emit_collision_xml(prim, prop_matrix, parent):
    final = prop_matrix @ prim["matrix"]
    pos = final.translation
    eul = final.to_3x3().to_euler('XYZ')
    if prim["kind"] == "plane":
        el = ET.SubElement(parent, "collision-plane", {"id": "0"})
        ET.SubElement(el, "width").text = fnum(prim["width"], 3)
        ET.SubElement(el, "length").text = fnum(prim["length"], 3)
    elif prim["kind"] == "box":
        el = ET.SubElement(parent, "collision-box", {"id": "0"})
        s = prim["size"]
        _xml_vec3_children(el, "size", s.x, s.y, s.z, 3)
    else:
        el = ET.SubElement(parent, "collision-triangle", {"id": "0"})
        for i, (vx, vy) in enumerate(prim["v"]):
            _xml_vec3_children(el, f"v{i}", vx, vy, 0.0, 3)
    _xml_vec3_children(el, "position", pos.x, pos.y, pos.z, 3)
    _xml_vec3_children(el, "rotation", eul.x, eul.y, eul.z, 6)


ROLE_ITEMS = [
    ('NONE', "None", "Not part of the Tanki map"),
    ('PROP', "Prop", "Static geometry prop (mesh or sprite)"),
    ('SPAWN', "Spawn Point", "Tank spawn point"),
    ('BONUS', "Bonus Region", "Bonus drop region"),
    ('KILL', "Kill Zone", "Kill/kick/block zone (special-geometry)"),
    ('FLAG', "CTF Flag", "Capture-the-flag flag base"),
    ('DOM', "Domination Point", "Domination control point"),
    ('PREVIEW', "Collision Preview", "Visualization only, ignored on export"),
]


def _make_settings_annotations():
    d = {
        "role": EnumProperty(name="Tanki Role", items=ROLE_ITEMS, default='NONE'),
        "library": StringProperty(name="Library"),
        "group": StringProperty(name="Group"),
        "prop_name": StringProperty(name="Prop"),
        "texture": StringProperty(name="Texture"),
        "is_sprite": BoolProperty(name="Sprite", default=False),
        "free": BoolProperty(name="Free (off-grid)", default=False,
                             description="not snapped to the grid"),
        "no_collision": BoolProperty(name="Disable Collision", default=False,
                                     description="no collision for this one"),
        "spawn_type": EnumProperty(name="Spawn Type",
                                   items=[(t, t, "") for t in SPAWN_TYPES], default='dm'),
        "bonus_name": StringProperty(name="Region Name", default=FREE_BONUS_NAME),
        "parachute": BoolProperty(name="Parachute", default=True),
        "action": EnumProperty(name="Action", items=[(a, a, "") for a in KILL_ACTIONS], default='kill'),
        "flag_team": EnumProperty(name="Team", items=[('red', "Red", ""), ('blue', "Blue", "")], default='red'),
        "point_name": EnumProperty(name="Point Letter", items=[(l, l, "") for l in LETTERS], default='A'),
    }
    for t in BONUS_TYPES:
        d["bt_" + t] = BoolProperty(name=t, default=(t == "damageup"))
    for m in GAME_MODES:
        d["gm_" + m] = BoolProperty(name=m, default=(m in ("dm", "tdm", "ctf", "dom")))
    for m in CONTROL_POINT_MODES:
        d["dom_" + m] = BoolProperty(name=m, default=(m == "dom"))
    return d


class TankiObjectSettings(PropertyGroup):
    pass


TankiObjectSettings.__annotations__ = _make_settings_annotations()


def _viz_material(name, rgba):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_fake_user = True
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (rgba[0], rgba[1], rgba[2], 1.0)
            try:
                bsdf.inputs["Alpha"].default_value = rgba[3]
            except KeyError:
                pass
        if rgba[3] < 1.0:
            try:
                mat.blend_method = 'BLEND'
            except Exception:
                pass
        mat.diffuse_color = rgba
    _full_roughness(mat)
    return mat


def _cuboid_mesh(name, mn, mx):
    me = bpy.data.meshes.get(name)
    if me is not None:
        return me
    x0, y0, z0 = mn
    x1, y1, z1 = mx
    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    me.use_fake_user = True
    return me


def _spawn_arrow_mesh():
    name = "TANKI_spawn_arrow"
    me = bpy.data.meshes.get(name)
    if me is not None:
        return me
    verts = [
        (-30, -120, 2), (30, -120, 2), (30, 60, 2), (-30, 60, 2),
        (-80, 60, 2), (80, 60, 2), (0, 170, 2),
    ]
    faces = [(0, 1, 2, 3), (4, 5, 6)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    me.use_fake_user = True
    return me


def _flag_mesh():
    name = "TANKI_flag"
    me = bpy.data.meshes.get(name)
    if me is not None:
        return me
    verts = [
        (-6, -6, 0), (6, -6, 0), (6, 6, 0), (-6, 6, 0),
        (-6, -6, 300), (6, -6, 300), (6, 6, 300), (-6, 6, 300),
        (6, 0, 300), (6, 0, 235), (130, 0, 268),
    ]
    faces = [
        (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
        (8, 9, 10),
    ]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    me.use_fake_user = True
    return me


def _new_object(name, mesh, collection):
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def make_spawn_object(collection, spawn_type, position, rot_z, scale):
    obj = _new_object(f"spawn_{spawn_type}", _spawn_arrow_mesh(), collection)
    obj.location = Vector(position) * scale
    obj.rotation_euler = (0, 0, rot_z)
    obj.scale = (scale, scale, scale)
    color = SPAWN_COLORS.get(spawn_type, COL_YELLOW)
    mat = _viz_material(f"TANKI_spawn_{spawn_type}", color)
    if not obj.data.materials:
        obj.data.materials.append(None)
    obj.material_slots[0].link = 'OBJECT'
    obj.material_slots[0].material = mat
    obj.color = color
    obj.show_name = True
    t = obj.tanki
    t.role = 'SPAWN'
    if spawn_type in SPAWN_TYPES:
        t.spawn_type = spawn_type
    else:
        obj["tanki_raw_spawn_type"] = spawn_type
    return obj


def make_flag_object(collection, team, position, scale):
    obj = _new_object(f"flag_{team}", _flag_mesh(), collection)
    obj.location = Vector(position) * scale
    obj.scale = (scale, scale, scale)
    color = COL_RED if team == "red" else COL_BLUE
    if not obj.data.materials:
        obj.data.materials.append(None)
    obj.material_slots[0].link = 'OBJECT'
    obj.material_slots[0].material = _viz_material(f"TANKI_flag_{team}", color)
    obj.color = color
    obj.show_name = True
    t = obj.tanki
    t.role = 'FLAG'
    t.flag_team = team
    return obj


def make_dom_object(collection, letter, position, scale):
    mesh = _cuboid_mesh("TANKI_dom_box", (-50, -50, 0), (50, 50, 400))
    obj = _new_object(f"dom_point_{letter}", mesh, collection)
    obj.location = Vector(position) * scale
    obj.scale = (scale, scale, scale)
    if not obj.data.materials:
        obj.data.materials.append(None)
    obj.material_slots[0].link = 'OBJECT'
    obj.material_slots[0].material = _viz_material("TANKI_dom", COL_DOM)
    obj.color = COL_DOM
    obj.show_name = True
    t = obj.tanki
    t.role = 'DOM'
    if letter in LETTERS:
        t.point_name = letter
    return obj


class MapCollections:
    def __init__(self, context, map_name, create):
        self.context = context
        self.create = create
        if create:
            self.root = bpy.data.collections.new(map_name)
            context.scene.collection.children.link(self.root)
        else:
            self.root = context.scene.collection
        self._subs = {}

    def get(self, name):
        if not self.create:
            return self.root
        if name not in self._subs:
            col = bpy.data.collections.new(name)
            self.root.children.link(col)
            self._subs[name] = col
        return self._subs[name]


class IMPORT_OT_tanki_map(Operator, ImportHelper):
    bl_idname = "import_scene.tanki_map"
    bl_label = "Import Tanki Map (.xml)"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".xml"
    filter_glob: StringProperty(default="*.xml", options={'HIDDEN'})

    create_collections: BoolProperty(
        name="Create Collections", default=True,
        description="puts stuff into folders")
    scale_factor: FloatProperty(
        name="Scale Factor", default=0.01, precision=4,
        description="how big the map is in blender. 0.01 means one tile is 5m")
    import_functional: BoolProperty(
        name="Import Spawns, Flags and Points", default=True,
        description="spawns, ctf flags and dom points")
    import_collision_preview: BoolProperty(
        name="Import Collisions as Separate Objects", default=False,
        description="brings in the map's collision as separate models. just for looking, export makes its own")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Import")
        box.prop(self, "create_collections")
        box.prop(self, "scale_factor")
        box.prop(self, "import_functional")
        box.prop(self, "import_collision_preview")

    def execute(self, context):
        prefs = get_prefs(context)
        if prefs is None:
            self.report({'ERROR'}, "install this as an addon first, running it from the text editor wont work")
            return {'CANCELLED'}
        if not os.path.isdir(prefs.prop_libs_directory or ""):
            self.report({'ERROR'}, "set your prop libs folder in the addon settings first")
            return {'CANCELLED'}
        wm = context.window_manager
        wm.progress_begin(0, 100)
        start = time.time()
        try:
            result = self.import_map(context, wm)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"import broke: {e}")
            result = {'CANCELLED'}
        wm.progress_end()
        if result == {'FINISHED'}:
            self.report({'INFO'}, f"done in {time.time() - start:.2f}s")
        return result


    def import_map(self, context, wm):
        try:
            tree = ET.parse(self.filepath)
            root = tree.getroot()
        except Exception as e:
            self.report({'ERROR'}, f"cant read that xml: {e}")
            return {'CANCELLED'}

        version = root.get("version") or "1.0"
        _LIB_CACHE.clear()
        mats_before = set(bpy.data.materials)
        map_name = os.path.splitext(os.path.basename(self.filepath))[0]
        cols = MapCollections(context, map_name, self.create_collections)
        scale = self.scale_factor
        context.scene.tanki_scale = scale

        if version in ("1.0", "1.0.Light"):
            placements = self._parse_v1_props(root)
        elif version == "2.0":
            placements = self._parse_v2_props(root)
        elif version == "3.0":
            placements = self._parse_v3_props(root)
        else:
            self.report({'WARNING'}, f"dont know map version '{version}', trying it as 1.0")
            placements = self._parse_v1_props(root)

        wm.progress_update(5)

        by_variant = defaultdict(list)
        for p in placements:
            by_variant[(p["lib"], p["grp"], p["prop"], p["tex"])].append(p)

        total = max(len(by_variant), 1)
        static_col = cols.get("Static")
        missing = set()
        placed = 0
        placed_meshes = set()

        for i, (vkey, plist) in enumerate(by_variant.items()):
            lib, grp, prop, tex = vkey
            wm.progress_update(5 + int(i / total * 75))
            mesh, asset = get_mesh_variant(context, lib, grp, prop, tex)
            if mesh is None:
                missing.add(f"{lib}/{grp}/{prop}")
                continue
            placed_meshes.add(mesh)
            is_sprite = asset["kind"] == "sprite"
            base_name = f"{lib}::{grp}::{prop}".replace(" ", "_")
            for p in plist:
                obj = bpy.data.objects.new(base_name, mesh)
                static_col.objects.link(obj)
                obj.location = Vector(p["pos"]) * scale
                obj.rotation_euler = (0, 0, p["rot"])
                obj.scale = (scale, scale, scale)
                t = obj.tanki
                t.role = 'PROP'
                t.library = lib
                t.group = grp
                t.prop_name = prop
                t.texture = tex or ""
                t.is_sprite = is_sprite
                t.free = p["free"]
                t.no_collision = p["nocol"] and not is_sprite
                placed += 1

        for mesh in placed_meshes:
            for mat in mesh.materials:
                _full_roughness(mat)
        for mat in bpy.data.materials:
            if mat not in mats_before:
                _full_roughness(mat)

        wm.progress_update(85)

        if self.import_functional:
            self._import_functional(root, version, cols, scale)

        if self.import_collision_preview and version in ("1.0", "1.0.Light"):
            self._import_collision_preview(root, cols, scale)

        try:
            bpy.data.orphans_purge(do_recursive=True)
        except Exception:
            pass

        wm.progress_update(100)
        if missing:
            self.report({'WARNING'},
                        f"placed {placed} props, couldnt find {len(missing)} of them "
                        f"(check console): {', '.join(sorted(missing)[:5])}...")
        else:
            self.report({'INFO'}, f"placed {placed} props")
        return {'FINISHED'}


    def _parse_v1_props(self, root):
        placements = []
        sg = root.find("static-geometry")
        if sg is None:
            return placements
        for prop in sg.findall("prop"):
            lib = prop.get("library-name")
            grp = prop.get("group-name")
            name = prop.get("name")
            if not (lib and grp and name):
                continue
            pos = find_position(prop)
            if pos is None:
                continue
            rot = 0.0
            rot_el = prop.find("rotation")
            if rot_el is not None:
                rot = elem_float(rot_el, "z")
            tex_el = prop.find("texture-name")
            tex = (tex_el.text or "").strip() if tex_el is not None and tex_el.text else ""
            placements.append({
                "lib": lib, "grp": grp, "prop": name, "tex": tex,
                "pos": pos, "rot": rot,
                "free": prop.get("free") == "true",
                "nocol": prop.get("noCollision") == "true",
            })
        return placements

    def _read_prop_registry(self, root):
        registry = []
        for prop in root.findall("prop"):
            registry.append({
                "lib": prop.get("library-name"),
                "grp": prop.get("group-name"),
                "prop": prop.get("name"),
                "textures": [t.text or "" for t in prop.findall("texture-name")],
            })
        return registry

    def _registry_placement(self, registry, elem, default_rot=0.0):
        try:
            r = registry[int(elem.get("prop-index", "0"))]
        except (IndexError, ValueError):
            return None
        pos = find_position(elem)
        if pos is None:
            return None
        rot = default_rot
        rz = elem.find("rotation-z")
        if rz is not None and rz.text:
            try:
                rot = float(rz.text)
            except ValueError:
                pass
        tex = ""
        ti = elem.find("texture-index")
        if ti is not None and ti.text:
            try:
                idx = int(ti.text)
                if 0 <= idx < len(r["textures"]):
                    tex = r["textures"][idx]
            except ValueError:
                pass
        return {
            "lib": r["lib"], "grp": r["grp"], "prop": r["prop"], "tex": tex,
            "pos": pos, "rot": rot,
            "free": elem.get("free") == "true", "nocol": False,
        }

    def _parse_v2_props(self, root):
        registry = self._read_prop_registry(root)
        placements = []
        for mo in root.findall("map-object"):
            p = self._registry_placement(registry, mo)
            if p:
                placements.append(p)
        return placements

    def _parse_v3_props(self, root):
        registry = self._read_prop_registry(root)
        placements = []
        for mo in root.findall("mesh"):
            p = self._registry_placement(registry, mo)
            if p:
                placements.append(p)
        for sp in root.findall("sprite"):
            p = self._registry_placement(registry, sp)
            if p:
                placements.append(p)
        return placements


    def _import_functional(self, root, version, cols, scale):
        self._import_spawns(root, cols, scale)
        self._import_flags(root, cols, scale)
        self._import_dom_points(root, cols, scale)

    def _spawn_from_elem(self, sp, col, scale):
        stype = sp.get("type") or "dm"
        pos = find_position(sp)
        if pos is None:
            return None
        rot = 0.0
        rot_el = sp.find("rotation")
        if rot_el is not None:
            rot = elem_float(rot_el, "z")
        else:
            d = sp.find("direction")
            if d is not None and d.text:
                try:
                    rot = float(d.text)
                except ValueError:
                    pass
        obj = make_spawn_object(col, stype, pos, rot, scale)
        obj.tanki.free = sp.get("free") == "true"
        return obj

    def _import_spawns(self, root, cols, scale):
        col = cols.get("Spawn Points")
        containers = root.findall("spawn-points")
        elems = []
        for c in containers:
            elems.extend(c.findall("spawn-point"))
        elems.extend(root.findall("spawn-point"))
        for sp in elems:
            self._spawn_from_elem(sp, col, scale)

    def _import_flags(self, root, cols, scale):
        flags = root.find("ctf-flags")
        if flags is None:
            return
        col = cols.get("Flags")
        for tag, team in (("flag-red", "red"), ("flag-blue", "blue")):
            el = flags.find(tag)
            if el is None:
                continue
            pos = read_position(el)
            if pos is None:
                continue
            make_flag_object(col, team, pos, scale)

    def _import_dom_points(self, root, cols, scale):
        kps = root.find("dom-keypoints")
        if kps is None:
            return
        col = cols.get("Domination")
        for kp in kps.findall("dom-keypoint"):
            pos = find_position(kp)
            if pos is None:
                continue
            letter = (kp.get("name") or "A").upper()
            obj = make_dom_object(col, letter if letter in LETTERS else "A", pos, scale)
            t = obj.tanki
            t.free = kp.get("free") == "true"
            modes = [m.strip().lower() for m in (kp.get("modes") or "").split(",") if m.strip()]
            if modes:
                for m in CONTROL_POINT_MODES:
                    setattr(t, "dom_" + m, m in modes)
            for sp in kp.findall("spawn-point"):
                child = self._spawn_from_elem(sp, col, scale)
                if child is not None:
                    child.parent = obj
                    child.matrix_parent_inverse = obj.matrix_basis.inverted()


    def _import_collision_preview(self, root, cols, scale):
        cg = root.find("collision-geometry")
        if cg is None:
            return
        col = cols.get("Collision Preview")
        mat = _viz_material("TANKI_collision", COL_COLLISION)

        def make(name, verts, faces, elem):
            me = bpy.data.meshes.new(name)
            me.from_pydata(verts, [], faces)
            me.update()
            obj = bpy.data.objects.new(name, me)
            col.objects.link(obj)
            pos = find_position(elem)
            rot = elem.find("rotation")
            rx = elem_float(rot, "x") if rot is not None else 0.0
            ry = elem_float(rot, "y") if rot is not None else 0.0
            rz = elem_float(rot, "z") if rot is not None else 0.0
            obj.rotation_euler = (rx, ry, rz)
            if pos is not None:
                obj.location = pos * scale
            obj.scale = (scale, scale, scale)
            if not me.materials:
                me.materials.append(mat)
            obj.tanki.role = 'PREVIEW'

        for el in cg.findall("collision-plane"):
            w = elem_float(el, "width", 100.0)
            l = elem_float(el, "length", 100.0)
            hw, hl = w / 2.0, l / 2.0
            make("col_plane",
                 [(-hw, -hl, 0), (hw, -hl, 0), (hw, hl, 0), (-hw, hl, 0)],
                 [(0, 1, 2, 3)], el)
        for el in cg.findall("collision-box"):
            size = read_position(el.find("size")) or Vector((100, 100, 100))
            hx, hy, hz = size.x / 2, size.y / 2, size.z / 2
            verts = [(sx * hx, sy * hy, sz * hz)
                     for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
            faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
            make("col_box", verts, faces, el)
        for el in cg.findall("collision-triangle"):
            vs = []
            for i in range(3):
                v = read_position(el.find(f"v{i}")) or Vector()
                vs.append((v.x, v.y, v.z))
            make("col_tri", vs, [(0, 1, 2)], el)


def object_role(obj):
    t = obj.tanki
    if t.role != 'NONE':
        return t.role
    if obj.type == 'MESH' and ("xml_library_name" in obj or "::" in obj.name):
        return 'PROP'
    return 'NONE'


def legacy_prop_identity(obj):
    def clean(name):
        return re.sub(r"\.\d+$", "", name) if name else ""
    if "xml_library_name" in obj:
        return (obj["xml_library_name"], obj.get("xml_group_name", "default"),
                obj.get("xml_prop_name", ""))
    parts = obj.name.split("::")
    if len(parts) >= 3:
        return parts[0], parts[1], clean(parts[2])
    if len(parts) == 2:
        return parts[0], "default", clean(parts[1])
    return None, None, None


def world_aabb(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    mn = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    mx = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    return mn, mx


class EXPORT_OT_tanki_map(Operator, ExportHelper):
    bl_idname = "export_scene.tanki_map"
    bl_label = "Export Tanki Map (.xml)"
    bl_options = {'PRESET'}

    filename_ext = ".xml"
    filter_glob: StringProperty(default="*.xml", options={'HIDDEN'})

    map_format: EnumProperty(
        name="Format",
        items=[
            ('FULL', "1.0 (Full)", "everything"),
            ('LITE', "1.0.Light", "same but no kill zones"),
        ],
        default='FULL')
    export_selected: BoolProperty(name="Selected Only", default=False)
    export_collection: StringProperty(
        name="Collection", default="",
        description="only export this collection, leave empty for everything")
    skip_hidden: BoolProperty(
        name="Skip Hidden Stuff", default=False,
        description="hidden stuff doesnt get exported")
    auto_scale: BoolProperty(
        name="Auto Scale", default=True,
        description="use the same scale the map was imported with")
    scale_factor: FloatProperty(
        name="Scale Factor", default=100.0,
        description="only used when auto scale is off. 100 = 1m becomes 100 tanki units")
    export_collision: BoolProperty(
        name="Export Collisions", default=True,
        description="makes collision from the props in your libs")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Export")
        box.prop(self, "map_format")
        box.prop(self, "export_selected")
        box.prop(self, "export_collection")
        box.prop(self, "skip_hidden")
        box.prop(self, "export_collision")
        box = layout.box()
        box.prop(self, "auto_scale")
        row = box.row()
        row.enabled = not self.auto_scale
        row.prop(self, "scale_factor")

    def execute(self, context):
        start = time.time()
        result = self.export_map(context)
        if result == {'FINISHED'}:
            self.report({'INFO'}, f"saved in {time.time() - start:.2f}s")
        return result

    def _gather_objects(self, context):
        if self.export_selected:
            objects = list(context.selected_objects)
        elif self.export_collection:
            col = bpy.data.collections.get(self.export_collection)
            if col is None:
                return None
            objects = list(col.all_objects)
        else:
            objects = list(context.scene.objects)
        if self.skip_hidden:
            def visible(o):
                try:
                    return o.visible_get()
                except Exception:
                    return True
            objects = [o for o in objects if visible(o)]
        return objects

    def export_map(self, context):
        objects = self._gather_objects(context)
        if objects is None:
            self.report({'ERROR'}, f"no collection called {self.export_collection}")
            return {'CANCELLED'}

        scale = self.scale_factor
        if self.auto_scale:
            stored = context.scene.tanki_scale
            if stored:
                scale = 1.0 / float(stored)

        is_full = self.map_format == 'FULL'
        root = ET.Element("map", {"version": "1.0" if is_full else "1.0.Light"})
        static_el = ET.SubElement(root, "static-geometry")
        collision_el = ET.SubElement(root, "collision-geometry")
        spawns_el = ET.SubElement(root, "spawn-points")
        bonus_el = ET.SubElement(root, "bonus-regions")
        kill_el = ET.SubElement(root, KILL_GEOMETRY) if is_full else None
        flags_el = None
        dom_el = None

        counts = defaultdict(int)
        warnings = []
        missing_collision = set()
        obj_set = set(objects)

        for obj in objects:
            role = object_role(obj)
            if role in ('NONE', 'PREVIEW'):
                continue
            t = obj.tanki

            if role == 'PROP':
                self._export_prop(context, obj, static_el, collision_el, scale,
                                  counts, warnings, missing_collision)

            elif role == 'SPAWN':
                stype = obj.get("tanki_raw_spawn_type") or t.spawn_type
                parent = obj.parent
                if parent is not None and parent in obj_set and object_role(parent) == 'DOM':
                    continue
                if stype == "dom":
                    warnings.append(f"skipped '{obj.name}', dom spawns need to be parented to a dom point")
                    continue
                spawns_el.append(self._spawn_xml(obj, stype, scale))
                counts["spawns"] += 1

            elif role == 'BONUS':
                bonus_el.append(self._bonus_xml(obj, scale))
                counts["bonus regions"] += 1

            elif role == 'KILL':
                if kill_el is None:
                    counts["kill zones skipped (Lite)"] += 1
                    continue
                kill_el.append(self._kill_xml(obj, scale))
                counts["kill zones"] += 1

            elif role == 'FLAG':
                if flags_el is None:
                    flags_el = ET.SubElement(root, "ctf-flags")
                pos = obj.matrix_world.translation * scale
                fel = ET.SubElement(flags_el, f"flag-{t.flag_team}")
                ET.SubElement(fel, "x").text = fnum(pos.x, 3)
                ET.SubElement(fel, "y").text = fnum(pos.y, 3)
                ET.SubElement(fel, "z").text = fnum(pos.z, 3)
                counts["flags"] += 1

            elif role == 'DOM':
                if dom_el is None:
                    dom_el = ET.SubElement(root, "dom-keypoints")
                dom_el.append(self._dom_xml(obj, obj_set, scale))
                counts["domination points"] += 1

        tree = ET.ElementTree(root)
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass
        xml_text = ET.tostring(root, encoding="unicode")
        xml_text = xml_text.replace("<texture-name></texture-name>", "<texture-name/>")
        xml_text = xml_text.replace("<texture-name />", "<texture-name/>")
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(xml_text)
        except OSError as e:
            self.report({'ERROR'}, f"couldnt save the file: {e}")
            return {'CANCELLED'}

        try:
            bpy.data.orphans_purge(do_recursive=True)
        except Exception:
            pass

        summary = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing"
        if missing_collision:
            warnings.append(f"no collision for {len(missing_collision)} props cause they arent in your libs (check console)")
            for m in sorted(missing_collision):
                print(f"[Tanki] no collision, library missing: {m}")
        for w in warnings[:5]:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"exported {summary}")
        return {'FINISHED'}


    def _export_prop(self, context, obj, static_el, collision_el, scale,
                     counts, warnings, missing_collision):
        t = obj.tanki
        if t.role == 'PROP' and t.library:
            lib, grp, prop = t.library, t.group, t.prop_name
            texture = t.texture
            is_sprite = t.is_sprite
            no_collision = t.no_collision
            free = t.free
        else:
            lib, grp, prop = legacy_prop_identity(obj)
            texture = ""
            if obj.data and obj.data.materials and obj.data.materials[0]:
                mat = obj.data.materials[0]
                if "_material" in mat.name:
                    texture = mat.name.split("_material")[0]
            is_sprite = False
            no_collision = False
            free = False
        if not (lib and prop):
            return

        world = obj.matrix_world
        pos = world.translation * scale
        eul = world.to_euler('XYZ')
        rot_z = eul.z
        if abs(eul.x) > 1e-4 or abs(eul.y) > 1e-4:
            warnings.append(f"'{obj.name}' is tilted, tanki maps only do Z rotation so that gets lost")

        attrs = {"library-name": lib, "group-name": grp or "default", "name": prop}
        pel = ET.SubElement(static_el, "prop", attrs)
        rel = ET.SubElement(pel, "rotation")
        ET.SubElement(rel, "z").text = fnum(rot_z, 6)
        tex_el = ET.SubElement(pel, "texture-name")
        if texture:
            tex_el.text = texture
        _xml_vec3_children(pel, "position", pos.x, pos.y, pos.z, 3)
        if free:
            pel.set("free", "true")
        if no_collision and not is_sprite:
            pel.set("noCollision", "true")
        counts["props"] += 1

        if self.export_collision and not is_sprite and not no_collision:
            asset = ensure_asset(context, lib, grp or "default", prop)
            if asset is None:
                missing_collision.add(f"{lib}/{grp}/{prop}")
                return
            prims = get_collision_primitives(asset)
            if prims:
                prop_matrix = Matrix.Rotation(rot_z, 4, 'Z')
                prop_matrix.translation = pos
                for prim in prims:
                    emit_collision_xml(prim, prop_matrix, collision_el)
                counts["collision primitives"] += len(prims)

    def _spawn_xml(self, obj, stype, scale):
        el = ET.Element("spawn-point", {"type": stype})
        rel = ET.SubElement(el, "rotation")
        ET.SubElement(rel, "z").text = fnum(obj.matrix_world.to_euler('XYZ').z, 3)
        pos = obj.matrix_world.translation * scale
        _xml_vec3_children(el, "position", pos.x, pos.y, pos.z, 3)
        if obj.tanki.free:
            el.set("free", "true")
        return el

    def _bonus_xml(self, obj, scale):
        t = obj.tanki
        mn, mx = world_aabb(obj)
        mn *= scale
        mx *= scale
        el = ET.Element("bonus-region", {
            "name": t.bonus_name or FREE_BONUS_NAME,
            "parachute": "true" if t.parachute else "false",
        })
        rel = ET.SubElement(el, "rotation")
        ET.SubElement(rel, "z").text = fnum(0.0, 3)
        _xml_vec3_children(el, "min", mn.x, mn.y, mn.z, 3)
        _xml_vec3_children(el, "max", mx.x, mx.y, mx.z, 3)
        _xml_vec3_children(el, "position", mn.x, mn.y, mn.z, 3)
        if t.free:
            el.set("free", "true")
        for bt in BONUS_TYPES:
            if getattr(t, "bt_" + bt):
                ET.SubElement(el, "bonus-type").text = bt
        for gm in GAME_MODES:
            if getattr(t, "gm_" + gm):
                ET.SubElement(el, "game-mode").text = gm
        return el

    def _kill_xml(self, obj, scale):
        t = obj.tanki
        mn, mx = world_aabb(obj)
        mn *= scale
        mx *= scale
        el = ET.Element(KILL_BOX_NAME, {"type": KILL_BOX_NAME})
        ET.SubElement(el, "minX").text = rawnum(mn.x)
        ET.SubElement(el, "minY").text = rawnum(mn.y)
        ET.SubElement(el, "minZ").text = rawnum(mn.z)
        ET.SubElement(el, "maxX").text = rawnum(mx.x)
        ET.SubElement(el, "maxY").text = rawnum(mx.y)
        ET.SubElement(el, "maxZ").text = rawnum(mx.z)
        ET.SubElement(el, "action").text = t.action
        if t.free:
            el.set("free", "true")
        return el

    def _dom_xml(self, obj, obj_set, scale):
        t = obj.tanki
        modes = [m for m in CONTROL_POINT_MODES if getattr(t, "dom_" + m)] or ["dom"]
        el = ET.Element("dom-keypoint", {
            "free": "true" if t.free else "false",
            "name": t.point_name,
            "modes": ",".join(modes),
        })
        pos = obj.matrix_world.translation * scale
        _xml_vec3_children(el, "position", pos.x, pos.y, pos.z, 3)
        for child in obj.children:
            if child in obj_set and object_role(child) == 'SPAWN':
                stype = child.get("tanki_raw_spawn_type") or child.tanki.spawn_type
                el.append(self._spawn_xml(child, stype, scale))
        return el


class TankiMapPreferences(AddonPreferences):
    bl_idname = __name__

    prop_libs_directory: StringProperty(
        name="Prop Libs Folder",
        subtype='DIR_PATH',
        description="the folder with all your prop libs in it")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "prop_libs_directory")


classes = (
    TankiObjectSettings,
    TankiMapPreferences,
    IMPORT_OT_tanki_map,
    EXPORT_OT_tanki_map,
)


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_tanki_map.bl_idname, text="Tanki Map (.xml)")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_tanki_map.bl_idname, text="Tanki Map (.xml)")


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.tanki = PointerProperty(type=TankiObjectSettings)
    bpy.types.Scene.tanki_scale = FloatProperty(
        name="Map Scale", default=0.01, min=0.0001, max=1.0, precision=4,
        description="map scale, gets set when you import")
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.tanki_scale
    del bpy.types.Object.tanki
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    clear_caches()


if __name__ == "__main__":
    register()
