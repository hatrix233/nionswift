# standard libraries
import collections
import copy
import datetime
import functools
import gettext
import json
import logging
import numbers
import os.path
import shutil
import threading
import time
import typing
import uuid
import weakref

# third party libraries
import scipy

# local libraries
from nion.data import Image
from nion.swift.model import Cache
from nion.swift.model import Connection
from nion.swift.model import DataGroup
from nion.swift.model import DataItem
from nion.swift.model import Graphics
from nion.swift.model import HardwareSource
from nion.swift.model import ImportExportManager
from nion.swift.model import PlugInManager
from nion.swift.model import Symbolic
from nion.swift.model import Utility
from nion.swift.model import WorkspaceLayout
from nion.utils import Converter
from nion.utils import Event
from nion.utils import Observable
from nion.utils import Persistence
from nion.utils import ReferenceCounting
from nion.utils import ThreadPool

_ = gettext.gettext


class FilePersistentStorage:

    def __init__(self, filepath=None, create=True):
        self.__filepath = filepath
        self.__properties = self.__read_properties()
        self.__properties_lock = threading.RLock()

    def get_version(self):
        return 0

    def __read_properties(self):
        properties = dict()
        if self.__filepath and os.path.exists(self.__filepath):
            with open(self.__filepath, "r") as fp:
                properties = json.load(fp)
        # migrations go here
        return properties

    def __get_properties(self):
        with self.__properties_lock:
            return copy.deepcopy(self.__properties)
    properties = property(__get_properties)

    def __get_storage_dict(self, object):
        persistent_object_parent = object.persistent_object_parent
        if not persistent_object_parent:
            return self.__properties
        else:
            parent_storage_dict = self.__get_storage_dict(persistent_object_parent.parent)
            return object.get_accessor_in_parent()(parent_storage_dict)

    def __update_modified_and_get_storage_dict(self, object):
        storage_dict = self.__get_storage_dict(object)
        with self.__properties_lock:
            storage_dict["modified"] = object.modified.isoformat()
        persistent_object_parent = object.persistent_object_parent
        parent = persistent_object_parent.parent if persistent_object_parent else None
        if parent:
            self.__update_modified_and_get_storage_dict(parent)
        return storage_dict

    def update_properties(self):
        if self.__filepath:
            with open(self.__filepath, "w") as fp:
                json.dump(self.__properties, fp)

    def insert_item(self, parent, name, before_index, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            item_list = storage_dict.setdefault(name, list())
            item_dict = item.write_to_dict()
            item_list.insert(before_index, item_dict)
            item.persistent_object_context = parent.persistent_object_context
        self.update_properties()

    def remove_item(self, parent, name, index, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            item_list = storage_dict[name]
            del item_list[index]
        self.update_properties()
        item.persistent_object_context = None

    def set_item(self, parent, name, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            if item:
                item_dict = item.write_to_dict()
                storage_dict[name] = item_dict
                item.persistent_object_context = parent.persistent_object_context
            else:
                if name in storage_dict:
                    del storage_dict[name]
        self.update_properties()

    def set_property(self, object, name, value):
        storage_dict = self.__update_modified_and_get_storage_dict(object)
        with self.__properties_lock:
            storage_dict[name] = value
        self.update_properties()


class DataItemPersistentStorage:

    """
        Manages persistent storage for data items by caching properties and data, maintaining the PersistentObjectContext
        on contained items, and writing to disk when necessary.

        The persistent_storage_handler must respond to these methods:
            read_properties()
            read_data()
            write_properties(properties, file_datetime)
            write_data(data, file_datetime)
    """

    def __init__(self, persistent_storage_handler=None, data_item=None, properties=None):
        self.__persistent_storage_handler = persistent_storage_handler
        self.__properties = Utility.clean_dict(copy.deepcopy(properties) if properties else dict())
        self.__properties_lock = threading.RLock()
        self.__weak_data_item = weakref.ref(data_item) if data_item else None
        self.write_delayed = False

    @property
    def data_item(self):
        return self.__weak_data_item() if self.__weak_data_item else None

    @data_item.setter
    def data_item(self, data_item):
        self.__weak_data_item = weakref.ref(data_item) if data_item else None

    @property
    def properties(self):
        with self.__properties_lock:
            return copy.deepcopy(self.__properties)

    @property
    def _persistent_storage_handler(self):
        return self.__persistent_storage_handler

    def __get_storage_dict(self, object):
        persistent_object_parent = object.persistent_object_parent
        if not persistent_object_parent:
            return self.__properties
        else:
            parent_storage_dict = self.__get_storage_dict(persistent_object_parent.parent)
            return object.get_accessor_in_parent()(parent_storage_dict)

    def __update_modified_and_get_storage_dict(self, object):
        storage_dict = self.__get_storage_dict(object)
        with self.__properties_lock:
            storage_dict["modified"] = object.modified.isoformat()
        persistent_object_parent = object.persistent_object_parent
        parent = persistent_object_parent.parent if persistent_object_parent else None
        if parent:
            self.__update_modified_and_get_storage_dict(parent)
        return storage_dict

    def update_properties(self):
        if not self.write_delayed:
            file_datetime = self.data_item.created_local
            self.__persistent_storage_handler.write_properties(self.properties, file_datetime)

    def insert_item(self, parent, name, before_index, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            item_list = storage_dict.setdefault(name, list())
            item_dict = item.write_to_dict()
            item_list.insert(before_index, item_dict)
            item.persistent_object_context = parent.persistent_object_context
        self.update_properties()

    def remove_item(self, parent, name, index, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            item_list = storage_dict[name]
            del item_list[index]
        self.update_properties()
        item.persistent_object_context = None

    def set_item(self, parent, name, item):
        storage_dict = self.__update_modified_and_get_storage_dict(parent)
        with self.__properties_lock:
            if item:
                item_dict = item.write_to_dict()
                storage_dict[name] = item_dict
                item.persistent_object_context = parent.persistent_object_context
            else:
                if name in storage_dict:
                    del storage_dict[name]
        self.update_properties()

    def update_data(self, data):
        if not self.write_delayed:
            file_datetime = self.data_item.created_local
            if data is not None:
                self.__persistent_storage_handler.write_data(data, file_datetime)

    def load_data(self):
        assert self.data_item.maybe_data_source and self.data_item.maybe_data_source.has_data
        return self.__persistent_storage_handler.read_data()

    def set_property(self, object, name, value):
        storage_dict = self.__update_modified_and_get_storage_dict(object)
        with self.__properties_lock:
            storage_dict[name] = value
        self.update_properties()

    def remove(self):
        self.__persistent_storage_handler.remove()


class MemoryPersistentStorageSystem:

    def __init__(self):
        self.data = dict()
        self.properties = dict()
        self._test_data_read_event = Event.Event()

    class MemoryStorageHandler:

        def __init__(self, uuid, properties, data, data_read_event):
            self.__uuid = uuid
            self.__properties = properties
            self.__data = data
            self.__data_read_event = data_read_event

        @property
        def reference(self):
            return str(self.__uuid)

        def read_properties(self):
            return self.__properties.get(self.__uuid, dict())

        def read_data(self):
            self.__data_read_event.fire(self.__uuid)
            return self.__data.get(self.__uuid)

        def write_properties(self, properties, file_datetime):
            self.__properties[self.__uuid] = copy.deepcopy(properties)

        def write_data(self, data, file_datetime):
            self.__data[self.__uuid] = data.copy()

        def remove(self):
            self.__data.pop(self.__uuid, None)
            self.__properties.pop(self.__uuid, None)

    def find_data_items(self):
        persistent_storage_handlers = list()
        for key in sorted(self.properties):
            self.properties[key].setdefault("uuid", str(uuid.uuid4()))
            persistent_storage_handlers.append(MemoryPersistentStorageSystem.MemoryStorageHandler(key, self.properties, self.data, self._test_data_read_event))
        return persistent_storage_handlers

    def make_persistent_storage_handler(self, data_item):
        uuid = str(data_item.uuid)
        return MemoryPersistentStorageSystem.MemoryStorageHandler(uuid, self.properties, self.data, self._test_data_read_event)


from nion.swift.model import NDataHandler

class FilePersistentStorageSystem:

    _file_handlers = [NDataHandler.NDataHandler]

    def __init__(self, directories):
        self.__directories = directories
        self.__file_handlers = FilePersistentStorageSystem._file_handlers

    def find_data_items(self):
        persistent_storage_handlers = list()
        absolute_file_paths = set()
        for directory in self.__directories:
            for root, dirs, files in os.walk(directory):
                absolute_file_paths.update([os.path.join(root, data_file) for data_file in files])
        for file_handler in self.__file_handlers:
            for data_file in filter(file_handler.is_matching, absolute_file_paths):
                try:
                    persistent_storage_handler = file_handler(data_file)
                    assert persistent_storage_handler.is_valid
                    persistent_storage_handlers.append(persistent_storage_handler)
                except Exception as e:
                    logging.error("Exception reading file: %s", data_file)
                    logging.error(str(e))
                    raise
        return persistent_storage_handlers

    def __get_default_path(self, data_item):
        uuid_ = data_item.uuid
        created_local = data_item.created_local
        session_id = data_item.session_id
        # uuid_.bytes.encode('base64').rstrip('=\n').replace('/', '_')
        # and back: uuid_ = uuid.UUID(bytes=(slug + '==').replace('_', '/').decode('base64'))
        # also:
        def encode(uuid_, alphabet):
            result = str()
            uuid_int = uuid_.int
            while uuid_int:
                uuid_int, digit = divmod(uuid_int, len(alphabet))
                result += alphabet[digit]
            return result
        encoded_uuid_str = encode(uuid_, "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")  # 25 character results
        path_components = created_local.strftime("%Y-%m-%d").split('-')
        session_id = session_id if session_id else created_local.strftime("%Y%m%d-000000")
        path_components.append(session_id)
        path_components.append("data_" + encoded_uuid_str)
        return os.path.join(*path_components)

    def make_persistent_storage_handler(self, data_item):
        return self.__file_handlers[0].make(os.path.join(self.__directories[0], self.__get_default_path(data_item)))


class PersistentDataItemContext(Persistence.PersistentObjectContext):

    """
        A PersistentObjectContext that adds extra methods for handling data items.

        Versioning

        If the file is too old, it must be migrated to the newer version.
        If the file is too new, it cannot be loaded.

        When writing, the version the file format is written to the 'version' property.

    """

    def __init__(self, persistent_storage_systems=None, ignore_older_files: bool=False, log_migrations: bool=True, log_copying: bool=False):
        super().__init__()
        self.__persistent_storage_systems = persistent_storage_systems if persistent_storage_systems else [MemoryPersistentStorageSystem()]
        self.__ignore_older_files = ignore_older_files
        self.__log_migrations = log_migrations
        self.__log_copying = log_copying

    @property
    def persistent_storage_systems(self):
        return self.__persistent_storage_systems

    def read_data_items_version_stats(self):
        persistent_storage_handlers = list()  # persistent_storage_handler
        for persistent_storage_system in self.__persistent_storage_systems:
            persistent_storage_handlers.extend(persistent_storage_system.find_data_items())
        count = [0, 0, 0]  # data item matches version, data item has higher version, data item has lower version
        writer_version = DataItem.DataItem.writer_version
        for persistent_storage_handler in persistent_storage_handlers:
            properties = persistent_storage_handler.read_properties()
            version = properties.get("version", 0)
            if version < writer_version:
                count[2] += 1
            elif version > writer_version:
                count[1] += 1
            else:
                count[0] += 1
        return count

    def read_data_items(self, target_document=None):
        """
        Read data items from the data reference handler and return as a list.

        Data items will have persistent_object_context set upon return, but caller will need to call finish_reading
        on each of the data items.

        Pass target_document to copy data items into new document. Useful for auto migration.
        """
        persistent_storage_handlers = list()  # persistent_storage_handler
        for persistent_storage_system in self.__persistent_storage_systems:
            persistent_storage_handlers.extend(persistent_storage_system.find_data_items())
        data_items_by_uuid = dict()
        ReaderInfo = collections.namedtuple("ReaderInfo", ["properties", "changed_ref", "persistent_storage_handler"])
        reader_info_list = list()
        for persistent_storage_handler in persistent_storage_handlers:
            try:
                reader_info_list.append(ReaderInfo(persistent_storage_handler.read_properties(), [False], persistent_storage_handler))
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()
        if not self.__ignore_older_files:
            self.__migrate_to_latest(reader_info_list)
        for reader_info in reader_info_list:
            properties = reader_info.properties
            changed_ref = reader_info.changed_ref
            persistent_storage_handler = reader_info.persistent_storage_handler
            try:
                version = properties.get("version", 0)
                if version == DataItem.DataItem.writer_version:
                    data_item_uuid = uuid.UUID(properties["uuid"])
                    if target_document is not None:
                        if not target_document.get_data_item_by_uuid(data_item_uuid):
                            new_data_item = self.__auto_migrate_data_item(data_item_uuid, persistent_storage_handler, properties, target_document)
                            if new_data_item:
                                data_items_by_uuid[data_item_uuid] = new_data_item
                    else:
                        if changed_ref[0]:
                            persistent_storage_handler.write_properties(copy.deepcopy(properties), datetime.datetime.now())
                        # NOTE: Search for to-do 'file format' to gather together 'would be nice' changes
                        # NOTE: change writer_version in DataItem.py
                        data_item = DataItem.DataItem(item_uuid=data_item_uuid)
                        data_item.begin_reading()
                        persistent_storage = DataItemPersistentStorage(persistent_storage_handler=persistent_storage_handler, data_item=data_item, properties=properties)
                        data_item.read_from_dict(persistent_storage.properties)
                        self._set_persistent_storage_for_object(data_item, persistent_storage)
                        data_item.persistent_object_context = self
                        if self.__log_migrations and data_item.uuid in data_items_by_uuid:
                            logging.info("Warning: Duplicate data item %s", data_item.uuid)
                        data_items_by_uuid[data_item.uuid] = data_item
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()
        def sort_by_date_key(data_item):
            return data_item.created
        data_items = list(data_items_by_uuid.values())
        data_items.sort(key=sort_by_date_key)
        return data_items

    def __auto_migrate_data_item(self, data_item_uuid, persistent_storage_handler, properties, target_document):
        new_data_item = None
        target_persistent_storage_handler = None
        for persistent_storage_system in target_document.persistent_object_context.persistent_storage_systems:
            # create a temporary data item that can be used to get the new file reference
            old_data_item = DataItem.DataItem(item_uuid=data_item_uuid)
            old_data_item.begin_reading()
            old_data_item.read_from_dict(properties)
            old_data_item.finish_reading()
            target_persistent_storage_handler = persistent_storage_system.make_persistent_storage_handler(old_data_item)
            if target_persistent_storage_handler:
                break
        if target_persistent_storage_handler:
            os.makedirs(os.path.dirname(target_persistent_storage_handler.reference), exist_ok=True)
            shutil.copyfile(persistent_storage_handler.reference, target_persistent_storage_handler.reference)
            target_persistent_storage_handler.write_properties(copy.deepcopy(properties), datetime.datetime.now())
            new_data_item = DataItem.DataItem(item_uuid=data_item_uuid)
            new_data_item.begin_reading()
            persistent_storage = DataItemPersistentStorage(persistent_storage_handler=target_persistent_storage_handler, data_item=new_data_item, properties=properties)
            new_data_item.read_from_dict(persistent_storage.properties)
            target_document.persistent_object_context._set_persistent_storage_for_object(new_data_item, persistent_storage)
            new_data_item.persistent_object_context = target_document.persistent_object_context
            if self.__log_copying:
                logging.info("Copying data item %s to library.", data_item_uuid)
        elif self.__log_copying:
            logging.info("Unable to copy data item %s to library.", data_item_uuid)
        return new_data_item

    def __migrate_to_latest(self, reader_info_list):
        self.__migrate_to_v2(reader_info_list)
        self.__migrate_to_v3(reader_info_list)
        self.__migrate_to_v4(reader_info_list)
        self.__migrate_to_v5(reader_info_list)
        self.__migrate_to_v6(reader_info_list)
        self.__migrate_to_v7(reader_info_list)
        self.__migrate_to_v8(reader_info_list)
        self.__migrate_to_v9(reader_info_list)
        self.__migrate_to_v10(reader_info_list)

    def __migrate_to_v10(self, reader_info_list):
        translate_region_type = {"point-region": "point-graphic", "line-region": "line-profile-graphic", "rectangle-region": "rect-graphic", "ellipse-region": "ellipse-graphic",
            "interval-region": "interval-graphic"}
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 9:
                    reader_info.changed_ref[0] = True
                    # import pprint
                    # pprint.pprint(properties)
                    for data_source in properties.get("data_sources", list()):
                        displays = data_source.get("displays", list())
                        if len(displays) > 0:
                            display = displays[0]
                            for region in data_source.get("regions", list()):
                                graphic = dict()
                                graphic["type"] = translate_region_type[region["type"]]
                                graphic["uuid"] = region["uuid"]
                                region_id = region.get("region_id")
                                if region_id is not None:
                                    graphic["graphic_id"] = region_id
                                label = region.get("label")
                                if label is not None:
                                    graphic["label"] = label
                                is_position_locked = region.get("is_position_locked")
                                if is_position_locked is not None:
                                    graphic["is_position_locked"] = is_position_locked
                                is_shape_locked = region.get("is_shape_locked")
                                if is_shape_locked is not None:
                                    graphic["is_shape_locked"] = is_shape_locked
                                is_bounds_constrained = region.get("is_bounds_constrained")
                                if is_bounds_constrained is not None:
                                    graphic["is_bounds_constrained"] = is_bounds_constrained
                                center = region.get("center")
                                size = region.get("size")
                                if center is not None and size is not None:
                                    graphic["bounds"] = (center[0] - size[0] * 0.5, center[1] - size[1] * 0.5), (size[0], size[1])
                                start = region.get("start")
                                if start is not None:
                                    graphic["start"] = start
                                end = region.get("end")
                                if end is not None:
                                    graphic["end"] = end
                                width = region.get("width")
                                if width is not None:
                                    graphic["width"] = width
                                position = region.get("position")
                                if position is not None:
                                    graphic["position"] = position
                                interval = region.get("interval")
                                if interval is not None:
                                    graphic["interval"] = interval
                                display.setdefault("graphics", list()).append(graphic)
                        data_source.pop("regions", None)
                    for connection in properties.get("connections", list()):
                        if connection.get("type") == "interval-list-connection":
                            connection["source_uuid"] = properties["data_sources"][0]["displays"][0]["uuid"]
                    # pprint.pprint(properties)
                    # version 9 -> 10 merges regions into graphics.
                    properties["version"] = 10
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (regions merged into graphics)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v9(self, reader_info_list):
        data_source_uuid_to_data_item_uuid = dict()
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                data_source_dicts = properties.get("data_sources", list())
                for data_source_dict in data_source_dicts:
                    data_source_uuid_to_data_item_uuid[data_source_dict["uuid"]] = properties["uuid"]
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 8:
                    reader_info.changed_ref[0] = True
                    # version 8 -> 9 changes operations to computations
                    data_source_dicts = properties.get("data_sources", list())
                    for data_source_dict in data_source_dicts:
                        metadata = data_source_dict.get("metadata", dict())
                        hardware_source_dict = metadata.get("hardware_source", dict())
                        high_tension_v = hardware_source_dict.get("extra_high_tension")
                        # hardware_source_dict.pop("extra_high_tension", None)
                        if high_tension_v:
                            autostem_dict = hardware_source_dict.setdefault("autostem", dict())
                            autostem_dict["high_tension_v"] = high_tension_v
                    data_source_dicts = properties.get("data_sources", list())
                    ExpressionInfo = collections.namedtuple("ExpressionInfo", ["label", "expression", "processing_id", "src_labels", "src_names", "variables", "use_display_data"])
                    info = dict()
                    info["fft-operation"] = ExpressionInfo(_("FFT"), "xd.fft({src})", "fft", [_("Source")], ["src"], list(), True)
                    info["inverse-fft-operation"] = ExpressionInfo(_("Inverse FFT"), "xd.ifft({src})", "inverse-fft", [_("Source")], ["src"], list(), False)
                    info["auto-correlate-operation"] = ExpressionInfo(_("Auto Correlate"), "xd.autocorrelate({src})", "auto-correlate", [_("Source")], ["src"], list(), True)
                    info["cross-correlate-operation"] = ExpressionInfo(_("Cross Correlate"), "xd.crosscorrelate({src1}, {src2})", "cross-correlate", [_("Source1"), _("Source2")], ["src1", "src2"], list(), True)
                    info["invert-operation"] = ExpressionInfo(_("Invert"), "xd.invert({src})", "invert", [_("Source")], ["src"], list(), True)
                    info["sobel-operation"] = ExpressionInfo(_("Sobel"), "xd.sobel({src})", "sobel", [_("Source")], ["src"], list(), True)
                    info["laplace-operation"] = ExpressionInfo(_("Laplace"), "xd.laplace({src})", "laplace", [_("Source")], ["src"], list(), True)
                    sigma_var = {'control_type': 'slider', 'label': _('Sigma'), 'name': 'sigma', 'type': 'variable', 'value': 3.0, 'value_default': 3.0, 'value_max': 100.0, 'value_min': 0.0, 'value_type': 'real'}
                    info["gaussian-blur-operation"] = ExpressionInfo(_("Gaussian Blur"), "xd.gaussian_blur({src}, sigma)", "gaussian-blur", [_("Source")], ["src"], [sigma_var], True)
                    filter_size_var = {'label': _("Size"), 'op_name': 'size', 'name': 'filter_size', 'type': 'variable', 'value': 3, 'value_default': 3, 'value_max': 100, 'value_min': 1, 'value_type': 'integral'}
                    info["median-filter-operation"] = ExpressionInfo(_("Median Filter"), "xd.median_filter({src}, filter_size)", "median-filter", [_("Source")], ["src"], [filter_size_var], True)
                    info["uniform-filter-operation"] = ExpressionInfo(_("Uniform Filter"), "xd.uniform_filter({src}, filter_size)", "uniform-filter", [_("Source")], ["src"], [filter_size_var], True)
                    do_transpose_var = {'label': _("Tranpose"), 'op_name': 'transpose', 'name': 'do_transpose', 'type': 'variable', 'value': False, 'value_default': False, 'value_type': 'boolean'}
                    do_flip_v_var = {'label': _("Flip Vertical"), 'op_name': 'flip_horizontal', 'name': 'do_flip_v', 'type': 'variable', 'value': False, 'value_default': False, 'value_type': 'boolean'}
                    do_flip_h_var = {'label': _("Flip Horizontal"), 'op_name': 'flip_vertical', 'name': 'do_flip_h', 'type': 'variable', 'value': False, 'value_default': False, 'value_type': 'boolean'}
                    info["transpose-flip-operation"] = ExpressionInfo(_("Transpose/Flip"), "xd.transpose_flip({src}, do_transpose, do_flip_v, do_flip_h)", "transpose-flip", [_("Source")], ["src"], [do_transpose_var, do_flip_v_var, do_flip_h_var], True)
                    info["crop-operation"] = ExpressionInfo(_("Crop"), "xd.crop({src}, crop_region.bounds)", "crop", [_("Source")], ["src"], list(), True)
                    center_var = {'label': _("Center"), 'op_name': 'slice_center', 'name': 'center', 'type': 'variable', 'value': 0, 'value_default': 0, 'value_min': 0, 'value_type': 'integral'}
                    width_var = {'label': _("Width"), 'op_name': 'slice_width', 'name': 'width', 'type': 'variable', 'value': 1, 'value_default': 1, 'value_min': 1, 'value_type': 'integral'}
                    info["slice-operation"] = ExpressionInfo(_("Slice"), "xd.slice_sum({src}, center, width)", "slice", [_("Source")], ["src"], [center_var, width_var], False)
                    pt_var = {'label': _("Pick Point"), 'name': 'pick_region', 'type': 'variable', 'value_type': 'point'}
                    info["pick-operation"] = ExpressionInfo(_("Pick"), "xd.pick({src}, pick_region.position)", "pick-point", [_("Source")], ["src"], [pt_var], False)
                    info["projection-operation"] = ExpressionInfo(_("Sum"), "xd.sum({src}, 0)", "sum", [_("Source")], ["src"], list(), False)
                    width_var = {'label': _("Width"), 'name': 'width', 'type': 'variable', 'value': 256, 'value_default': 256, 'value_min': 1, 'value_type': 'integral'}
                    height_var = {'label': _("Height"), 'name': 'height', 'type': 'variable', 'value': 256, 'value_default': 256, 'value_min': 1, 'value_type': 'integral'}
                    info["resample-operation"] = ExpressionInfo(_("Reshape"), "xd.resample_image({src}, (height, width))", "resample", [_("Source")], ["src"], [width_var, height_var], True)
                    bins_var = {'label': _("Bins"), 'name': 'bins', 'type': 'variable', 'value': 256, 'value_default': 256, 'value_min': 2, 'value_type': 'integral'}
                    info["histogram-operation"] = ExpressionInfo(_("Histogram"), "xd.histogram({src}, bins)", "histogram", [_("Source")], ["src"], [bins_var], True)
                    line_var = {'label': _("Line Profile"), 'name': 'line_region', 'type': 'variable', 'value_type': 'line'}
                    info["line-profile-operation"] = ExpressionInfo(_("Line Profile"), "xd.line_profile({src}, line_region.vector, line_region.width)", "line-profile", [_("Source")], ["src"], [line_var], True)
                    info["convert-to-scalar-operation"] = ExpressionInfo(_("Scalar"), "{src}", "convert-to-scalar", [_("Source")], ["src"], list(), True)
                    # node-operation
                    for data_source_dict in data_source_dicts:
                        operation_dict = data_source_dict.get("data_source")
                        if operation_dict and operation_dict.get("type") == "operation":
                            del data_source_dict["data_source"]
                            operation_id = operation_dict["operation_id"]
                            computation_dict = dict()
                            if operation_id in info:
                                computation_dict["label"] = info[operation_id].label
                                computation_dict["processing_id"] = info[operation_id].processing_id
                                computation_dict["type"] = "computation"
                                computation_dict["uuid"] = str(uuid.uuid4())
                                variables_list = list()
                                data_sources = operation_dict.get("data_sources", list())
                                srcs = ("src", ) if len(data_sources) < 2 else ("src1", "src2")
                                kws = {}
                                for src in srcs:
                                    kws[src] = None
                                for i, src_data_source in enumerate(data_sources):
                                    kws[srcs[i]] = srcs[i] + (".display_data" if info[operation_id].use_display_data else ".data")
                                    if src_data_source.get("type") == "data-item-data-source":
                                        src_uuid = data_source_uuid_to_data_item_uuid.get(src_data_source["buffered_data_source_uuid"], str(uuid.uuid4()))
                                        variable_src = {"cascade_delete": True, "label": info[operation_id].src_labels[i], "name": info[operation_id].src_names[i], "type": "variable", "uuid": str(uuid.uuid4())}
                                        variable_src["specifier"] = {"type": "data_item", "uuid": src_uuid, "version": 1}
                                        variables_list.append(variable_src)
                                        if operation_id == "crop-operation":
                                            variable_src = {"cascade_delete": True, "label": _("Crop Region"), "name": "crop_region", "type": "variable", "uuid": str(uuid.uuid4())}
                                            variable_src["specifier"] = {"type": "region", "uuid": operation_dict["region_connections"]["crop"], "version": 1}
                                            variables_list.append(variable_src)
                                    elif src_data_source.get("type") == "operation":
                                        src_uuid = data_source_uuid_to_data_item_uuid.get(src_data_source["data_sources"][0]["buffered_data_source_uuid"], str(uuid.uuid4()))
                                        variable_src = {"cascade_delete": True, "label": info[operation_id].src_labels[i], "name": info[operation_id].src_names[i], "type": "variable", "uuid": str(uuid.uuid4())}
                                        variable_src["specifier"] = {"type": "data_item", "uuid": src_uuid, "version": 1}
                                        variables_list.append(variable_src)
                                        variable_src = {"cascade_delete": True, "label": _("Crop Region"), "name": "crop_region", "type": "variable", "uuid": str(uuid.uuid4())}
                                        variable_src["specifier"] = {"type": "region", "uuid": src_data_source["region_connections"]["crop"], "version": 1}
                                        variables_list.append(variable_src)
                                        kws[srcs[i]] = "xd.crop({}, crop_region.bounds)".format(kws[srcs[i]])
                                for rc_k, rc_v in operation_dict.get("region_connections", dict()).items():
                                    if rc_k == 'pick':
                                        variable_src = {"cascade_delete": True, "name": "pick_region", "type": "variable", "uuid": str(uuid.uuid4())}
                                        variable_src["specifier"] = {"type": "region", "uuid": rc_v, "version": 1}
                                        variables_list.append(variable_src)
                                    elif rc_k == 'line':
                                        variable_src = {"cascade_delete": True, "name": "line_region", "type": "variable", "uuid": str(uuid.uuid4())}
                                        variable_src["specifier"] = {"type": "region", "uuid": rc_v, "version": 1}
                                        variables_list.append(variable_src)
                                for var in copy.deepcopy(info[operation_id].variables):
                                    if var.get("value_type") not in ("line", "point"):
                                        var["uuid"] = str(uuid.uuid4())
                                        var_name = var.get("op_name") or var.get("name")
                                        var["value"] = operation_dict["values"].get(var_name, var.get("value"))
                                        variables_list.append(var)
                                computation_dict["variables"] = variables_list
                                computation_dict["original_expression"] = info[operation_id].expression.format(**kws)
                                data_source_dict["computation"] = computation_dict
                    properties["version"] = 9
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (operation to computation)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v8(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 7:
                    reader_info.changed_ref[0] = True
                    # version 7 -> 8 changes metadata to be stored in buffered_data_source
                    data_source_dicts = properties.get("data_sources", list())
                    description_metadata = properties.setdefault("metadata", dict()).setdefault("description", dict())
                    data_source_dict = dict()
                    if len(data_source_dicts) == 1:
                        data_source_dict = data_source_dicts[0]
                        excluded = ["rating", "datetime_original", "title", "source_file_path", "session_id", "caption", "flag", "datetime_modified", "connections", "data_sources", "uuid", "reader_version",
                            "version", "metadata"]
                        for key in list(properties.keys()):
                            if key not in excluded:
                                data_source_dict.setdefault("metadata", dict())[key] = properties[key]
                                del properties[key]
                        for key in ["caption", "flag", "rating", "title"]:
                            if key in properties:
                                description_metadata[key] = properties[key]
                                del properties[key]
                    datetime_original = properties.get("datetime_original", dict())
                    dst_value = datetime_original.get("dst", "+00")
                    dst_adjust = int(dst_value)
                    tz_value = datetime_original.get("tz", "+0000")
                    tz_adjust = int(tz_value[0:3]) * 60 + int(tz_value[3:5]) * (-1 if tz_value[0] == '-1' else 1)
                    local_datetime = Utility.get_datetime_from_datetime_item(datetime_original)
                    if not local_datetime:
                        local_datetime = datetime.datetime.utcnow()
                    data_source_dict["created"] = (local_datetime - datetime.timedelta(minutes=dst_adjust + tz_adjust)).isoformat()
                    data_source_dict["modified"] = data_source_dict["created"]
                    properties["created"] = data_source_dict["created"]
                    properties["modified"] = properties["created"]
                    time_zone_dict = description_metadata.setdefault("time_zone", dict())
                    time_zone_dict["dst"] = dst_value
                    time_zone_dict["tz"] = tz_value
                    properties.pop("datetime_original", None)
                    properties.pop("datetime_modified", None)
                    properties["version"] = 8
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (metadata to data source)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v7(self, reader_info_list):
        v7lookup = dict()  # map data_item.uuid to buffered_data_source.uuid
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 6:
                    reader_info.changed_ref[0] = True
                    # version 6 -> 7 changes data to be cached in the buffered data source object
                    buffered_data_source_dict = dict()
                    buffered_data_source_dict["type"] = "buffered-data-source"
                    buffered_data_source_dict["uuid"] = v7lookup.setdefault(properties["uuid"], str(uuid.uuid4()))  # assign a new uuid
                    include_data = "master_data_shape" in properties and "master_data_dtype" in properties
                    data_shape = properties.get("master_data_shape")
                    data_dtype = properties.get("master_data_dtype")
                    if "intensity_calibration" in properties:
                        buffered_data_source_dict["intensity_calibration"] = properties["intensity_calibration"]
                        del properties["intensity_calibration"]
                    if "dimensional_calibrations" in properties:
                        buffered_data_source_dict["dimensional_calibrations"] = properties["dimensional_calibrations"]
                        del properties["dimensional_calibrations"]
                    if "master_data_shape" in properties:
                        buffered_data_source_dict["data_shape"] = data_shape
                        del properties["master_data_shape"]
                    if "master_data_dtype" in properties:
                        buffered_data_source_dict["data_dtype"] = data_dtype
                        del properties["master_data_dtype"]
                    if "displays" in properties:
                        buffered_data_source_dict["displays"] = properties["displays"]
                        del properties["displays"]
                    if "regions" in properties:
                        buffered_data_source_dict["regions"] = properties["regions"]
                        del properties["regions"]
                    operation_dict = properties.pop("operation", None)
                    if operation_dict is not None:
                        buffered_data_source_dict["data_source"] = operation_dict
                        for data_source_dict in operation_dict.get("data_sources", dict()):
                            data_source_dict["buffered_data_source_uuid"] = v7lookup.setdefault(data_source_dict["data_item_uuid"], str(uuid.uuid4()))
                            data_source_dict.pop("data_item_uuid", None)
                    if include_data or operation_dict is not None:
                        properties["data_sources"] = [buffered_data_source_dict]
                    properties["version"] = 7
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (buffered data sources)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v6(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 5:
                    reader_info.changed_ref[0] = True
                    # version 5 -> 6 changes operations to a single operation, expands data sources list
                    operations_list = properties.get("operations", list())
                    if len(operations_list) == 1:
                        operation_dict = operations_list[0]
                        operation_dict["type"] = "operation"
                        properties["operation"] = operation_dict
                        data_sources_list = properties.get("data_sources", list())
                        if len(data_sources_list) > 0:
                            new_data_sources_list = list()
                            for data_source_uuid_str in data_sources_list:
                                new_data_sources_list.append({"type": "data-item-data-source", "data_item_uuid": data_source_uuid_str})
                            operation_dict["data_sources"] = new_data_sources_list
                    if "operations" in properties:
                        del properties["operations"]
                    if "data_sources" in properties:
                        del properties["data_sources"]
                    if "intrinsic_intensity_calibration" in properties:
                        properties["intensity_calibration"] = properties["intrinsic_intensity_calibration"]
                        del properties["intrinsic_intensity_calibration"]
                    if "intrinsic_spatial_calibrations" in properties:
                        properties["dimensional_calibrations"] = properties["intrinsic_spatial_calibrations"]
                        del properties["intrinsic_spatial_calibrations"]
                    properties["version"] = 6
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (operation hierarchy)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v5(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 4:
                    reader_info.changed_ref[0] = True
                    # version 4 -> 5 changes region_uuid to region_connections map.
                    operations_list = properties.get("operations", list())
                    for operation_dict in operations_list:
                        if operation_dict["operation_id"] == "crop-operation" and "region_uuid" in operation_dict:
                            operation_dict["region_connections"] = {"crop": operation_dict["region_uuid"]}
                            del operation_dict["region_uuid"]
                        elif operation_dict["operation_id"] == "line-profile-operation" and "region_uuid" in operation_dict:
                            operation_dict["region_connections"] = {"line": operation_dict["region_uuid"]}
                            del operation_dict["region_uuid"]
                    properties["version"] = 5
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (region_uuid)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v4(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 3:
                    reader_info.changed_ref[0] = True
                    # version 3 -> 4 changes origin to offset in all calibrations.
                    calibration_dict = properties.get("intrinsic_intensity_calibration", dict())
                    if "origin" in calibration_dict:
                        calibration_dict["offset"] = calibration_dict["origin"]
                        del calibration_dict["origin"]
                    for calibration_dict in properties.get("intrinsic_spatial_calibrations", list()):
                        if "origin" in calibration_dict:
                            calibration_dict["offset"] = calibration_dict["origin"]
                            del calibration_dict["origin"]
                    properties["version"] = 4
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (calibration offset)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v3(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version == 2:
                    reader_info.changed_ref[0] = True
                    # version 2 -> 3 adds uuid's to displays, graphics, and operations. regions already have uuids.
                    for display_properties in properties.get("displays", list()):
                        display_properties.setdefault("uuid", str(uuid.uuid4()))
                        for graphic_properties in display_properties.get("graphics", list()):
                            graphic_properties.setdefault("uuid", str(uuid.uuid4()))
                    for operation_properties in properties.get("operations", list()):
                        operation_properties.setdefault("uuid", str(uuid.uuid4()))
                    properties["version"] = 3
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (add uuids)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def __migrate_to_v2(self, reader_info_list):
        for reader_info in reader_info_list:
            persistent_storage_handler = reader_info.persistent_storage_handler
            properties = reader_info.properties
            try:
                version = properties.get("version", 0)
                if version <= 1:
                    if "spatial_calibrations" in properties:
                        properties["intrinsic_spatial_calibrations"] = properties["spatial_calibrations"]
                        del properties["spatial_calibrations"]
                    if "intensity_calibration" in properties:
                        properties["intrinsic_intensity_calibration"] = properties["intensity_calibration"]
                        del properties["intensity_calibration"]
                    if "data_source_uuid" in properties:
                        # for now, this is not translated into v2. it was an extra item.
                        del properties["data_source_uuid"]
                    if "properties" in properties:
                        old_properties = properties["properties"]
                        new_properties = properties.setdefault("hardware_source", dict())
                        new_properties.update(copy.deepcopy(old_properties))
                        if "session_uuid" in new_properties:
                            del new_properties["session_uuid"]
                        del properties["properties"]
                    temp_data = persistent_storage_handler.read_data()
                    if temp_data is not None:
                        properties["master_data_dtype"] = str(temp_data.dtype)
                        properties["master_data_shape"] = temp_data.shape
                    properties["displays"] = [{}]
                    properties["uuid"] = str(uuid.uuid4())  # assign a new uuid
                    properties["version"] = 2
                    if self.__log_migrations:
                        logging.info("Updated %s to %s (ndata1)", persistent_storage_handler.reference, properties["version"])
            except Exception as e:
                logging.debug("Error reading %s", persistent_storage_handler.reference)
                import traceback
                traceback.print_exc()
                traceback.print_stack()

    def write_data_item(self, data_item):
        """ Write data item to persistent storage. """
        persistent_storage = self._get_persistent_storage_for_object(data_item)
        if not persistent_storage:
            persistent_storage_handler = None
            for persistent_storage_system in self.__persistent_storage_systems:
                persistent_storage_handler = persistent_storage_system.make_persistent_storage_handler(data_item)
                if persistent_storage_handler:
                    break
            properties = data_item.write_to_dict()
            persistent_storage = DataItemPersistentStorage(persistent_storage_handler=persistent_storage_handler, data_item=data_item, properties=properties)
            self._set_persistent_storage_for_object(data_item, persistent_storage)
        data_item.persistent_object_context_changed()
        # write the uuid and version explicitly
        self.property_changed(data_item, "uuid", str(data_item.uuid))
        self.property_changed(data_item, "version", DataItem.DataItem.writer_version)
        if data_item.maybe_data_source:
            self.rewrite_data_item_data(data_item.maybe_data_source)

    def rewrite_data_item_data(self, buffered_data_source):
        persistent_storage = self._get_persistent_storage_for_object(buffered_data_source)
        persistent_storage.update_data(buffered_data_source.data)

    def erase_data_item(self, data_item):
        persistent_storage = self._get_persistent_storage_for_object(data_item)
        persistent_storage.remove()
        data_item.persistent_object_context = None

    def load_data(self, data_item):
        persistent_storage = self._get_persistent_storage_for_object(data_item)
        return persistent_storage.load_data()

    def _test_get_file_path(self, data_item):
        persistent_storage = self._get_persistent_storage_for_object(data_item)
        return persistent_storage._persistent_storage_handler.reference


class ComputationQueueItem:
    def __init__(self, data_item, buffered_data_source, computation):
        self.data_item = data_item
        self.buffered_data_source = buffered_data_source
        self.computation = computation
        self.valid = True


class DocumentModel(Observable.Observable, ReferenceCounting.ReferenceCounted, Persistence.PersistentObject):

    """The document model manages storage and dependencies between data items and other objects.

    The document model provides a dispatcher object which will run tasks in a thread pool.
    """

    computation_min_period = 0.0

    def __init__(self, library_storage=None, persistent_storage_systems=None, storage_cache=None, log_migrations=True, ignore_older_files=False):
        super(DocumentModel, self).__init__()

        self.data_item_deleted_event = Event.Event()  # will be called after the item is deleted
        self.data_item_will_be_removed_event = Event.Event()  # will be called before the item is deleted
        self.data_item_inserted_event = Event.Event()
        self.data_item_removed_event = Event.Event()

        self.__thread_pool = ThreadPool.ThreadPool()
        self.__computation_thread_pool = ThreadPool.ThreadPool()
        self.persistent_object_context = PersistentDataItemContext(persistent_storage_systems, ignore_older_files, log_migrations)
        self.__library_storage = library_storage if library_storage else FilePersistentStorage()
        self.persistent_object_context._set_persistent_storage_for_object(self, self.__library_storage)
        self.storage_cache = storage_cache if storage_cache else Cache.DictStorageCache()
        self.__transactions_lock = threading.RLock()
        self.__transactions = dict()
        self.__live_data_items_lock = threading.RLock()
        self.__live_data_items = dict()
        self.__dependent_data_items_lock = threading.RLock()
        self.__dependent_data_items = dict()
        self.__source_data_items = dict()
        self.__computation_dependency_data_items = dict()
        self.__data_items = list()
        self.__data_item_item_inserted_listeners = dict()
        self.__data_item_item_removed_listeners = dict()
        self.__data_item_request_remove_region = dict()
        self.__computation_changed_or_mutated_listeners = dict()
        self.__data_item_request_remove_data_item_listeners = dict()
        self.__data_item_references = dict()
        self.__computation_queue_lock = threading.RLock()
        self.__computation_pending_queue = list()  # type: typing.List[ComputationQueueItem]
        self.__computation_active_items = list()  # type: typing.List[ComputationQueueItem]
        self.define_type("library")
        self.define_relationship("data_groups", DataGroup.data_group_factory)
        self.define_relationship("workspaces", WorkspaceLayout.factory)  # TODO: file format. Rename workspaces to workspace_layouts.
        self.define_property("session_metadata", dict(), copy_on_read=True, changed=self.__session_metadata_changed)
        self.define_property("workspace_uuid", converter=Converter.UuidToStringConverter())
        self.define_property("data_item_references", dict(), hidden=True)
        self.__buffered_data_source_set = set()
        self.__buffered_data_source_set_changed_event = Event.Event()
        self.session_id = None
        self.start_new_session()
        self.__computation_changed_listeners = dict()
        self.__read()
        self.__library_storage.set_property(self, "uuid", str(self.uuid))
        self.__library_storage.set_property(self, "version", 0)

        self.__data_channel_updated_listeners = dict()
        self.__data_channel_start_listeners = dict()
        self.__data_channel_stop_listeners = dict()
        self.__data_channel_states_updated_listeners = dict()
        self.__last_data_items_dict = dict()  # maps hardware source to list of data items for that hardware source

        self.append_data_item_event = Event.Event()

        def append_data_item(data_item):
            self.append_data_item_event.fire_any(data_item)

        self.__pending_data_item_updates_lock = threading.RLock()
        self.__pending_data_item_updates = list()
        self.perform_data_item_updates_event = Event.Event()

        self.__hardware_source_added_event_listener = HardwareSource.HardwareSourceManager().hardware_source_added_event.listen(functools.partial(self.__hardware_source_added, append_data_item))
        self.__hardware_source_removed_event_listener = HardwareSource.HardwareSourceManager().hardware_source_removed_event.listen(self.__hardware_source_removed)

        for hardware_source in HardwareSource.HardwareSourceManager().hardware_sources:
            self.__hardware_source_added(append_data_item, hardware_source)

    def __read(self):
        # first read the items
        self.read_from_dict(self.__library_storage.properties)
        data_items = self.persistent_object_context.read_data_items()
        self.__finish_read(data_items)

    def __finish_read(self, data_items: typing.List[DataItem.DataItem]) -> None:
        for index, data_item in enumerate(data_items):
            self.__data_items.insert(index, data_item)
            data_item.set_storage_cache(self.storage_cache)
            self.__data_item_item_inserted_listeners[data_item.uuid] = data_item.item_inserted_event.listen(self.__item_inserted)
            self.__data_item_item_removed_listeners[data_item.uuid] = data_item.item_removed_event.listen(self.__item_removed)
            self.__data_item_request_remove_region[data_item.uuid] = data_item.request_remove_region_event.listen(self.__remove_region_specifier)
            self.__computation_changed_or_mutated_listeners[data_item.uuid] = data_item.computation_changed_or_mutated_event.listen(self.__handle_computation_changed_or_mutated)
            self.__data_item_request_remove_data_item_listeners[data_item.uuid] = data_item.request_remove_data_item_event.listen(self.__request_remove_data_item)
            data_item.set_data_item_manager(self)
            self.__buffered_data_source_set.update(set(data_item.data_sources))
            self.buffered_data_source_set_changed_event.fire(set(data_item.data_sources), set())
        # all sorts of interconnections may occur between data items and other objects. give the data item a chance to
        # mark itself clean after reading all of them in.
        for data_item in data_items:
            data_item.finish_reading()
        for data_item in data_items:
            if data_item.maybe_data_source:
                self.__handle_computation_changed_or_mutated(data_item, data_item.maybe_data_source, data_item.maybe_data_source.computation)
        for data_item in data_items:
            for buffered_data_source in data_item.data_sources:
                computation = buffered_data_source.computation
                if computation:
                    try:
                        self.update_computation(computation)
                        computation.bind(self)
                    except Exception as e:
                        print(str(e))
            data_item.connect_data_items(data_items, self.get_data_item_by_uuid)
        # # initialize data item references
        data_item_references_dict = self._get_persistent_property_value("data_item_references")
        for key, data_item_uuid in data_item_references_dict.items():
            data_item = self.get_data_item_by_uuid(uuid.UUID(data_item_uuid))
            if data_item:
                self.__data_item_references.setdefault(key, DocumentModel.DataItemReference(self, key, data_item))
        # all data items will already have a persistent_object_context
        for data_group in self.data_groups:
            data_group.connect_data_items(data_items, self.get_data_item_by_uuid)

    def close(self):
        # stop computations
        with self.__computation_queue_lock:
            self.__computation_pending_queue.clear()
            for computation_queue_item in self.__computation_active_items:
                computation_queue_item.valid = False
            self.__computation_active_items.clear()

        # close hardware source related stuff
        self.__hardware_source_added_event_listener.close()
        self.__hardware_source_added_event_listener = None
        self.__hardware_source_removed_event_listener.close()
        self.__hardware_source_removed_event_listener = None
        for listener in self.__data_channel_states_updated_listeners.values():
            listener.close()
        self.__data_channel_states_updated_listeners = None
        # TODO: close other listeners here too
        HardwareSource.HardwareSourceManager().abort_all_and_close()

        # make sure the data item references shut down cleanly
        for data_item in self.data_items:
            for data_item_reference in self.__data_item_references.values():
                data_item_reference.data_item_removed(data_item)

        for listeners in self.__data_channel_updated_listeners.values():
            for listener in listeners:
                listener.close()
        for listeners in self.__data_channel_start_listeners.values():
            for listener in listeners:
                listener.close()
        for listeners in self.__data_channel_stop_listeners.values():
            for listener in listeners:
                listener.close()
        self.__data_channel_updated_listeners = None
        self.__data_channel_start_listeners = None
        self.__data_channel_stop_listeners = None

        self.__thread_pool.close()
        self.__computation_thread_pool.close()
        for data_item in self.data_items:
            data_item.about_to_be_removed()
            data_item.close()
        self.storage_cache.close()

    def about_to_delete(self):
        # override from ReferenceCounted. several DocumentControllers may retain references
        self.close()
        # these are here so that the document model gets garbage collected.
        # TODO: generalize this behavior into a close method on persistent object
        self.undefine_properties()
        self.undefine_items()
        self.undefine_relationships()

    def auto_migrate(self, paths: typing.List[str], log_copying: bool=True) -> None:
        file_persistent_storage_system = FilePersistentStorageSystem(paths)
        persistent_object_context = PersistentDataItemContext([file_persistent_storage_system], ignore_older_files=False, log_migrations=False, log_copying=log_copying)
        data_items = persistent_object_context.read_data_items(target_document=self)
        self.__finish_read(data_items)

    def start_new_session(self):
        self.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    def __session_metadata_changed(self, name, value):
        self.notify_set_property("session_metadata", self.session_metadata)

    def set_session_field(self, field_id: str, value: str) -> None:
        session_metadata = self.session_metadata
        session_metadata[field_id] = str(value)
        self.session_metadata = session_metadata

    def get_session_field(self, field_id: str) -> str:
        return self.session_metadata.get(field_id)

    def append_workspace(self, workspace):
        self.insert_workspace(len(self.workspaces), workspace)

    def insert_workspace(self, before_index, workspace):
        self.insert_item("workspaces", before_index, workspace)
        self.notify_insert_item("workspaces", workspace, before_index)

    def remove_workspace(self, workspace):
        index = self.workspaces.index(workspace)
        self.remove_item("workspaces", workspace)
        self.notify_remove_item("workspaces", workspace, index)

    def append_data_item(self, data_item):
        self.insert_data_item(len(self.data_items), data_item)

    def insert_data_item(self, before_index, data_item):
        """Insert a new data item into document model.

        Data item will have persistent_object_context set upon return.

        This method is NOT threadsafe.
        """
        assert data_item is not None
        assert data_item not in self.__data_items
        assert before_index <= len(self.__data_items) and before_index >= 0
        # insert in internal list
        self.__data_items.insert(before_index, data_item)
        data_item.set_storage_cache(self.storage_cache)
        data_item.persistent_object_context = self.persistent_object_context
        self.persistent_object_context.write_data_item(data_item)
        self.__data_item_item_inserted_listeners[data_item.uuid] = data_item.item_inserted_event.listen(self.__item_inserted)
        self.__data_item_item_removed_listeners[data_item.uuid] = data_item.item_removed_event.listen(self.__item_removed)
        self.__data_item_request_remove_region[data_item.uuid] = data_item.request_remove_region_event.listen(self.__remove_region_specifier)
        self.__computation_changed_or_mutated_listeners[data_item.uuid] = data_item.computation_changed_or_mutated_event.listen(self.__handle_computation_changed_or_mutated)
        if data_item.maybe_data_source:
            self.__handle_computation_changed_or_mutated(data_item, data_item.maybe_data_source, data_item.maybe_data_source.computation)
        self.__data_item_request_remove_data_item_listeners[data_item.uuid] = data_item.request_remove_data_item_event.listen(self.__request_remove_data_item)
        self.data_item_inserted_event.fire(self, data_item, before_index, False)
        for data_item_reference in self.__data_item_references.values():
            data_item_reference.data_item_inserted(data_item)
        data_item.set_data_item_manager(self)
        # fire buffered_data_source_set_changed_event
        self.__buffered_data_source_set.update(set(data_item.data_sources))
        self.buffered_data_source_set_changed_event.fire(set(data_item.data_sources), set())
        # handle computation
        for data_source in data_item.data_sources:
            self.computation_changed(data_item, data_source, data_source.computation)
            if data_source.computation:
                data_source.computation.bind(self)

    def remove_data_item(self, data_item):
        """Remove data item from document model.

        Data item will have persistent_object_context cleared upon return.

        This method is NOT threadsafe.
        """
        # remove data item from any computations
        with self.__computation_queue_lock:
            for computation_queue_item in self.__computation_pending_queue + self.__computation_active_items:
                if computation_queue_item.buffered_data_source in data_item.data_sources:
                    computation_queue_item.valid = False
        # remove data item from any selections
        self.data_item_will_be_removed_event.fire(data_item)
        # remove the data item from any groups
        for data_group in self.get_flat_data_group_generator():
            if data_item in data_group.data_items:
                data_group.remove_data_item(data_item)
        # remove data items that are entirely dependent on data item being removed
        # entirely dependent means that the data item has a single data item source
        # and it matches the data_item being removed.
        for other_data_item in self.get_dependent_data_items(data_item):
            if self.get_source_data_items(other_data_item) == [data_item]:  # ordered data sources exactly equal to data item?
                self.remove_data_item(other_data_item)
        # fire buffered_data_source_set_changed_event
        self.__buffered_data_source_set.difference_update(set(data_item.data_sources))
        self.buffered_data_source_set_changed_event.fire(set(), set(data_item.data_sources))
        # tell the data item it is about to be removed
        data_item.about_to_be_removed()
        # disconnect the data source
        data_item.set_data_item_manager(None)
        # remove it from the persistent_storage
        assert data_item is not None
        assert data_item in self.__data_items
        index = self.__data_items.index(data_item)
        # do actual removal
        del self.__data_items[index]
        # keep storage up-to-date
        self.persistent_object_context.erase_data_item(data_item)
        data_item.__storage_cache = None
        self.__data_item_item_inserted_listeners[data_item.uuid].close()
        del self.__data_item_item_inserted_listeners[data_item.uuid]
        self.__data_item_item_removed_listeners[data_item.uuid].close()
        del self.__data_item_item_removed_listeners[data_item.uuid]
        self.__data_item_request_remove_region[data_item.uuid].close()
        del self.__data_item_request_remove_region[data_item.uuid]
        self.__computation_changed_or_mutated_listeners[data_item.uuid].close()
        del self.__computation_changed_or_mutated_listeners[data_item.uuid]
        self.__data_item_request_remove_data_item_listeners[data_item.uuid].close()
        del self.__data_item_request_remove_data_item_listeners[data_item.uuid]
        # update data item count
        for data_item_reference in self.__data_item_references.values():
            data_item_reference.data_item_removed(data_item)
        self.data_item_removed_event.fire(self, data_item, index, False)
        data_item.close()  # make sure dependents get updated. argh.
        self.data_item_deleted_event.fire(data_item)

    def __item_inserted(self, key, value, before_index):
        # called when a relationship in one of the items we're observing changes.
        if key == "data_sources":
            # fire buffered_data_source_set_changed_event
            assert isinstance(value, DataItem.BufferedDataSource)
            data_source = value
            self.__buffered_data_source_set.update(set([data_source]))
            self.buffered_data_source_set_changed_event.fire(set([data_source]), set())

    def __item_removed(self, key, value, index):
        # called when a relationship in one of the items we're observing changes.
        if key == "data_sources":
            # fire buffered_data_source_set_changed_event
            assert isinstance(value, DataItem.BufferedDataSource)
            data_source = value
            self.__buffered_data_source_set.difference_update(set([data_source]))
            self.buffered_data_source_set_changed_event.fire(set(), set([data_source]))

    def __remove_region_specifier(self, region_specifier) -> None:
        bound_region = self.resolve_object_specifier(region_specifier)
        if bound_region:
            region = bound_region.value._graphic
            for data_item in self.data_items:
                for data_source in data_item.data_sources:
                    for display in data_source.displays:
                        if region in display.graphics:
                            if not region._about_to_be_removed:  # HACK! to handle document closing. Argh.
                                display.remove_graphic(region)
                                break

    def __remove_dependency(self, source_data_item, target_data_item):
        with self.__dependent_data_items_lock:
            self.__dependent_data_items.setdefault(weakref.ref(source_data_item), list()).remove(target_data_item)
            self.__source_data_items.setdefault(weakref.ref(target_data_item), list()).remove(source_data_item)
        # propagate transaction and live states to dependents
        if source_data_item.in_transaction_state:
            self.end_data_item_transaction(target_data_item)
        if source_data_item.is_live:
            self.end_data_item_live(target_data_item)

    def __add_dependency(self, source_data_item, target_data_item):
        assert isinstance(source_data_item, DataItem.DataItem)
        assert isinstance(target_data_item, DataItem.DataItem)
        with self.__dependent_data_items_lock:
            self.__dependent_data_items.setdefault(weakref.ref(source_data_item), list()).append(target_data_item)
            self.__source_data_items.setdefault(weakref.ref(target_data_item), list()).append(source_data_item)
        # propagate transaction and live states to dependents
        if source_data_item.in_transaction_state:
            self.begin_data_item_transaction(target_data_item)
        if source_data_item.is_live:
            self.begin_data_item_live(target_data_item)

    def __handle_computation_changed_or_mutated(self, data_item, data_source, computation):
        with self.__dependent_data_items_lock:
            source_data_item_set = self.__computation_dependency_data_items.setdefault(weakref.ref(data_item), set())
            for source_data_item in source_data_item_set:
                self.__remove_dependency(source_data_item, data_item)
            source_data_item_set.clear()
            if computation:
                for variable in computation.variables:
                    specifier = variable.specifier
                    if specifier:
                        object = self.resolve_object_specifier(variable.specifier)
                        if object and hasattr(object.value, "_data_item"):
                            source_data_item = object.value._data_item
                            if not source_data_item in source_data_item_set:
                                source_data_item_set.add(source_data_item)
                                self.__add_dependency(source_data_item, data_item)

    def rebind_computations(self):
        """Call this to rebind all computations.

        This is helpful when extending the computation type system.
        After new objcts have been loaded, call this so that existing
        computations can find the new objects during startup.
        """
        for data_item in self.data_items:
            for data_source in data_item.data_sources:
                if data_source.computation:
                    data_source.computation.unbind()
                    data_source.computation.bind(self)

    # TODO: evaluate if buffered_data_source_set is needed
    @property
    def buffered_data_source_set(self):
        return self.__buffered_data_source_set

    @property
    def buffered_data_source_set_changed_event(self):
        return self.__buffered_data_source_set_changed_event

    def __get_data_items(self):
        return tuple(self.__data_items)  # tuple makes it read only
    data_items = property(__get_data_items)

    # transactions, live state, and dependencies

    def get_source_data_items(self, data_item):
        with self.__dependent_data_items_lock:
            return copy.copy(self.__source_data_items.get(weakref.ref(data_item), list()))

    def get_dependent_data_items(self, data_item):
        """Return the list of data items containing data that directly depends on data in this item."""
        with self.__dependent_data_items_lock:
            return copy.copy(self.__dependent_data_items.get(weakref.ref(data_item), list()))

    def data_item_transaction(self, data_item):
        """ Return a context manager to put the data item under a 'transaction'. """
        class TransactionContextManager:
            def __init__(self, manager, object):
                self.__manager = manager
                self.__object = object
            def __enter__(self):
                self.__manager.begin_data_item_transaction(self.__object)
                return self
            def __exit__(self, type, value, traceback):
                self.__manager.end_data_item_transaction(self.__object)
        return TransactionContextManager(self, data_item)

    def begin_data_item_transaction(self, data_item):
        """Begin transaction state.

        A transaction state is exists to prevent writing out to disk, mainly for performance reasons.
        All changes to the object are delayed until the transaction state exits.

        Has the side effects of entering the write delay state, cache delay state (via is_cached_delayed),
        loading data of data sources, and entering transaction state for dependent data items.

        This method is thread safe.
        """
        with self.__transactions_lock:
            old_transaction_count = self.__transactions.get(data_item.uuid, 0)
            self.__transactions[data_item.uuid] = old_transaction_count + 1
        # if the old transaction count was 0, it means we're entering the transaction state.
        if old_transaction_count == 0:
            data_item._enter_transaction_state()
            # finally, tell dependent data items to enter their transaction states also
            # so that they also don't write change to disk immediately.
            for dependent_data_item in self.get_dependent_data_items(data_item):
                self.begin_data_item_transaction(dependent_data_item)

    def end_data_item_transaction(self, data_item):
        """End transaction state.

        Has the side effects of exiting the write delay state, cache delay state (via is_cached_delayed),
        unloading data of data sources, and exiting transaction state for dependent data items.

        As a consequence of exiting write delay state, data and metadata may be written to disk.

        As a consequence of existing cache delay state, cache may be written to disk.

        This method is thread safe.
        """
        # maintain the transaction count under a mutex
        with self.__transactions_lock:
            transaction_count = self.__transactions.get(data_item.uuid, 0) - 1
            assert transaction_count >= 0
            self.__transactions[data_item.uuid] = transaction_count
        # if the new transaction count is 0, it means we're exiting the transaction state.
        if transaction_count == 0:
            # first, tell our dependent data items to exit their transaction states.
            for dependent_data_item in self.get_dependent_data_items(data_item):
                self.end_data_item_transaction(dependent_data_item)
            data_item._exit_transaction_state()

    def data_item_live(self, data_item):
        """ Return a context manager to put the data item in a 'live state'. """
        class LiveContextManager:
            def __init__(self, manager, object):
                self.__manager = manager
                self.__object = object
            def __enter__(self):
                self.__manager.begin_data_item_live(self.__object)
                return self
            def __exit__(self, type, value, traceback):
                self.__manager.end_data_item_live(self.__object)
        return LiveContextManager(self, data_item)

    def begin_data_item_live(self, data_item):
        """Begins a live transaction for the data item.

        The live state is propagated to dependent data items.

        This method is thread safe. See slow_test_dependent_data_item_removed_while_live_data_item_becomes_unlive.
        """
        with self.__live_data_items_lock:
            old_live_count = self.__live_data_items.get(data_item.uuid, 0)
            self.__live_data_items[data_item.uuid] = old_live_count + 1
        if old_live_count == 0:
            data_item._enter_live_state()
            for dependent_data_item in self.get_dependent_data_items(data_item):
                self.begin_data_item_live(dependent_data_item)

    def end_data_item_live(self, data_item):
        """Ends a live transaction for the data item.

        The live-ness property is propagated to dependent data items, similar to the transactions.

        This method is thread safe.
        """
        with self.__live_data_items_lock:
            live_count = self.__live_data_items.get(data_item.uuid, 0) - 1
            assert live_count >= 0
            self.__live_data_items[data_item.uuid] = live_count
        if live_count == 0:
            data_item._exit_live_state()
            for dependent_data_item in self.get_dependent_data_items(data_item):
                self.end_data_item_live(dependent_data_item)

    # data groups

    def append_data_group(self, data_group):
        self.insert_data_group(len(self.data_groups), data_group)

    def insert_data_group(self, before_index, data_group):
        self.insert_item("data_groups", before_index, data_group)
        self.notify_insert_item("data_groups", data_group, before_index)

    def remove_data_group(self, data_group):
        data_group.disconnect_data_items()
        index = self.data_groups.index(data_group)
        self.remove_item("data_groups", data_group)
        self.notify_remove_item("data_groups", data_group, index)

    def create_default_data_groups(self):
        # ensure there is at least one group
        if len(self.data_groups) < 1:
            data_group = DataGroup.DataGroup()
            data_group.title = _("My Data")
            self.append_data_group(data_group)

    def create_sample_images(self, resources_path):
        if True:
            data_group = self.get_or_create_data_group(_("Example Data"))
            handler = ImportExportManager.NDataImportExportHandler("ndata1-io-handler", None, ["ndata1"])
            samples_dir = os.path.join(resources_path, "SampleImages")
            #logging.debug("Looking in %s", samples_dir)
            def is_ndata(file_path):
                #logging.debug("Checking %s", file_path)
                _, extension = os.path.splitext(file_path)
                return extension == ".ndata1"
            if os.path.isdir(samples_dir):
                sample_paths = [os.path.normpath(os.path.join(samples_dir, d)) for d in os.listdir(samples_dir) if is_ndata(os.path.join(samples_dir, d))]
            else:
                sample_paths = []
            for sample_path in sorted(sample_paths):
                data_items = handler.read_data_items(None, "ndata1", sample_path)
                for data_item in data_items:
                    if not self.get_data_item_by_uuid(data_item.uuid):
                        self.append_data_item(data_item)
                        data_group.append_data_item(data_item)
        else:
            # for testing, add a checkerboard image data item
            checkerboard_data_item = DataItem.DataItem(Image.create_checkerboard((512, 512)))
            checkerboard_data_item.title = "Checkerboard"
            self.append_data_item(checkerboard_data_item)
            # for testing, add a color image data item
            color_data_item = DataItem.DataItem(Image.create_color_image((512, 512), 128, 255, 128))
            color_data_item.title = "Green Color"
            self.append_data_item(color_data_item)
            # for testing, add a color image data item
            lena_data_item = DataItem.DataItem(scipy.misc.lena())
            lena_data_item.title = "Lena"
            self.append_data_item(lena_data_item)

    # this message comes from a data item when it wants to be removed from the document. ugh.
    def __request_remove_data_item(self, data_item):
        DataGroup.get_data_item_container(self, data_item).remove_data_item(data_item)

    # TODO: what about thread safety for these classes?

    # Return a generator over all data items
    def get_flat_data_item_generator(self):
        for data_item in self.data_items:
            yield data_item

    # Return a generator over all data groups
    def get_flat_data_group_generator(self):
        return DataGroup.get_flat_data_group_generator_in_container(self)

    def get_data_group_by_uuid(self, uuid):
        for data_group in DataGroup.get_flat_data_group_generator_in_container(self):
            if data_group.uuid == uuid:
                return data_group
        return None

    def get_data_item_count(self):
        return len(list(self.get_flat_data_item_generator()))

    # temporary method to find the container of a data item. this goes away when
    # data items get stored in a flat table.
    def get_data_item_data_group(self, data_item):
        for data_group in self.get_flat_data_group_generator():
            if data_item in DataGroup.get_flat_data_item_generator_in_container(data_group):
                return data_group
        return None

    # access data item by key (title, uuid, index)
    def get_data_item_by_key(self, key):
        if isinstance(key, numbers.Integral):
            return list(self.get_flat_data_item_generator())[key]
        if isinstance(key, uuid.UUID):
            return self.get_data_item_by_uuid(key)
        return self.get_data_item_by_title(str(key))

    # access data items by title
    def get_data_item_by_title(self, title):
        for data_item in self.get_flat_data_item_generator():
            if data_item.title == title:
                return data_item
        return None

    # access data items by index
    def get_data_item_by_index(self, index):
        return list(self.get_flat_data_item_generator())[index]

    def get_index_for_data_item(self, data_item):
        return list(self.get_flat_data_item_generator()).index(data_item)

    # access data items by uuid
    def get_data_item_by_uuid(self, uuid: uuid.UUID) -> DataItem.DataItem:
        for data_item in self.get_flat_data_item_generator():
            if data_item.uuid == uuid:
                return data_item
        return None

    def get_or_create_data_group(self, group_name):
        data_group = DataGroup.get_data_group_in_container_by_title(self, group_name)
        if data_group is None:
            # we create a new group
            data_group = DataGroup.DataGroup()
            data_group.title = group_name
            self.insert_data_group(0, data_group)
        return data_group

    def create_computation(self, expression: str=None) -> Symbolic.Computation:
        computation = Symbolic.Computation(expression)
        computation.bind(self)
        return computation

    def dispatch_task(self, task, description=None):
        self.__thread_pool.queue_fn(task, description)

    def dispatch_task2(self, task, description=None):
        self.__computation_thread_pool.queue_fn(task, description)

    def recompute_all(self):
        self.__computation_thread_pool.run_all()

    def recompute_one(self):
        self.__computation_thread_pool.run_one()

    def start_dispatcher(self):
        self.__thread_pool.start()
        self.__computation_thread_pool.start(1)

    def __recompute(self):
        computation_queue_item = None
        with self.__computation_queue_lock:
            if len(self.__computation_pending_queue) > 0:
                computation_queue_item = self.__computation_pending_queue.pop(0)
                self.__computation_active_items.append(computation_queue_item)
        data_item = computation_queue_item.data_item
        buffered_data_source = computation_queue_item.buffered_data_source
        computation = computation_queue_item.computation
        if computation:
            try:
                api = PlugInManager.api_broker_fn("~1.0", None)
                data_item_clone = data_item.clone()
                api_data_item = DataItem.new_api_data_item("~1.0", data_item_clone)
                if computation.needs_update:
                    computation.evaluate_with_target(api, api_data_item)
                    throttle_time = max(DocumentModel.computation_min_period - (time.perf_counter() - computation.last_evaluate_data_time), 0)
                    time.sleep(max(throttle_time, 0.0))
                if computation_queue_item.valid:  # TODO: race condition for 'valid'
                    data_item_data_modified = data_item.maybe_data_source.data_modified or datetime.datetime.min
                    data_item_data_clone_modified = data_item_clone.maybe_data_source.data_modified or datetime.datetime.min
                    if data_item_data_clone_modified > data_item_data_modified:
                        buffered_data_source.set_data_and_metadata(api_data_item.data_and_metadata)
                    data_item.merge_from_clone(data_item_clone)
            except Exception as e:
                import traceback
                traceback.print_exc()
                computation.error_text = _("Unable to compute data")
        with self.__computation_queue_lock:
            self.__computation_active_items.remove(computation_queue_item)

    def recompute_immediate(self, data_item: DataItem.DataItem) -> None:
        # this can be called on the UI thread; but it can also take time. use sparingly.
        # need the data to make connect_explorer_interval work; so do this here. ugh.
        buffered_data_source = data_item.maybe_data_source
        data_and_metadata = DataItem.evaluate_data(buffered_data_source.computation)
        if data_and_metadata:
            with buffered_data_source._changes():
                with buffered_data_source.data_ref() as data_ref:
                    data_ref.data = data_and_metadata.data
                    buffered_data_source.update_metadata(data_and_metadata.metadata)
                    buffered_data_source.set_intensity_calibration(data_and_metadata.intensity_calibration)
                    buffered_data_source.set_dimensional_calibrations(data_and_metadata.dimensional_calibrations)

    def computation_changed(self, data_item, buffered_data_source, computation):
        existing_computation_changed_listener = self.__computation_changed_listeners.get(buffered_data_source.uuid)
        if existing_computation_changed_listener:
            existing_computation_changed_listener.close()
            del self.__computation_changed_listeners[buffered_data_source.uuid]
        if computation:

            def computation_needs_update():
                with self.__computation_queue_lock:
                    for computation_queue_item in self.__computation_pending_queue:
                        if computation_queue_item.buffered_data_source == buffered_data_source:
                            computation_queue_item.computation = computation
                            return
                    computation_queue_item = ComputationQueueItem(data_item, buffered_data_source, computation)
                    self.__computation_pending_queue.append(computation_queue_item)
                self.dispatch_task2(self.__recompute)

            computation_changed_listener = computation.needs_update_event.listen(computation_needs_update)
            self.__computation_changed_listeners[buffered_data_source.uuid] = computation_changed_listener
            computation_needs_update()
        else:
            with self.__computation_queue_lock:
                for computation_queue_item in self.__computation_pending_queue + self.__computation_active_items:
                    if computation_queue_item.buffered_data_source == buffered_data_source:
                        computation_queue_item.valid = False
                        return

    def get_object_specifier(self, object):
        if isinstance(object, DataItem.DataItem):
            return {"version": 1, "type": "data_item", "uuid": str(object.uuid)}
        elif isinstance(object, Graphics.Graphic):
            return {"version": 1, "type": "region", "uuid": str(object.uuid)}
        return Symbolic.ComputationVariable.get_extension_object_specifier(object)

    def resolve_object_specifier(self, specifier: dict):
        document_model = self
        if specifier.get("version") == 1:
            specifier_type = specifier["type"]
            if specifier_type == "data_item":
                specifier_uuid_str = specifier.get("uuid")
                object_uuid = uuid.UUID(specifier_uuid_str) if specifier_uuid_str else None
                data_item = self.get_data_item_by_uuid(object_uuid) if object_uuid else None
                class BoundDataItem:
                    def __init__(self, data_item):
                        self.__data_item = data_item
                        self.__buffered_data_source = data_item.maybe_data_source
                        self.changed_event = Event.Event()
                        self.deleted_event = Event.Event()
                        def data_and_metadata_changed():
                            self.changed_event.fire()
                        def data_item_will_be_removed(data_item):
                            if data_item == self.__data_item:
                                self.deleted_event.fire()
                        self.__data_and_metadata_changed_event_listener = self.__buffered_data_source.data_and_metadata_changed_event.listen(data_and_metadata_changed)
                        self.__data_item_will_be_removed_event_listener = document_model.data_item_will_be_removed_event.listen(data_item_will_be_removed)
                    @property
                    def value(self):
                        return DataItem.new_api_data_item("~1.0", self.__data_item)
                    def close(self):
                        self.__data_and_metadata_changed_event_listener.close()
                        self.__data_and_metadata_changed_event_listener = None
                        self.__data_item_will_be_removed_event_listener.close()
                        self.__data_item_will_be_removed_event_listener = None
                if data_item:
                    return BoundDataItem(data_item)
            elif specifier_type == "region":
                specifier_uuid_str = specifier.get("uuid")
                object_uuid = uuid.UUID(specifier_uuid_str) if specifier_uuid_str else None
                for data_item in self.data_items:
                    for data_source in data_item.data_sources:
                        for display in data_source.displays:
                            for graphic in display.graphics:
                                if graphic.uuid == object_uuid:
                                    class BoundGraphic:
                                        def __init__(self, display, object):
                                            self.__object = object
                                            self.changed_event = Event.Event()
                                            self.deleted_event = Event.Event()
                                            def remove_region(region):
                                                if region == object:
                                                    self.deleted_event.fire()
                                            self.__remove_region_listener = display.display_graphic_will_remove_event.listen(remove_region)
                                            def property_changed(property_name_being_changed, value):
                                                self.changed_event.fire()
                                            self.__property_changed_listener = self.__object.property_changed_event.listen(property_changed)
                                        def close(self):
                                            self.__property_changed_listener.close()
                                            self.__property_changed_listener = None
                                            self.__remove_region_listener.close()
                                            self.__remove_region_listener = None
                                        @property
                                        def value(self):
                                            return Graphics.new_api_graphic("~1.0", self.__object)
                                    if graphic:
                                        return BoundGraphic(display, graphic)
        return Symbolic.ComputationVariable.resolve_extension_object_specifier(specifier)

    class DataItemReference:
        def __init__(self, document_model: "DocumentModel", key: str, data_item: DataItem.DataItem=None):
            self.__document_model = document_model
            self.__key = key
            self.__data_item = data_item
            self.mutex = threading.RLock()
            self.data_item_changed_event = Event.Event()

        # this method gets called directly from the document model
        def data_item_inserted(self, data_item):
            pass

        # this method gets called directly from the document model
        def data_item_removed(self, data_item):
            with self.mutex:
                if data_item == self.__data_item:
                    self.__data_item = None

        @property
        def data_item(self):
            with self.mutex:
                return self.__data_item

        @data_item.setter
        def data_item(self, value):
            with self.mutex:
                if self.__data_item != value:
                    self.__data_item = value
                    self.data_item_changed_event.fire()
                    self.__document_model._update_data_item_reference(self.__key, self.__data_item)

        def update_data(self, data_and_metadata, sub_area):
            # thread safe
            self.__document_model.queue_data_item_update(self.data_item, data_and_metadata, sub_area)

    def queue_data_item_update(self, data_item, data_and_metadata, sub_area):
        if data_item:
            with self.__pending_data_item_updates_lock:
                # TODO: optimize case where sub_area is None
                self.__pending_data_item_updates.append((data_item, data_and_metadata, sub_area))
            self.perform_data_item_updates_event.fire_any()

    def perform_data_item_updates(self):
        assert threading.current_thread() == threading.main_thread()
        with self.__pending_data_item_updates_lock:
            pending_data_item_updates = copy.copy(self.__pending_data_item_updates)
            self.__pending_data_item_updates = list()
        for data_item, data_and_metadata, sub_area in pending_data_item_updates:
            data_item.update_data_and_metadata(data_and_metadata, sub_area)

    def _update_data_item_reference(self, key: str, data_item: DataItem.DataItem) -> None:
        data_item_references_dict = copy.deepcopy(self._get_persistent_property_value("data_item_references"))
        if data_item:
            data_item_references_dict[key] = str(data_item.uuid)
        else:
            del data_item_references_dict[key]
        self._set_persistent_property_value("data_item_references", data_item_references_dict)

    def make_data_item_reference_key(self, *components) -> str:
        return "_".join([str(component) for component in list(components) if component is not None])

    def get_data_item_reference(self, key) -> "DocumentModel.DataItemReference":
        # this is implemented this way to avoid creating a data item reference unless it is missing.
        data_item_reference = self.__data_item_references.get(key)
        if data_item_reference:
            return data_item_reference
        return self.__data_item_references.setdefault(key, DocumentModel.DataItemReference(self, key))

    def setup_channel(self, data_item_reference_key: str, data_item: DataItem.DataItem) -> None:
        data_item_reference = self.get_data_item_reference(data_item_reference_key)
        data_item_reference.data_item = data_item

    def __construct_data_item_reference(self, hardware_source: HardwareSource.HardwareSource, data_channel: HardwareSource.DataChannel, append_data_item_fn):
        """Construct a data item reference.

        Construct a data item reference and assign a data item to it. Update data item session id and session metadata.
        Also connect the data channel processor.
        """
        session_id = self.session_id
        data_item_reference = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, data_channel.channel_id))
        with data_item_reference.mutex:
            data_item = data_item_reference.data_item
            # if we still don't have a data item, create it.
            if not data_item:
                data_item = DataItem.DataItem()
                data_item.title = "%s (%s)" % (hardware_source.display_name, data_channel.name) if data_channel.name else hardware_source.display_name
                data_item.category = "temporary"
                buffered_data_source = DataItem.BufferedDataSource()
                data_item.append_data_source(buffered_data_source)
                data_item_reference.data_item = data_item
                data_item.increment_data_ref_counts()
                self.begin_data_item_transaction(data_item)
                self.begin_data_item_live(data_item)
                append_data_item_fn(data_item)
            # update the session, but only if necessary (this is an optimization to prevent unnecessary display updates)
            if data_item.session_id != session_id:
                data_item.session_id = session_id
            session_metadata = self.session_metadata
            if data_item.session_metadata != session_metadata:
                data_item.session_metadata = session_metadata
            if data_channel.processor:
                src_data_channel = hardware_source.data_channels[data_channel.src_channel_index]
                src_data_item = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, src_data_channel.channel_id)).data_item
                data_channel.processor.connect(src_data_item)
            return data_item_reference

    def __data_channel_start(self, hardware_source, data_channel):
        data_item = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, data_channel.channel_id)).data_item
        if data_item:
            data_item.increment_data_ref_counts()
            self.begin_data_item_transaction(data_item)
            self.begin_data_item_live(data_item)

    def __data_channel_stop(self, hardware_source, data_channel):
        data_item = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, data_channel.channel_id)).data_item
        # the order of these two statements is important, at least for now (12/2013)
        # when the transaction ends, the data will get written to disk, so we need to
        # make sure it's still in memory. if decrement were to come before the end
        # of the transaction, the data would be unloaded from memory, losing it forever.
        if data_item:
            self.end_data_item_transaction(data_item)
            self.end_data_item_live(data_item)
            data_item.decrement_data_ref_counts()

    def __data_channel_updated(self, hardware_source, data_channel, append_data_item_fn, data_and_metadata):
        data_item_reference = self.__construct_data_item_reference(hardware_source, data_channel, append_data_item_fn)
        data_item_reference.update_data(data_and_metadata, data_channel.sub_area)

    def __data_channel_states_updated(self, hardware_source, data_channels):
        data_item_states = list()
        for data_channel in data_channels:
            data_item_reference = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, data_channel.channel_id))
            data_item = data_item_reference.data_item
            channel_id = data_channel.channel_id
            channel_data_state = data_channel.state
            sub_area = data_channel.sub_area
            # make sure to send out the complete frame
            data_item_state = dict()
            if channel_id is not None:
                data_item_state["channel_id"] = channel_id
            data_item_state["data_item"] = data_item
            data_item_state["channel_state"] = channel_data_state
            if sub_area:
                data_item_state["sub_area"] = sub_area
            data_item_states.append(data_item_state)
        # temporary until things get cleaned up
        hardware_source.data_item_states_changed_event.fire(data_item_states)
        hardware_source.data_item_states_changed(data_item_states)

    def __hardware_source_added(self, append_data_item_fn, hardware_source: HardwareSource.HardwareSource) -> None:
        self.__data_channel_states_updated_listeners[hardware_source.hardware_source_id] = hardware_source.data_channel_states_updated.listen(functools.partial(self.__data_channel_states_updated, hardware_source))
        for data_channel in hardware_source.data_channels:
            data_channel_updated_listener = data_channel.data_channel_updated_event.listen(functools.partial(self.__data_channel_updated, hardware_source, data_channel, append_data_item_fn))
            self.__data_channel_updated_listeners.setdefault(hardware_source.hardware_source_id, list()).append(data_channel_updated_listener)
            data_channel_start_listener = data_channel.data_channel_start_event.listen(functools.partial(self.__data_channel_start, hardware_source, data_channel))
            self.__data_channel_start_listeners.setdefault(hardware_source.hardware_source_id, list()).append(data_channel_start_listener)
            data_channel_stop_listener = data_channel.data_channel_stop_event.listen(functools.partial(self.__data_channel_stop, hardware_source, data_channel))
            self.__data_channel_stop_listeners.setdefault(hardware_source.hardware_source_id, list()).append(data_channel_stop_listener)
            data_item_reference = self.get_data_item_reference(self.make_data_item_reference_key(hardware_source.hardware_source_id, data_channel.channel_id))
            data_item = data_item_reference.data_item
            if data_item:
                hardware_source.clean_data_item(data_item, data_channel)

    def __hardware_source_removed(self, hardware_source):
        self.__data_channel_states_updated_listeners[hardware_source.hardware_source_id].close()
        del self.__data_channel_states_updated_listeners[hardware_source.hardware_source_id]
        for listener in self.__data_channel_updated_listeners.get(hardware_source.hardware_source_id, list()):
            listener.close()
        for listener in self.__data_channel_start_listeners.get(hardware_source.hardware_source_id, list()):
            listener.close()
        for listener in self.__data_channel_stop_listeners.get(hardware_source.hardware_source_id, list()):
            listener.close()
        self.__data_channel_updated_listeners.pop(hardware_source.hardware_source_id, None)
        self.__data_channel_start_listeners.pop(hardware_source.hardware_source_id, None)
        self.__data_channel_stop_listeners.pop(hardware_source.hardware_source_id, None)

    def get_snapshot_new(self, data_item: DataItem.DataItem) -> DataItem.DataItem:
        assert isinstance(data_item, DataItem.DataItem)
        data_item_copy = data_item.snapshot()
        data_item_copy.title = _("Snapshot of ") + data_item.title
        self.append_data_item(data_item_copy)
        return data_item_copy

    def make_data_item_with_computation(self, processing_id: str, inputs: typing.List[typing.Tuple[DataItem.DataItem, Graphics.Graphic]], region_list_map: typing.Mapping[str, typing.List[Graphics.Graphic]]=None) -> DataItem.DataItem:
        return self.__make_computation(processing_id, inputs, region_list_map)

    def __make_computation(self, processing_id: str, inputs: typing.List[typing.Tuple[DataItem.DataItem, Graphics.Graphic]], region_list_map: typing.Mapping[str, typing.List[Graphics.Graphic]]=None) -> DataItem.DataItem:
        """Create a new data item with computation specified by processing_id, inputs, and region_list_map.
        """
        region_list_map = region_list_map or dict()

        processing_descriptions = self._processing_descriptions
        processing_description = processing_descriptions[processing_id]

        # first process the sources in the description. match them to the inputs (which are data item/crop graphic tuples)
        src_dicts = processing_description.get("sources", list())
        assert len(inputs) == len(src_dicts)
        src_names = list()
        src_texts = list()
        src_labels = list()
        crop_names = list()
        regions = list()
        region_map = dict()
        for i, (src_dict, input) in enumerate(zip(src_dicts, inputs)):

            display_specifier = DataItem.DisplaySpecifier.from_data_item(input[0])
            buffered_data_source = display_specifier.buffered_data_source
            display = display_specifier.display

            if not buffered_data_source:
                return None

            # each source can have a list of requirements, check through them
            requirements = src_dict.get("requirements", list())
            for requirement in requirements:
                requirement_type = requirement["type"]
                if requirement_type == "dimensionality":
                    min_dimension = requirement.get("min")
                    max_dimension = requirement.get("max")
                    dimensionality = len(buffered_data_source.dimensional_shape) if buffered_data_source else 0
                    if min_dimension is not None and dimensionality < min_dimension:
                        return None
                    if max_dimension is not None and dimensionality > max_dimension:
                        return None

            suffix = i if len(src_dicts) > 1 else ""
            src_name = src_dict["name"]
            src_label = src_dict["label"]
            use_display_data = src_dict.get("use_display_data", True)
            src_text = "{}.{}".format(src_name, "display_xdata" if use_display_data else "xdata")
            crop_region = input[1] if src_dict.get("croppable", False) else None
            crop_name = "crop_region{}".format(suffix) if crop_region else None
            src_text = src_text if not crop_region else "xd.crop({}, {}.bounds)".format(src_text, crop_name)
            src_names.append(src_name)
            src_texts.append(src_text)
            src_labels.append(src_label)
            crop_names.append(crop_name)

            # each source can have a list of regions to be matched to arguments or created on the source
            region_dict_list = src_dict.get("regions", list())
            src_region_list = region_list_map.get(src_name, list())
            assert len(region_dict_list) == len(src_region_list)
            for region_dict, region in zip(region_dict_list, src_region_list):
                region_params = region_dict.get("params", dict())
                region_type = region_dict["type"]
                region_name = region_dict["name"]
                region_label = region_params.get("label")
                if region_type == "point":
                    if region:
                        assert isinstance(region, Graphics.PointGraphic)
                        point_region = region
                    else:
                        point_region = Graphics.PointGraphic()
                        for k, v in region_params.items():
                            setattr(point_region, k, v)
                        display.add_graphic(point_region)
                    regions.append((region_name, point_region, region_label))
                    region_map[region_name] = point_region
                elif region_type == "line":
                    if region:
                        assert isinstance(region, Graphics.LineProfileGraphic)
                        line_region = region
                    else:
                        line_region = Graphics.LineProfileGraphic()
                        line_region.start = 0.25, 0.25
                        line_region.end = 0.75, 0.75
                        for k, v in region_params.items():
                            setattr(line_region, k, v)
                        display.add_graphic(line_region)
                    regions.append((region_name, line_region, region_params.get("label")))
                    region_map[region_name] = line_region
                elif region_type == "rectangle":
                    if region:
                        assert isinstance(region, Graphics.RectangleGraphic)
                        rect_region = region
                    else:
                        rect_region = Graphics.RectangleGraphic()
                        rect_region.center = 0.5, 0.5
                        rect_region.size = 0.5, 0.5
                        for k, v in region_params.items():
                            setattr(rect_region, k, v)
                        display.add_graphic(rect_region)
                    regions.append((region_name, rect_region, region_params.get("label")))
                    region_map[region_name] = rect_region
                elif region_type == "spot":
                    if region:
                        assert isinstance(region, Graphics.SpotGraphic)
                        spot_region = region
                    else:
                        spot_region = Graphics.SpotGraphic()
                        spot_region.center = 0.25, 0.75
                        spot_region.size = 0.1, 0.1
                        for k, v in region_params.items():
                            setattr(spot_region, k, v)
                        display.add_graphic(spot_region)
                    regions.append((region_name, spot_region, region_params.get("label")))
                    region_map[region_name] = spot_region
                elif region_type == "interval":
                    if region:
                        assert isinstance(region, Graphics.IntervalGraphic)
                        interval_region = region
                    else:
                        interval_region = Graphics.IntervalGraphic()
                        for k, v in region_params.items():
                            setattr(interval_region, k, v)
                        display.add_graphic(interval_region)
                    regions.append((region_name, interval_region, region_params.get("label")))
                    region_map[region_name] = interval_region
                elif region_type == "channel":
                    if region:
                        assert isinstance(region, Graphics.ChannelGraphic)
                        channel_region = region
                    else:
                        channel_region = Graphics.ChannelGraphic()
                        for k, v in region_params.items():
                            setattr(channel_region, k, v)
                        display.add_graphic(channel_region)
                    regions.append((region_name, channel_region, region_params.get("label")))
                    region_map[region_name] = channel_region

        # now extract the script (full script) or expression (implied imports and return statement)
        script = processing_description.get("script")
        if not script:
            expression = processing_description.get("expression")
            if expression:
                script = Symbolic.xdata_expression(expression)
        assert script

        # construct the computation
        script = script.format(**dict(zip(src_names, src_texts)))
        computation = self.create_computation(script)
        computation.label = processing_description["title"]
        computation.processing_id = processing_id
        # process the data item inputs
        for src_name, src_label, input in zip(src_names, src_labels, inputs):
            display_specifier = DataItem.DisplaySpecifier.from_data_item(input[0])
            computation.create_object(src_name, self.get_object_specifier(display_specifier.data_item), label=src_label, cascade_delete=True)
        # next process the crop regions
        for crop_name, input in zip(crop_names, inputs):
            if crop_name:
                assert input[1] is not None
                computation.create_object(crop_name, self.get_object_specifier(input[1]), label=_("Crop Region"), cascade_delete=True)
        # process the regions
        for region_name, region, region_label in regions:
            computation.create_object(region_name, self.get_object_specifier(region), label=region_label, cascade_delete=True)
        # next process the parameters
        for param_dict in processing_description.get("parameters", list()):
            computation.create_variable(param_dict["name"], param_dict["type"], param_dict["value"], value_default=param_dict.get("value_default"),
                                        value_min=param_dict.get("value_min"), value_max=param_dict.get("value_max"),
                                        control_type=param_dict.get("control_type"), label=param_dict["label"])

        data_item0 = inputs[0][0]
        new_data_item = DataItem.new_data_item()
        prefix = "{} of ".format(processing_description["title"])
        new_data_item.title = prefix + data_item0.title
        new_data_item.category = data_item0.category

        self.append_data_item(new_data_item)

        display_specifier = DataItem.DisplaySpecifier.from_data_item(new_data_item)
        buffered_data_source = display_specifier.buffered_data_source
        display = display_specifier.display

        # next come the output regions that get created on the target itself
        new_regions = dict()
        for out_region_dict in processing_description.get("out_regions", list()):
            region_type = out_region_dict["type"]
            region_name = out_region_dict["name"]
            region_params = out_region_dict.get("params", dict())
            if region_type == "interval":
                interval_region = Graphics.IntervalGraphic()
                for k, v in region_params.items():
                    setattr(interval_region, k, v)
                display.add_graphic(interval_region)
                new_regions[region_name] = interval_region

        # now come the connections between the source and target
        for connection_dict in processing_description.get("connections", list()):
            connection_type = connection_dict["type"]
            connection_src = connection_dict["src"]
            connection_src_prop = connection_dict.get("src_prop")
            connection_dst = connection_dict["dst"]
            connection_dst_prop = connection_dict.get("dst_prop")
            if connection_type == "property":
                if connection_src == "display":
                    # TODO: how to refer to the buffered_data_sources? hardcode to data_item0 for now.
                    new_data_item.add_connection(Connection.PropertyConnection(data_item0.data_sources[0].displays[0], connection_src_prop, new_regions[connection_dst], connection_dst_prop))
            elif connection_type == "interval_list":
                new_data_item.add_connection(Connection.IntervalListConnection(display, region_map[connection_dst]))

        # save setting the computation until last to work around threaded clone/merge operation bug.
        # the bug is that setting the computation triggers the recompute to occur on a thread.
        # the recompute clones the data item and runs the operation. meanwhile this thread
        # updates the connection. now the recompute finishes and merges back the data item
        # which was cloned before the connection was established, effectively reversing the
        # update that matched the graphic interval to the slice interval on the display.
        # the result is that the slice interval on the display would get set to the default
        # value of the graphic interval. so don't actually update the computation until after
        # everything is configured. permanent solution would be to improve the clone/merge to
        # only update data that had been changed. alternative implementation would only track
        # changes to the data item and then apply them again to the original during merge.
        buffered_data_source.set_computation(computation)

        return new_data_item

    def update_computation(self, computation: Symbolic.Computation) -> None:
        if computation:
            processing_descriptions = self._processing_descriptions
            processing_id = computation.processing_id
            processing_description = processing_descriptions.get(processing_id)
            if processing_description:
                src_names = list()
                src_texts = list()
                source_dicts = processing_description["sources"]
                for i, source_dict in enumerate(source_dicts):
                    src_names.append(source_dict["name"])
                    data_expression = source_dict["name"] + (".display_xdata" if source_dict.get("use_display_data", True) else ".xdata")
                    if source_dict.get("croppable", False):
                        crop_region_variable_name = "crop_region" + "" if len(source_dicts) == 1 else str(i)
                        if computation._has_variable(crop_region_variable_name):
                            data_expression = "xd.crop(" + data_expression + ", " + crop_region_variable_name + ".bounds)"
                    src_texts.append(data_expression)
                script = processing_description.get("script")
                if not script:
                    expression = processing_description.get("expression")
                    if expression:
                        script = Symbolic.xdata_expression(expression)
                script = script.format(**dict(zip(src_names, src_texts)))
                computation._get_persistent_property("original_expression").value = script

    _processing_descriptions = dict()
    _builtin_processing_descriptions = None

    @classmethod
    def register_processing_descriptions(cls, processing_descriptions: typing.Dict) -> None:
        assert len(set(cls._processing_descriptions.keys()).intersection(set(processing_descriptions.keys()))) == 0
        cls._processing_descriptions.update(processing_descriptions)

    @classmethod
    def unregister_processing_descriptions(cls, processing_ids: typing.Sequence[str]):
        assert len(set(cls.__get_builtin_processing_descriptions().keys()).intersection(set(processing_ids))) == len(processing_ids)
        for processing_id in processing_ids:
            cls._processing_descriptions.pop(processing_id)

    @classmethod
    def _get_builtin_processing_descriptions(cls) -> typing.Dict:
        if not cls._builtin_processing_descriptions:
            vs = dict()
            vs["fft"] = {"title": _("FFT"), "expression": "xd.fft({src})", "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            vs["inverse-fft"] = {"title": _("Inverse FFT"), "expression": "xd.ifft({src})",
                "sources": [{"name": "src", "label": _("Source"), "use_display_data": False}]}
            vs["auto-correlate"] = {"title": _("Auto Correlate"), "expression": "xd.autocorrelate({src})",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            vs["cross-correlate"] = {"title": _("Cross Correlate"), "expression": "xd.crosscorrelate({src1}, {src2})",
                "sources": [{"name": "src1", "label": _("Source 1"), "croppable": True}, {"name": "src2", "label": _("Source 2"), "croppable": True}]}
            vs["sobel"] = {"title": _("Sobel"), "expression": "xd.sobel({src})",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            vs["laplace"] = {"title": _("Laplace"), "expression": "xd.laplace({src})",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            sigma_param = {"name": "sigma", "label": _("Sigma"), "type": "real", "value": 3, "value_default": 3, "value_min": 0, "value_max": 100,
                "control_type": "slider"}
            vs["gaussian-blur"] = {"title": _("Gaussian Blur"), "expression": "xd.gaussian_blur({src}, sigma)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [sigma_param]}
            filter_size_param = {"name": "filter_size", "label": _("Size"), "type": "integral", "value": 3, "value_default": 3, "value_min": 1, "value_max": 100}
            vs["median-filter"] = {"title": _("Median Filter"), "expression": "xd.median_filter({src}, filter_size)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [filter_size_param]}
            vs["uniform-filter"] = {"title": _("Uniform Filter"), "expression": "xd.uniform_filter({src}, filter_size)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [filter_size_param]}
            do_transpose_param = {"name": "do_transpose", "label": _("Transpose"), "type": "boolean", "value": False, "value_default": False}
            do_flip_v_param = {"name": "do_flip_v", "label": _("Flip Vertical"), "type": "boolean", "value": False, "value_default": False}
            do_flip_h_param = {"name": "do_flip_h", "label": _("Flip Horizontal"), "type": "boolean", "value": False, "value_default": False}
            vs["transpose-flip"] = {"title": _("Transpose/Flip"), "expression": "xd.transpose_flip({src}, do_transpose, do_flip_v, do_flip_h)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [do_transpose_param, do_flip_v_param, do_flip_h_param]}
            width_param = {"name": "width", "label": _("Width"), "type": "integral", "value": 256, "value_default": 256, "value_min": 1}
            height_param = {"name": "height", "label": _("Height"), "type": "integral", "value": 256, "value_default": 256, "value_min": 1}
            vs["resample"] = {"title": _("Resample"), "expression": "xd.resample_image({src}, (height, width))",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [width_param, height_param]}
            bins_param = {"name": "bins", "label": _("Bins"), "type": "integral", "value": 256, "value_default": 256, "value_min": 2}
            vs["histogram"] = {"title": _("Histogram"), "expression": "xd.histogram({src}, bins)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}], "parameters": [bins_param]}
            vs["invert"] = {"title": _("Invert"), "expression": "xd.invert({src})",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            vs["convert-to-scalar"] = {"title": _("Scalar"), "expression": "{src}",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True}]}
            requirement_2d = {"type": "dimensionality", "min": 2, "max": 2}
            requirement_3d = {"type": "dimensionality", "min": 3, "max": 3}
            crop_in_region = {"name": "crop_region", "type": "rectangle", "params": {"label": _("Crop Region")}}
            vs["crop"] = {"title": _("Crop"), "expression": "xd.crop({src}, crop_region.bounds)",
                "sources": [{"name": "src", "label": _("Source"), "regions": [crop_in_region], "requirements": [requirement_2d]}]}
            vs["sum"] = {"title": _("Sum"), "expression": "xd.sum({src}, 0)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True, "use_display_data": False, "requirements": [requirement_2d]}]}
            slice_center_param = {"name": "center", "label": _("Center"), "type": "integral", "value": 0, "value_default": 0, "value_min": 0}
            slice_width_param = {"name": "width", "label": _("Width"), "type": "integral", "value": 1, "value_default": 1, "value_min": 1}
            vs["slice"] = {"title": _("Slice"), "expression": "xd.slice_sum({src}, center, width)",
                "sources": [{"name": "src", "label": _("Source"), "croppable": True, "use_display_data": False, "requirements": [requirement_3d]}],
                "parameters": [slice_center_param, slice_width_param]}
            pick_in_region = {"name": "pick_region", "type": "point", "params": {"label": _("Pick Point")}}
            pick_out_region = {"name": "interval_region", "type": "interval", "params": {"label": _("Display Slice")}}
            pick_connection = {"type": "property", "src": "display", "src_prop": "slice_interval", "dst": "interval_region", "dst_prop": "interval"}
            vs["pick-point"] = {"title": _("Pick"), "expression": "xd.pick({src}, pick_region.position)",
                "sources": [{"name": "src", "label": _("Source"), "use_display_data": False, "regions": [pick_in_region], "requirements": [requirement_3d]}],
                "out_regions": [pick_out_region], "connections": [pick_connection]}
            pick_sum_in_region = {"name": "region", "type": "rectangle", "params": {"label": _("Pick Region")}}
            pick_sum_out_region = {"name": "interval_region", "type": "interval", "params": {"label": _("Display Slice")}}
            pick_sum_connection = {"type": "property", "src": "display", "src_prop": "slice_interval", "dst": "interval_region", "dst_prop": "interval"}
            vs["pick-mask-sum"] = {"title": _("Pick Sum"), "expression": "xd.sum_region({src}, region.mask_xdata_with_shape({src}.data_shape[0:2]))",
                "sources": [{"name": "src", "label": _("Source"), "use_display_data": False, "regions": [pick_sum_in_region], "requirements": [requirement_3d]}],
                "out_regions": [pick_sum_out_region], "connections": [pick_sum_connection]}
            line_profile_in_region = {"name": "line_region", "type": "line", "params": {"label": _("Line Profile")}}
            line_profile_connection = {"type": "interval_list", "src": "data_source", "dst": "line_region"}
            vs["line-profile"] = {"title": _("Line Profile"), "expression": "xd.line_profile({src}, line_region.vector, line_region.width)",
                "sources": [{"name": "src", "label": _("Source"), "regions": [line_profile_in_region]}], "connections": [line_profile_connection]}
            filter_in_region = {"name": "region", "type": "spot"}
            vs["filter"] = {"title": _("Filter"), "expression": "xd.real(xd.ifft(xd.fourier_mask({src}, region.mask_xdata_with_shape({src}.data_shape[0:2]))))",
                "sources": [{"name": "src", "label": _("Source"), "regions": [filter_in_region], "requirements": [requirement_2d]}]}
            cls._builtin_processing_descriptions = vs
        return cls._builtin_processing_descriptions

    def get_fft_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("fft", [(data_item, crop_region)])

    def get_ifft_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("inverse-fft", [(data_item, crop_region)])

    def get_auto_correlate_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("auto-correlate", [(data_item, crop_region)])

    def get_cross_correlate_new(self, data_item1: DataItem.DataItem, data_item2: DataItem.DataItem, crop_region1: Graphics.RectangleTypeGraphic=None, crop_region2: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("cross-correlate", [(data_item1, crop_region1), (data_item2, crop_region2)])

    def get_sobel_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("sobel", [(data_item, crop_region)])

    def get_laplace_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("laplace", [(data_item, crop_region)])

    def get_gaussian_blur_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("gaussian-blur", [(data_item, crop_region)])

    def get_median_filter_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("median-filter", [(data_item, crop_region)])

    def get_uniform_filter_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("uniform-filter", [(data_item, crop_region)])

    def get_transpose_flip_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("transpose-flip", [(data_item, crop_region)])

    def get_resample_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("resample", [(data_item, crop_region)])

    def get_histogram_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("histogram", [(data_item, crop_region)])

    def get_invert_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("invert", [(data_item, crop_region)])

    def get_convert_to_scalar_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("convert-to-scalar", [(data_item, crop_region)])

    def get_crop_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("crop", [(data_item, crop_region)], {"src": [crop_region]})

    def get_projection_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("sum", [(data_item, crop_region)])

    def get_slice_sum_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("slice", [(data_item, crop_region)])

    def get_pick_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None, pick_region: Graphics.PointTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("pick-point", [(data_item, crop_region)], {"src": [pick_region]})

    def get_pick_region_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None, pick_region: Graphics.Graphic=None) -> DataItem.DataItem:
        return self.__make_computation("pick-mask-sum", [(data_item, crop_region)], {"src": [pick_region]})

    def get_line_profile_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None, line_region: Graphics.LineTypeGraphic=None) -> DataItem.DataItem:
        return self.__make_computation("line-profile", [(data_item, crop_region)], {"src": [line_region]})

    def get_fourier_filter_new(self, data_item: DataItem.DataItem, crop_region: Graphics.RectangleTypeGraphic=None, filter_region: Graphics.Graphic=None) -> DataItem.DataItem:
        return self.__make_computation("filter", [(data_item, crop_region)], {"src": [filter_region]})

DocumentModel.register_processing_descriptions(DocumentModel._get_builtin_processing_descriptions())
