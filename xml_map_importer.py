"""
XML Map Importer for Blender 4.4
Imports XML map files with prop libraries.
"""

bl_info = {
    "name": "Tanki XML Map Importer",
    "author": "MapMakers Conglomerate",
    "version": (1, 0),
    "blender": (4, 4, 0),
    "location": "File > Import > XML Map (.xml)",
    "description": "Import tanki XML maps ",
    "warning": "You need to have the 3DS addonn enabled to use this.",
    "doc_url": "",
    "category": "Import-Export",
}

import bpy
import os
import xml.etree.ElementTree as ET
import math
import tempfile
import mathutils
import time
import threading
from collections import defaultdict
from bpy.props import StringProperty, CollectionProperty, BoolProperty, EnumProperty, IntProperty
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator, Panel, PropertyGroup, AddonPreferences

# preferences
class XMLMapImporterPreferences(AddonPreferences):
    bl_idname = __name__

    prop_libs_directory: StringProperty(
        name="Prop Libraries Directory",
        subtype='DIR_PATH',
        description="Directory containing prop libraries"
    )
    
    threads: IntProperty(
        name="Import Threads",
        description="Number of threads to use for parallel importing (0 = auto)",
        default=0,
        min=0,
        max=32
    )
    
    batch_size: IntProperty(
        name="Batch Size",
        description="Number of props to process in each batch",
        default=50,
        min=10,
        max=1000
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="XML Map Importer Settings")
        layout.prop(self, "prop_libs_directory")
        layout.prop(self, "threads")
        layout.prop(self, "batch_size")


class PropLibraryItem(PropertyGroup):
    name: StringProperty(name="Name")
    path: StringProperty(name="Path")
    loaded: BoolProperty(name="Loaded", default=False)

# Main importer
class IMPORT_OT_xml_map(Operator, ImportHelper):
    """Import an XML map file with associated prop libraries"""
    bl_idname = "import_scene.xml_map"
    bl_label = "Import XML Map"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".xml"
    
    filter_glob: StringProperty(
        default="*.xml",
        options={'HIDDEN'},
    )

    import_textures: BoolProperty(
        name="Import Textures",
        description="Import and assign textures to objects",
        default=True,
    )
    
    create_collections: BoolProperty(
        name="Create Collection",
        description="Create a collection for the map",
        default=True,
    )
    
    scale_factor: bpy.props.FloatProperty(
        name="Scale Factor",
        description="Scale factor for imported objects",
        default=0.01,
    )
    
    axis_forward: EnumProperty(
        name="Forward Axis",
        items=(
            ('X', "X", ""),
            ('Y', "Y", ""),
            ('Z', "Z", ""),
            ('-X', "-X", ""),
            ('-Y', "-Y", ""),
            ('-Z', "-Z", ""),
        ),
        default='Y',
    )
    
    axis_up: EnumProperty(
        name="Up Axis",
        items=(
            ('X', "X", ""),
            ('Y', "Y", ""),
            ('Z', "Z", ""),
            ('-X', "-X", ""),
            ('-Y', "-Y", ""),
            ('-Z', "-Z", ""),
        ),
        default='Z',
    )
    
    rotation_mode: EnumProperty(
        name="Rotation",
        items=(
            ('DEGREES', "Degrees", "Rotations in XML are in degrees"),
            ('RADIANS', "Radians", "Rotations in XML are in radians"),
        ),
        default='RADIANS',
    )
    
    use_caching: BoolProperty(
        name="Cache Props",
        description="Cache prop meshes to speed up loading of repeated props",
        default=True,
    )

    def execute(self, context):

        prefs = context.preferences.addons[__name__].preferences
        prop_libs_dir = prefs.prop_libs_directory
        

        if not os.path.isdir(prop_libs_dir):
            self.report({'ERROR'}, f"Prop libraries directory not found: {prop_libs_dir}")
            return {'CANCELLED'}
        

        start_time = time.time()
        result = self.import_xml_map(context, prop_libs_dir)
        end_time = time.time()
        
        if result == {'FINISHED'}:
            self.report({'INFO'}, f"Map imported in {end_time - start_time:.2f} seconds")
        
        return result
    
    def import_xml_map(self, context, prop_libs_dir):
        # parse the XML
        try:
            tree = ET.parse(self.filepath)
            root = tree.getroot()
        except Exception as e:
            self.report({'ERROR'}, f"Error parsing XML file: {e}")
            return {'CANCELLED'}
        

        if root.tag != 'map':
            self.report({'ERROR'}, "Not a valid map file")
            return {'CANCELLED'}
        

        map_name = os.path.splitext(os.path.basename(self.filepath))[0]
        map_collection = None
        
        if self.create_collections:
            map_collection = bpy.data.collections.new(map_name)
            bpy.context.scene.collection.children.link(map_collection)
        
        # Load the prop libs - this could be slow
        self.report({'INFO'}, "Loading prop libraries...")
        prop_libraries = self.load_prop_libraries(prop_libs_dir)
        

        if self.use_caching:
            self._mesh_cache = {}
            self._material_cache = {}
        

        static_geometry = root.find('static-geometry')
        if static_geometry is not None:
            self.import_static_geometry(context, static_geometry, prop_libraries, map_collection)
        

        if self.use_caching:
            self._mesh_cache = {}
            self._material_cache = {}
        
        self.report({'INFO'}, f"XML Map imported successfully: {map_name}")
        return {'FINISHED'}
    
    def load_prop_libraries(self, prop_libs_dir):
        """Load prop libraries from the directory"""
        prop_libraries = {}
        

        lib_dirs = [os.path.join(prop_libs_dir, item) for item in os.listdir(prop_libs_dir) 
                   if os.path.isdir(os.path.join(prop_libs_dir, item))]
        
        # Load each library
        for lib_dir in lib_dirs:
            lib_xml_path = os.path.join(lib_dir, "library.xml")
            
            if os.path.exists(lib_xml_path):
                try:

                    lib_tree = ET.parse(lib_xml_path)
                    lib_root = lib_tree.getroot()
                    

                    lib_name = lib_root.get('name')
                    if lib_name:

                        prop_libraries[lib_name] = {
                            'path': lib_dir,
                            'xml': lib_root,
                            'props': {}
                        }
                        

                        props_dict = {}
                        for prop_group in lib_root.findall('.//prop-group'):
                            group_name = prop_group.get('name')
                            
                            for prop in prop_group.findall('.//prop'):
                                prop_name = prop.get('name')
                                prop_key = f"{group_name}/{prop_name}" 
                                props_dict[prop_key] = prop
                        
                        prop_libraries[lib_name]['props'] = props_dict
                
                except Exception as e:
                    print(f"Error loading prop library {lib_dir}: {e}")
        
        return prop_libraries
    
    def import_static_geometry(self, context, static_geometry, prop_libraries, parent_collection):
        """Import all the props from static geometry"""

        target_collection = parent_collection or context.scene.collection
        

        props_to_import = []
        for prop_elem in static_geometry.findall('.//prop'):
            library_name = prop_elem.get('library-name')
            group_name = prop_elem.get('group-name')
            prop_name = prop_elem.get('name')
            

            position_elem = prop_elem.find('.//position')
            if position_elem is None:
                continue
                
            x = float(position_elem.find('x').text)
            y = float(position_elem.find('y').text)
            z = float(position_elem.find('z').text)
            

            rotation_elem = prop_elem.find('.//rotation')
            rot_z = 0.0
            if rotation_elem is not None and rotation_elem.find('z') is not None:
                rot_z = float(rotation_elem.find('z').text)
                

                if self.rotation_mode == 'DEGREES':
                    rot_z = math.radians(rot_z)
            

            texture_name_elem = prop_elem.find('.//texture-name')
            texture_name = ""
            if texture_name_elem is not None and texture_name_elem.text:
                texture_name = texture_name_elem.text
            

            props_to_import.append({
                'library_name': library_name,
                'group_name': group_name,
                'prop_name': prop_name,
                'position': (x, y, z),
                'rotation': rot_z,
                'texture_name': texture_name
            })
        

        prefs = context.preferences.addons[__name__].preferences
        batch_size = prefs.batch_size
        

        total_props = len(props_to_import)
        self.report({'INFO'}, f"Importing {total_props} props...")
        

        props_by_type = defaultdict(list)
        for prop_info in props_to_import:
            prop_key = f"{prop_info['library_name']}/{prop_info['group_name']}/{prop_info['prop_name']}"
            props_by_type[prop_key].append(prop_info)
        

        imported_count = 0
        for prop_type, props in props_by_type.items():

            if props:
                prop_info = props[0]
                self.import_prop(context, prop_info['library_name'], prop_info['group_name'], 
                                prop_info['prop_name'], prop_info['position'], prop_info['rotation'], 
                                prop_info['texture_name'], prop_libraries, target_collection)
                imported_count += 1
                

                for prop_info in props[1:]:
                    self.import_prop(context, prop_info['library_name'], prop_info['group_name'], 
                                    prop_info['prop_name'], prop_info['position'], prop_info['rotation'], 
                                    prop_info['texture_name'], prop_libraries, target_collection)
                    imported_count += 1

                    if imported_count % 50 == 0:
                        self.report({'INFO'}, f"Imported {imported_count}/{total_props} props...")
        
        self.report({'INFO'}, f"Finished importing {imported_count} props")
    
    def import_prop(self, context, library_name, group_name, prop_name, 
                   position, rotation, texture_name, prop_libraries, parent_collection):
        """Import a single prop"""

        if library_name not in prop_libraries:
            return None
        
        library = prop_libraries[library_name]
        prop_key = f"{group_name}/{prop_name}"
        

        if prop_key not in library['props']:
            return None
        
        prop_def = library['props'][prop_key]
        

        mesh_elem = prop_def.find('.//mesh')
        if mesh_elem is None:
            return None
        
        mesh_file = mesh_elem.get('file')
        mesh_path = os.path.join(library['path'], mesh_file)
        

        if not os.path.exists(mesh_path):
            return None
        

        cache_key = f"{library_name}_{group_name}_{prop_name}"
        
        mesh_data = None
        material = None
        
        if self.use_caching and cache_key in self._mesh_cache:
            # get the cached mesh data, but make a copy when you need a different material
            original_mesh_data = self._mesh_cache[cache_key]
            mesh_data = original_mesh_data.copy()
            
            if texture_name and texture_name in self._material_cache:
                material = self._material_cache[texture_name]
        else:
            mesh_data = self.import_mesh_data(context, mesh_path, library_name, prop_name)
            
            if self.use_caching and mesh_data:
                self._mesh_cache[cache_key] = mesh_data
        

        if mesh_data:
            object_name = f"{library_name}_{prop_name}"
            prop_obj = bpy.data.objects.new(object_name, mesh_data)
            

            parent_collection.objects.link(prop_obj)
            

            scaled_position = [p * self.scale_factor for p in position]
            

            if self.axis_up == 'Z':

                final_position = (scaled_position[0], scaled_position[1], scaled_position[2])
            else:

                final_position = (scaled_position[0], scaled_position[2], scaled_position[1])
            

            prop_obj.location = final_position
            

            rot_matrix = mathutils.Matrix.Rotation(rotation, 4, 'Z')
            

            if self.axis_up == 'Z':

                prop_obj.rotation_euler = rot_matrix.to_euler('XYZ')
            else:

                conversion_matrix = mathutils.Matrix.Rotation(math.pi/2.0, 4, 'X')
                final_rotation = conversion_matrix @ rot_matrix @ conversion_matrix.inverted()
                prop_obj.rotation_euler = final_rotation.to_euler()
            

            prop_obj.scale = (self.scale_factor, self.scale_factor, self.scale_factor)
            

            if self.import_textures and texture_name:
                if material:

                    if prop_obj.data.materials:
                        prop_obj.data.materials[0] = material
                    else:
                        prop_obj.data.materials.append(material)
                else:

                    material = self.create_material(texture_name, mesh_elem, library)
                    if material:
                        if prop_obj.data.materials:
                            prop_obj.data.materials[0] = material
                        else:
                            prop_obj.data.materials.append(material)
                        

                        if self.use_caching:
                            self._material_cache[texture_name] = material
            
            return prop_obj
        
        return None
    
    def import_mesh_data(self, context, mesh_path, library_name, prop_name):
        """Import mesh from 3DS file"""

        pre_import_objects = set(bpy.data.objects)
        

        temp_collection = bpy.data.collections.new("__temp_import")
        bpy.context.scene.collection.children.link(temp_collection)
        

        try:

            original_active_collection = context.view_layer.active_layer_collection
            temp_layer_collection = context.view_layer.layer_collection.children[temp_collection.name]
            context.view_layer.active_layer_collection = temp_layer_collection
            

            bpy.ops.import_scene.max3ds(filepath=mesh_path)

            context.view_layer.active_layer_collection = original_active_collection
            

            imported_objects = [obj for obj in bpy.data.objects if obj not in pre_import_objects]
            

            mesh_objects = [obj for obj in imported_objects if obj.type == 'MESH']
            

            if not mesh_objects:
                bpy.data.collections.remove(temp_collection)
                return None
            

            meshes_with_materials = [obj for obj in mesh_objects if 
                                   obj.data.materials and 
                                   len(obj.data.materials) > 0 and 
                                   any(mat is not None for mat in obj.data.materials)]
            

            best_mesh = None
            if meshes_with_materials:

                meshes_with_materials.sort(key=lambda obj: len(obj.data.vertices), reverse=True)
                best_mesh = meshes_with_materials[0].data
                best_mesh = best_mesh.copy()
            

            for obj in imported_objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            

            bpy.data.collections.remove(temp_collection)
            
            return best_mesh
            
        except Exception as e:

            if temp_collection and temp_collection.name in bpy.data.collections:
                bpy.data.collections.remove(temp_collection)
                
            print(f"Error importing mesh {mesh_path}: {e}")
            return None
    
    def create_material(self, texture_name, mesh_elem, library):
        """Create a new material with texture"""

        texture_elem = mesh_elem.find(f'.//texture[@name="{texture_name}"]')
        if texture_elem is None:
            return None
        
        diffuse_map = texture_elem.get('diffuse-map')
        if not diffuse_map:
            return None
        
        texture_path = os.path.join(library['path'], diffuse_map)
        

        if not os.path.exists(texture_path):
            return None
        

        img = None
        if texture_path in bpy.data.images:
            img = bpy.data.images[texture_path]
        else:
            img = bpy.data.images.load(texture_path)
        

        mat_name = f"{texture_name}_material"
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        bsdf = nodes.get('Principled BSDF')
        if bsdf is None:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        

        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = img
        

        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        
        return mat

    # ui for import options
    def draw(self, context):
        layout = self.layout
        

        box = layout.box()
        box.label(text="Import Options")
        box.prop(self, "import_textures")
        box.prop(self, "create_collections")
        box.prop(self, "scale_factor")
        box.prop(self, "use_caching")
        

        box = layout.box()
        box.label(text="Coordinate System")
        row = box.row()
        row.prop(self, "axis_forward")
        row.prop(self, "axis_up")
        

        box = layout.box()
        box.label(text="Rotation Settings")
        box.prop(self, "rotation_mode")


class VIEW3D_PT_xml_map_libraries(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XML Map'
    bl_label = "XML Map Libraries"
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        

        prefs = context.preferences.addons[__name__].preferences
        

        row = layout.row()
        row.label(text="Prop Libraries Directory:")
        row = layout.row()
        row.prop(prefs, "prop_libs_directory", text="")
        

        row = layout.row()
        row.prop(prefs, "threads")
        row = layout.row()
        row.prop(prefs, "batch_size")
        

        row = layout.row()
        row.operator("xml_map.refresh_libraries", text="Refresh Libraries")


class XML_MAP_OT_refresh_libraries(Operator):
    """Refresh the list of prop libraries"""
    bl_idname = "xml_map.refresh_libraries"
    bl_label = "Refresh Prop Libraries"
    
    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        prop_libs_dir = prefs.prop_libs_directory
        
        if not os.path.isdir(prop_libs_dir):
            self.report({'ERROR'}, f"Prop libraries directory not found: {prop_libs_dir}")
            return {'CANCELLED'}
        

        
        self.report({'INFO'}, f"Prop libraries refreshed from: {prop_libs_dir}")
        return {'FINISHED'}

# registration
classes = (
    XMLMapImporterPreferences,
    PropLibraryItem,
    IMPORT_OT_xml_map,
    VIEW3D_PT_xml_map_libraries,
    XML_MAP_OT_refresh_libraries,
)

def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_xml_map.bl_idname, text="XML Map (.xml)")

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register() 
