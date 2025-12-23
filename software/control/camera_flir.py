import argparse
import cv2
import time
import numpy as np
import PySpin
from control._def import *
import re

from typing import Callable, Optional, Tuple, Sequence
import threading

import squid.logging
from squid.config import CameraConfig, CameraPixelFormat, CameraReadoutMode
from squid.abc import (
    AbstractCamera,
    CameraAcquisitionMode,
    CameraFrameFormat,
    CameraFrame,
    CameraGainRange,
    CameraError,
)
class ReadType:
    """
    Use the following constants to determine whether nodes are read
    as Value nodes or their individual types.
    """

    VALUE = (0,)
    INDIVIDUAL = 1


try:
    if CHOSEN_READ == "VALUE":
        CHOSEN_READ = ReadType.VALUE
    else:
        CHOSEN_READ = ReadType.INDIVIDUAL
except:
    CHOSEN_READ = ReadType.INDIVIDUAL


def get_value_node(node):
    """
    Retrieves and prints the display name and value of all node types as value nodes.
    A value node is a general node type that allows for the reading and writing of any node type as a string.

    :param node: Node to get information from.
    :type node: INode
    :param level: Depth to indent output.
    :return: node name and value, both strings
    :rtype: (str (node name),str (node value)
    """
    try:
        # Create value node
        node_value = PySpin.CValuePtr(node)

        # Retrieve display name
        #
        # *** NOTES ***
        # A node's 'display name' is generally more appropriate for output and
        # user interaction whereas its 'name' is what the camera understands.
        # Generally, its name is the same as its display name but without
        # spaces - for instance, the name of the node that houses a camera's
        # serial number is 'DeviceSerialNumber' while its display name is
        # 'Device Serial Number'.
        name = node_value.GetName()

        # Retrieve value of any node type as string
        #
        # *** NOTES ***
        # Because value nodes return any node type as a string, it can be much
        # easier to deal with nodes as value nodes rather than their actual
        # individual types.
        value = node_value.ToString()
        return (name, value)
    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_string_node(node):
    """
    Retrieves the display name and value of a string node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str,str)
    """
    try:
        # Create string node
        node_string = PySpin.CStringPtr(node)

        # Retrieve string node value
        #
        # *** NOTES ***
        # Functions in Spinnaker C++ that use gcstring types
        # are substituted with Python strings in PySpin.
        # The only exception is shown in the DeviceEvents example, where
        # the callback function still uses a wrapped gcstring type.
        name = node_string.GetName()

        # Ensure that the value length is not excessive for printing
        value = node_string.GetValue()

        # Print value; 'level' determines the indentation level of output
        return (name, value)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_integer_node(node):
    """
    Retrieves and prints the display name and value of an integer node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, int)
    """
    try:
        # Create integer node
        node_integer = PySpin.CIntegerPtr(node)

        # Get display name
        name = node_integer.GetName()

        # Retrieve integer node value
        #
        # *** NOTES ***
        # All node types except base nodes have a ToString()
        # method which returns a value as a string.
        value = node_integer.GetValue()

        # Print value
        return (name, value)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_float_node(node):
    """
    Retrieves the name and value of a float node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, float)
    """
    try:

        # Create float node
        node_float = PySpin.CFloatPtr(node)

        # Get display name
        name = node_float.GetName()

        # Retrieve float value
        value = node_float.GetValue()

        # Print value
        return (name, value)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_boolean_node(node):
    """
    Retrieves the display name and value of a Boolean node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, bool)
    """
    try:
        # Create Boolean node
        node_boolean = PySpin.CBooleanPtr(node)

        # Get display name
        name = node_boolean.GetName()

        # Retrieve Boolean value
        value = node_boolean.GetValue()

        # Print Boolean value
        # NOTE: In Python a Boolean will be printed as "True" or "False".
        return (name, value)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_command_node(node):
    """
    This function retrieves the name and tooltip of a command
    The tooltip is printed below because command nodes do not have an intelligible
    value.

    :param node: Node to get information from.
    :type node: INode
    :return: node name and tooltip as a tuple
    :rtype: (str, str)
    """
    try:
        result = True

        # Create command node
        node_command = PySpin.CCommandPtr(node)

        # Get display name
        name = node_command.GetName()

        # Retrieve tooltip
        #
        # *** NOTES ***
        # All node types have a tooltip available. Tooltips provide useful
        # information about nodes. Command nodes do not have a method to
        # retrieve values as their is no intelligible value to retrieve.
        tooltip = node_command.GetToolTip()

        # Ensure that the value length is not excessive for printing

        # Print display name and tooltip
        return (name, tooltip)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_enumeration_node_and_current_entry(node):
    """
    This function retrieves and prints the display names of an enumeration node
    and its current entry (which is actually housed in another node unto itself).

    :param node: Node to get information from.
    :type node: INode
    :return: name and symbolic of current entry in enumeration
    :rtype: (str,str)
    """
    try:
        # Create enumeration node
        node_enumeration = PySpin.CEnumerationPtr(node)

        # Retrieve current entry as enumeration node
        #
        # *** NOTES ***
        # Enumeration nodes have three methods to differentiate between: first,
        # GetIntValue() returns the integer value of the current entry node;
        # second, GetCurrentEntry() returns the entry node itself; and third,
        # ToString() returns the symbolic of the current entry.
        node_enum_entry = PySpin.CEnumEntryPtr(node_enumeration.GetCurrentEntry())

        # Get display name
        name = node_enumeration.GetName()

        # Retrieve current symbolic
        #
        # *** NOTES ***
        # Rather than retrieving the current entry node and then retrieving its
        # symbolic, this could have been taken care of in one step by using the
        # enumeration node's ToString() method.
        entry_symbolic = node_enum_entry.GetSymbolic()

        # Print current entry symbolic
        return (name, entry_symbolic)

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)
        return ("", None)


def get_category_node_and_all_features(node):
    """
    This function retrieves and prints out the display name of a category node
    before printing all child nodes. Child nodes that are also category nodes
    are also retrieved recursively

    :param node: Category node to get information from.
    :type node: INode
    :return: Dictionary of category node features
    :rtype: dict
    """
    return_dict = {}
    try:
        # Create category node
        node_category = PySpin.CCategoryPtr(node)

        # Get and print display name
        # Retrieve and iterate through all children
        #
        # *** NOTES ***
        # The two nodes that typically have children are category nodes and
        # enumeration nodes. Throughout the examples, the children of category nodes
        # are referred to as features while the children of enumeration nodes are
        # referred to as entries. Keep in mind that enumeration nodes can be cast as
        # category nodes, but category nodes cannot be cast as enumerations.
        for node_feature in node_category.GetFeatures():

            # Ensure node is readable
            if not PySpin.IsReadable(node_feature):
                continue

            # Category nodes must be dealt with separately in order to retrieve subnodes recursively.
            if node_feature.GetPrincipalInterfaceType() == PySpin.intfICategory:
                return_dict[PySpin.CCategoryPtr(node_feature).GetName()] = get_category_node_and_all_features(
                    node_feature
                )

            # Cast all non-category nodes as value nodes
            #
            # *** NOTES ***
            # If dealing with a variety of node types and their values, it may be
            # simpler to cast them as value nodes rather than as their individual types.
            # However, with this increased ease-of-use, functionality is sacrificed.
            elif CHOSEN_READ == ReadType.VALUE:
                node_name, node_value = get_value_node(node_feature)
                return_dict[node_name] = node_value

            # Cast all non-category nodes as actual types
            elif CHOSEN_READ == ReadType.INDIVIDUAL:
                node_name = ""
                node_value = None
                principal_interface_type = node_feature.GetPrincipalInterfaceType()
                if principal_interface_type == PySpin.intfIString:
                    node_name, node_value = get_string_node(node_feature)
                elif principal_interface_type == PySpin.intfIInteger:
                    node_name, node_value = get_integer_node(node_feature)
                elif principal_interface_type == PySpin.intfIFloat:
                    node_name, node_value = get_float_node(node_feature)
                elif principal_interface_type == PySpin.intfIBoolean:
                    node_name, node_value = get_boolean_node(node_feature)
                elif principal_interface_type == PySpin.intfICommand:
                    node_name, node_value = get_command_node(node_feature)
                elif principal_interface_type == PySpin.intfIEnumeration:
                    node_name, node_value = get_enumeration_node_and_current_entry(node_feature)
                return_dict[node_name] = node_value

    except PySpin.SpinnakerException as ex:
        print("Error: %s" % ex)

    return return_dict

def get_node_ptr(node):
    if CHOSEN_READ == ReadType.VALUE:
        return PySpin.CValuePtr(node), None
    elif CHOSEN_READ == ReadType.INDIVIDUAL:
        principal_interface_type = node.GetPrincipalInterfaceType()
        if principal_interface_type == PySpin.intfIString:
            ptr = PySpin.CStringPtr(node)
        elif principal_interface_type == PySpin.intfIInteger:
            ptr = PySpin.CIntegerPtr(node)
        elif principal_interface_type == PySpin.intfIFloat:
            ptr = PySpin.CFloatPtr(node)
        elif principal_interface_type == PySpin.intfIBoolean:
            ptr = PySpin.CBooleanPtr(node)
        elif principal_interface_type == PySpin.intfICommand:
            ptr = PySpin.CCommandPtr(node)
        elif principal_interface_type == PySpin.intfIEnumeration:
            ptr = PySpin.CEnumerationPtr(node)
        else:
            raise ValueError(f"Unknown node type: {principal_interface_type}")
        return ptr, principal_interface_type

def get_device_info(cam):
    nodemap_tldevice = cam.GetTLDeviceNodeMap()
    device_info_dict = {}
    device_info_dict["TLDevice"] = get_category_node_and_all_features(nodemap_tldevice.GetNode("Root"))
    return device_info_dict


def get_device_info_full(cam, get_genicam=False):
    device_info_dict = {}
    nodemap_gentl = cam.GetTLDeviceNodeMap()
    device_info_dict["TLDevice"] = get_category_node_and_all_features(nodemap_gentl.GetNode("Root"))

    nodemap_tlstream = cam.GetTLStreamNodeMap()
    device_info_dict["TLStream"] = get_category_node_and_all_features(nodemap_tlstream.GetNode("Root"))
    if get_genicam:
        cam.Init()

        nodemap_applayer = cam.GetNodeMap()
        device_info_dict["GenICam"] = get_category_node_and_all_features(nodemap_applayer.GetNode("Root"))

        cam.DeInit()
    return device_info_dict


def retrieve_all_camera_info(get_genicam=False):
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    device_num = cam_list.GetSize()
    return_list = []
    if device_num > 0:
        for i, cam in enumerate(cam_list):
            return_list.append(get_device_info_full(cam, get_genicam=get_genicam))
        try:
            del cam
        except NameError:
            pass
    cam_list.Clear()
    system.ReleaseInstance()
    return return_list


def get_sn_by_model(model_name):
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    device_num = cam_list.GetSize()
    sn_to_return = None
    if device_num > 0:
        for i, cam in enumerate(cam_list):
            device_info = get_device_info(cam)
            try:
                if device_info["TLDevice"]["DeviceInformation"]["DeviceModelName"] == model_name:
                    sn_to_return = device_info["TLDevice"]["DeviceInformation"]["DeviceSerialNumber"]
                    break
            except KeyError:
                pass
        try:
            del cam
        except NameError:
            pass
    cam_list.Clear()
    system.ReleaseInstance()
    return sn_to_return

def set_enum_node(node, proposed_value):
    ptr, principal_interface_type = get_node_ptr(node)
    if principal_interface_type != PySpin.intfIEnumeration:
        raise ValueError(f"Node {node.GetName()} is not an enumeration node")
    node_enum = PySpin.CEnumerationPtr(node)
    valid_entry_names = [PySpin.CEnumEntryPtr(entry).GetSymbolic() for entry in node_enum.GetEntries()]
    if proposed_value not in valid_entry_names:
        raise ValueError(f"Proposed value {proposed_value} for node {ptr.GetName()} is not supported by this camera")
    if not PySpin.IsAvailable(ptr) or not PySpin.IsWritable(ptr):
        raise CameraError(f"Node {ptr.GetName()} is not available or writable")
    new_entry = node_enum.GetEntryByName(proposed_value)
    if not PySpin.IsAvailable(new_entry) or not PySpin.IsReadable(new_entry):
        raise ValueError(f"Proposed value {proposed_value} for node {ptr.GetName()} is not supported by this camera")
    ptr.SetIntValue(new_entry.GetValue())

class ImageEventHandler(PySpin.ImageEventHandler):
    def __init__(self, parent):
        super(ImageEventHandler, self).__init__()

        self.camera = parent  # Camera() type object

        self._processor = PySpin.ImageProcessor()
        self._processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

    def OnImageEvent(self, raw_image):

        if raw_image.IsIncomplete():
            print("Image incomplete with image status %i ..." % raw_image.GetImageStatus())
            return
        elif self._camera.is_color and "mono" not in self._camera.pixel_format.lower():
            if (
                "10" in self._camera.pixel_format
                or "12" in self._camera.pixel_format
                or "14" in self._camera.pixel_format
                or "16" in self._camera.pixel_format
            ):
                rgb_image = self._processor.Convert(raw_image, PySpin.PixelFormat_RGB16)
            else:
                rgb_image = self._processor.Convert(raw_image, PySpin.PixelFormat_RGB8)
            numpy_image = rgb_image.GetNDArray()
        else:
            if self._camera.convert_pixel_format:
                converted_image = self._processor.Convert(raw_image, self._camera.conversion_pixel_format)
                numpy_image = converted_image.GetNDArray()
                if self._camera.conversion_pixel_format == PySpin.PixelFormat_Mono12:
                    numpy_image = numpy_image << 4
            else:
                try:
                    numpy_image = raw_image.GetNDArray()
                except PySpin.SpinnakerException:
                    converted_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()
                if self._camera.pixel_format == "MONO12":
                    numpy_image = numpy_image << 4
        self._camera.current_frame = numpy_image
        self._camera.frame_ID_software = self._camera.frame_ID_software + 1
        self._camera.frame_ID = raw_image.GetFrameID()
        if self._camera.trigger_mode == TriggerMode.HARDWARE:
            if self._camera.frame_ID_offset_hardware_trigger == None:
                self._camera.frame_ID_offset_hardware_trigger = self._camera.frame_ID
            self._camera.frame_ID = self._camera.frame_ID - self._camera.frame_ID_offset_hardware_trigger
        self._camera.timestamp = time.time()
        self._camera.new_image_callback_external(self.camera)


class FLIRCamera(AbstractCamera):

    def __init__(self, camera_config: CameraConfig, hw_trigger_fn: Optional[Callable[[Optional[float]], bool]], hw_set_strobe_delay_ms_fn: Optional[Callable[[float], bool]]):
        """
        Initialize the FLIR camera.

        Args:
            camera_config: Camera configuration including pixel format, ROI, etc.
        """

        super().__init__(camera_config, hw_trigger_fn, hw_set_strobe_delay_ms_fn)

        # Threading for frame reading
        self._read_thread_lock = threading.Lock()
        self._read_thread: Optional[threading.Thread] = None
        self._read_thread_keep_running = threading.Event()
        self._read_thread_keep_running.clear()
        self._read_thread_wait_period_s = 1.0
        self._read_thread_running = threading.Event()
        self._read_thread_running.clear()

        # Frame management
        self._frame_lock = threading.Lock()
        self._current_frame: Optional[CameraFrame] = None
        self._last_trigger_timestamp = 0
        self._trigger_sent = threading.Event()
        self._is_streaming = threading.Event()
        
        # Fast acquisition support
        self._fast_acquisition_buffer = None  # Will be set by fast acquisition controller
        self._fast_acquisition_thread: Optional[threading.Thread] = None
        self._fast_acquisition_thread_keep_running = threading.Event()
        self._fast_acquisition_callback: Optional[Callable[[CameraFrame], None]] = None
        self.fast_acquisition_timeout_ms = None

        self.py_spin_system = PySpin.System.GetInstance()
        self.camera_list = self.py_spin_system.GetCameras()
        self._camera = None  # PySpin CameraPtr type

        self.device_info_dict = None
        self.device_index = 0
        self.callback_is_enabled = False
        self.is_color = None
        self._readout_mode = None
        self._exposure_time_ms = 20
        self._sensor_type = None
        self._pixel_format = None

        
        self.processor = PySpin.ImageProcessor()
        self.processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

        # many to be purged
        
        # self.camera = None  # PySpin CameraPtr type
        # self.is_color = None
        # self.gamma_lut = None
        # self.contrast_lut = None
        # self.color_correction_param = None

        # self.one_frame_post_processor = PySpin.ImageProcessor()
        # self.conversion_pixel_format = PySpin.PixelFormat_Mono8
        # self.convert_pixel_format = False
        # self.one_frame_post_processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

        # self.auto_exposure_mode = None
        # self.auto_gain_mode = None
        # self.auto_wb_mode = None
        # self.auto_wb_profile = None

        # self.rotate_image_angle = rotate_image_angle
        # self.flip_image = flip_image

        # self._exposure_time_ms = 1  # unit: ms
        # self.analog_gain = 0
        # self.frame_ID = -1
        # self.frame_ID_software = -1
        # self.frame_ID_offset_hardware_trigger = 0
        # self.timestamp = 0

        # self.image_locked = False
        # self.current_frame = None

        # self.GAIN_MAX = 24
        # self.GAIN_MIN = 0
        # self.GAIN_STEP = 1
        # self._exposure_time_ms_MS_MIN = 0.01
        # self._exposure_time_ms_MS_MAX = 4000

        # self.trigger_mode = None
        self.pixel_size_byte = 1

        # # below are values for IMX226 (MER2-1220-32U3M)
        if self._readout_mode == CameraReadoutMode.GLOBAL:
            self.row_period_us = 0
            self.exposure_delay_us_8bit = 100 # arbitrary value for global shutter
            self.exposure_delay_us = self.exposure_delay_us_8bit * self.pixel_size_byte
            self.strobe_delay_us = self.exposure_delay_us
        else:
            # Figure out how to standardize configuration of this later
            self.row_period_us = 10
            self.row_numbers = 3036
            self.exposure_delay_us_8bit = 650
            self.exposure_delay_us = self.exposure_delay_us_8bit * self.pixel_size_byte
            self.strobe_delay_us = self.exposure_delay_us + self.row_period_us * self.pixel_size_byte * (
                self.row_numbers - 1
            )

        # self.pixel_format = None  # use the default pixel format

        # self.is_live = False  # this determines whether a new frame received will be handled in the streamHandler

        self.image_event_handler = ImageEventHandler(self)
        # mainly for discarding the last frame received after stop_live() is called, where illumination is being turned off during exposure

    def open(self, index=0, is_color=None):
        if is_color is None:
            is_color = self.is_color
        try:
            self._camera.DeInit()
            del self._camera
        except AttributeError as e:
            print(f"Error: {e}")
            pass
        self._log.info(f"Opening camera with index {index} and is_color {is_color}")
        self.camera_list.Clear()
        self.camera_list = self.py_spin_system.GetCameras()
        device_num = self.camera_list.GetSize()
        if device_num == 0:
            raise RuntimeError("Could not find any USB camera devices!")
        if self._config.serial_number is None:
            self.device_index = index
            self._camera = self.camera_list.GetByIndex(index)
        else:
            self._camera = self.camera_list.GetBySerial(str(self._config.serial_number))

        self.device_info_dict = get_device_info_full(self._camera, get_genicam=True)

        self._camera.Init()
        self.nodemap = self._camera.GetNodeMap()

        self.is_color = is_color
        if self.is_color:
            self.set_wb_ratios(2, 1, 2)

        # Parse sensor type
        sensor_description = get_node_ptr(self.nodemap.GetNode("SensorDescription"))[0].GetValue()
        self._log.info(f"Sensor description: {sensor_description}")
        # Parse sensor type from sensor description - find patterns that match "IMX"
        self._sensor_type = re.search(r"IMX(\d+)", sensor_description).group(0)
        

        # set to highest possible framerate
        PySpin.CBooleanPtr(self.nodemap.GetNode("AcquisitionFrameRateEnable")).SetValue(True)
        target_rate = 1000
        for decrement in range(0, 1000):
            try:
                PySpin.CFloatPtr(self.nodemap.GetNode("AcquisitionFrameRate")).SetValue(target_rate - decrement)
                break
            except PySpin.SpinnakerException as ex:
                pass

        # turn off device throughput limit
        node_throughput_limit = PySpin.CIntegerPtr(self.nodemap.GetNode("DeviceLinkThroughputLimit"))
        node_throughput_limit.SetValue(node_throughput_limit.GetMax())

        self.Width = PySpin.CIntegerPtr(self.nodemap.GetNode("Width")).GetValue()
        self.Height = PySpin.CIntegerPtr(self.nodemap.GetNode("Height")).GetValue()

        self.WidthMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode("SensorWidth")).GetValue()
        self.HeightMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode("SensorHeight")).GetValue()

        self._log.info(f"Setting region of interest to {self._config.default_roi[0]}, {self._config.default_roi[1]}, {self._config.default_roi[2]}, {self._config.default_roi[3]}")
        self.set_region_of_interest(self._config.default_roi[0], self._config.default_roi[1], self._config.default_roi[2], self._config.default_roi[3])

        self.WidthMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode("WidthMax")).GetValue()
        self.HeightMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode("HeightMax")).GetValue()
        self.WidthMax = self.WidthMaxAbsolute
        self.HeightMax = self.HeightMaxAbsolute
        self.OffsetX = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetX")).GetValue()
        self.OffsetY = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetY")).GetValue()

        # disable gamma
        PySpin.CBooleanPtr(self.nodemap.GetNode("GammaEnable")).SetValue(False)

        # Configure trigger lines
        line_selector = self.nodemap.GetNode("LineSelector")
        set_enum_node(line_selector, "Line3") # line 3 is control trigger to camera
        line_mode = self.nodemap.GetNode("LineMode")
        set_enum_node(line_mode, "Input")


        

        set_enum_node(line_selector, "Line2") # line 2 sends out trigger from camera to other devices - use this as a frame counter
        line_mode = self.nodemap.GetNode("LineMode")
        set_enum_node(line_mode, "Output")
        line_source = self.nodemap.GetNode("LineSource")
        set_enum_node(line_source, "ExposureActive")
        v33_enable = PySpin.CBooleanPtr(self.nodemap.GetNode("V3_3Enable"))
        v33_enable.SetValue(False)


    def set_callback(self, function):
        self.new_image_callback_external = function

    def enable_callback(self):
        """Enable event handler callback (legacy method, callbacks now handled via _propogate_frame)."""
        if self.callback_is_enabled == False:
            # stop streaming
            if self._is_streaming.is_set():
                was_streaming = True
                self.stop_streaming()
            else:
                was_streaming = False
            # enable callback
            try:
                self._camera.RegisterEventHandler(self.image_event_handler)
                self.callback_is_enabled = True
            except PySpin.SpinnakerException as ex:
                self._log.warning(f"Error registering event handler: {ex}")
            # resume streaming if it was on
            if was_streaming:
                self.start_streaming()
            self.callback_is_enabled = True
        else:
            pass

    def disable_callback(self):
        """Disable event handler callback (legacy method, callbacks now handled via _propogate_frame)."""
        if self.callback_is_enabled == True:
            # stop streaming
            if self._is_streaming.is_set():
                was_streaming = True
                self.stop_streaming()
            else:
                was_streaming = False
            try:
                self._camera.UnregisterEventHandler(self.image_event_handler)
                self.callback_is_enabled = False
            except PySpin.SpinnakerException as ex:
                self._log.warning(f"Error unregistering event handler: {ex}")
            # resume streaming if it was on
            if was_streaming:
                self.start_streaming()
        else:
            pass

    def open_by_sn(self, sn, is_color=None):
        self._config.serial_number = sn
        self.open(is_color=is_color)

    def close(self):
        try:
            self._camera.DeInit()
            del self._camera
        except AttributeError:
            pass
        self._camera = None
        self.auto_gain_mode = None
        self.auto_exposure_mode = None
        self.auto_wb_mode = None
        self.auto_wb_profile = None
        self.device_info_dict = None
        self.is_color = None
        self.gamma_lut = None
        self.contrast_lut = None
        self.color_correction_param = None
        self.last_raw_image = None
        self.last_converted_image = None
        self.last_numpy_image = None
    
    def set_exposure_time(self, exposure_time_ms: float):
        """Set the exposure time in milliseconds."""
        if exposure_time_ms == self._exposure_time_ms:
            return
        self._set_exposure_time_imp(exposure_time_ms)

    def _set_exposure_time_imp(self, exposure_time_ms):  ## NOTE: Disables auto-exposure
        self.nodemap = self._camera.GetNodeMap()
        node_auto_exposure = self.nodemap.GetNode("ExposureAuto")
        set_enum_node(node_auto_exposure, "Off")

        readout_mode = self.get_readout_mode()
        use_strobe = (self.trigger_mode == TriggerMode.HARDWARE and readout_mode == CameraReadoutMode.ROLLING)  # true if using hardware trigger and rolling readout mode


        node_exposure_time = PySpin.CFloatPtr(self.nodemap.GetNode("ExposureTime"))
        if not PySpin.IsWritable(node_exposure_time):
            print("Unable to set exposure manually after disabling auto exposure")

        # FLIR cameras use microseconds for exposure time
        if not use_strobe:
            self._exposure_time_ms = exposure_time_ms
            node_exposure_time.SetValue(exposure_time_ms * 1000.0)
        else:
            # set the camera exposure time such that the active exposure time (illumination on time) is the desired value
            self._exposure_time_ms = exposure_time_ms
            # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
            camera_exposure_time = (
                self.exposure_delay_us
                + self._exposure_time_ms * 1000
                + self.row_period_us * self.pixel_size_byte * (self.row_numbers - 1)
                + 500
            )  # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
            node_exposure_time.SetValue(camera_exposure_time)

    def update_camera_exposure_time(self):
        self.set_exposure_time(self._exposure_time_ms)

    def set_analog_gain(self, analog_gain):  ## NOTE: Disables auto-gain
        self.nodemap = self._camera.GetNodeMap()

        node_auto_gain = PySpin.CEnumerationPtr(self.nodemap.GetNode("GainAuto"))
        node_auto_gain_off = PySpin.CEnumEntryPtr(node_auto_gain.GetEntryByName("Off"))
        if not PySpin.IsReadable(node_auto_gain_off) or not PySpin.IsWritable(node_auto_gain):
            print("Unable to set gain manually (cannot disable auto gain)")
            return

        if node_auto_gain.GetIntValue() != node_auto_gain_off.GetValue():
            self.auto_gain_mode = PySpin.CEnumEntryPtr(node_auto_gain.GetCurrentEntry()).GetValue()

        node_auto_gain.SetIntValue(node_auto_gain_off.GetValue())

        node_gain = PySpin.CFloatPtr(self.nodemap.GetNode("Gain"))

        if not PySpin.IsWritable(node_gain):
            print("Unable to set gain manually after disabling auto gain")
            return

        self.analog_gain = analog_gain
        node_gain.SetValue(analog_gain)

    def get_awb_ratios(self):  ## NOTE: Enables auto WB, defaults to continuous WB
        self.nodemap = self._camera.GetNodeMap()
        node_balance_white_auto = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAuto"))
        # node_balance_white_auto_options = [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_white_auto.GetEntries()]
        # print("WB Auto options: "+str(node_balance_white_auto_options))

        node_balance_ratio_select = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceRatioSelector"))
        # node_balance_ratio_select_options = [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_ratio_select.GetEntries()]
        # print("Balance Ratio Select options: "+str(node_balance_ratio_select_options))
        """
        node_balance_profile = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAutoProfile"))
        node_balance_profile_options= [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_profile.GetEntries()]
        print("WB Auto Profile options: "+str(node_balance_profile_options))
        """
        node_balance_white_auto_off = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntryByName("Off"))
        if not PySpin.IsReadable(node_balance_white_auto) or not PySpin.IsReadable(node_balance_white_auto_off):
            print("Unable to check if white balance is auto or not")

        elif (
            PySpin.IsWritable(node_balance_white_auto)
            and node_balance_white_auto.GetIntValue() == node_balance_white_auto_off.GetValue()
        ):
            if self.auto_wb_mode is not None:
                node_balance_white_auto.SetIntValue(self.auto_wb_mode)
            else:
                node_balance_white_continuous = PySpin.CEnumEntryPtr(
                    node_balance_white_auto.GetEntryByName("Continuous")
                )
                if PySpin.IsReadable(node_balance_white_continuous):
                    node_balance_white_auto.SetIntValue(node_balance_white_continuous.GetValue())
                else:
                    print("Cannot turn on auto white balance in continuous mode")
                    node_balance_white_once = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntry("Once"))
                    if PySpin.IsReadable(node_balance_white_once):
                        node_balance_white_auto.SetIntValue(node_balance_white_once.GetValue())
                    else:
                        print("Cannot turn on auto white balance in Once mode")
        else:
            print("Cannot turn on auto white balance, or auto white balance is already on")

        balance_ratio_red = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Red"))
        balance_ratio_green = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Green"))
        balance_ratio_blue = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Blue"))
        node_balance_ratio = PySpin.CFloatPtr(self.nodemap.GetNode("BalanceRatio"))
        if (
            not PySpin.IsWritable(node_balance_ratio_select)
            or not PySpin.IsReadable(balance_ratio_red)
            or not PySpin.IsReadable(balance_ratio_green)
            or not PySpin.IsReadable(balance_ratio_blue)
        ):
            print("Unable to move balance ratio selector")
            return (0, 0, 0)

        node_balance_ratio_select.SetIntValue(balance_ratio_red.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for red")
            awb_r = 0
        else:
            awb_r = node_balance_ratio.GetValue()

        node_balance_ratio_select.SetIntValue(balance_ratio_green.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for green")
            awb_g = 0
        else:
            awb_g = node_balance_ratio.GetValue()

        node_balance_ratio_select.SetIntValue(balance_ratio_blue.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for blue")
            awb_b = 0
        else:
            awb_b = node_balance_ratio.GetValue()

        return (awb_r, awb_g, awb_b)

    def set_wb_ratios(self, wb_r=None, wb_g=None, wb_b=None):  ## NOTE disables auto WB, stores extant
        ## auto WB mode if any
        self.nodemap = self._camera.GetNodeMap()
        node_balance_white_auto = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAuto"))
        node_balance_ratio_select = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceRatioSelector"))
        node_balance_white_auto_off = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntryByName("Off"))
        if not PySpin.IsReadable(node_balance_white_auto) or not PySpin.IsReadable(node_balance_white_auto_off):
            print("Unable to check if white balance is auto or not")
        elif node_balance_white_auto.GetIntValue() != node_balance_white_auto_off.GetValue():
            self.auto_wb_value = node_balance_white_auto.GetIntValue()
            if PySpin.IsWritable(node_balance_white_auto):
                node_balance_white_auto.SetIntValue(node_balance_white_auto_off.GetValue())
            else:
                print("Cannot turn off auto WB")

        balance_ratio_red = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Red"))
        balance_ratio_green = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Green"))
        balance_ratio_blue = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Blue"))
        node_balance_ratio = PySpin.CFloatPtr(self.nodemap.GetNode("BalanceRatio"))
        if (
            not PySpin.IsWritable(node_balance_ratio_select)
            or not PySpin.IsReadable(balance_ratio_red)
            or not PySpin.IsReadable(balance_ratio_green)
            or not PySpin.IsReadable(balance_ratio_blue)
        ):
            print("Unable to move balance ratio selector")
            return

        node_balance_ratio_select.SetIntValue(balance_ratio_red.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for red")
        else:
            if wb_r is not None:
                node_balance_ratio.SetValue(wb_r)

        node_balance_ratio_select.SetIntValue(balance_ratio_green.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for green")
        else:
            if wb_g is not None:
                node_balance_ratio.SetValue(wb_g)

        node_balance_ratio_select.SetIntValue(balance_ratio_blue.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for blue")
        else:
            if wb_b is not None:
                node_balance_ratio.SetValue(wb_b)

    def set_reverse_x(self, value):
        self.nodemap = self._camera.GetNodeMap()
        node_reverse_x = PySpin.CBooleanPtr(self.nodemap.GetNode("ReverseX"))
        if not PySpin.IsWritable(node_reverse_x):
            print("Can't write to reverse X node")
            return
        else:
            node_reverse_x.SetValue(bool(value))

    def set_reverse_y(self, value):
        self.nodemap = self._camera.GetNodeMap()
        node_reverse_y = PySpin.CBooleanPtr(self.nodemap.GetNode("ReverseY"))
        if not PySpin.IsWritable(node_reverse_y):
            print("Can't write to reverse Y node")
            return
        else:
            node_reverse_y.SetValue(bool(value))

    def start_streaming(self):
        if self._is_streaming.is_set():
            self._log.debug("Already streaming, start_streaming is noop")
            return

        try:
            if not self._camera.IsInitialized():
                self._camera.Init()
            self._camera.BeginAcquisition()
            self._ensure_read_thread_running()
            self._trigger_sent.clear()
            self._is_streaming.set()
            self._log.info(f"FLIR camera started streaming in mode: {self.get_acquisition_mode()}")
        except Exception as e:
            raise CameraError(f"Failed to start streaming: {e}")

    def stop_streaming(self):
        if not self._is_streaming.is_set():
            self._log.debug("Already stopped, stop_streaming is noop")
            return

        # try:
        self._cleanup_read_thread()
        self._trigger_sent.clear()
        self._is_streaming.clear()
        self._log.info("FLIR camera streaming stopped")
        # except Exception as e:
        #     raise CameraError(f"Failed to stop streaming: {e}")

    def _ensure_read_thread_running(self):
        """Start the frame reading thread if not already running."""
        with self._read_thread_lock:
            if self._read_thread is not None and self._read_thread_running.is_set():
                self._log.info("Read thread exists and is running.")
                return True

            elif self._read_thread is not None:
                self._log.warning("Read thread exists but not running. Attempting restart.")

            self._read_thread = threading.Thread(target=self._wait_for_frame, daemon=True)
            self._read_thread_keep_running.set()
            self._read_thread.start()

    def _cleanup_read_thread(self):
        """Stop and clean up the frame reading thread."""
        self._log.debug("Cleaning up read thread.")
        with self._read_thread_lock:
            if self._read_thread is None:
                self._log.warning("No read thread to clean up.")
                return True

            self._read_thread_keep_running.clear()

            try:
                # Abort acquisition to wake up GetNextImage if it's blocking
                if self._camera.IsStreaming():
                    self._camera.EndAcquisition()
            except Exception as e:
                self._log.warning(f"Failed to abort camera: {e}")

            self._read_thread.join(1.1 * self._read_thread_wait_period_s)

            if self._read_thread.is_alive():
                self._log.warning("Read thread refused to exit!")

            self._read_thread = None
            self._read_thread_running.clear()

    def _wait_for_frame(self):
        """Thread function to wait for and process frames."""
        self._log.info("Starting FLIR read thread.")
        self._read_thread_running.set()
        # self._log.info(f"Read thread keep running: {self._read_thread_keep_running.is_set()}")
        while self._read_thread_keep_running.is_set():
            # try:
                # Check if camera is still streaming
                if not self._camera.IsStreaming():
                    self._log.warning("Camera is not streaming, sleeping for 1ms")
                    time.sleep(0.001)
                    continue

                # Get next image with timeout
                try:
                    # Use exposure-time-based timeout for normal operation
                    timeout_ms = int(np.round(self._exposure_time_ms*1.1))
                    
                    raw_image = self._camera.GetNextImage(timeout_ms)
                except PySpin.SpinnakerException as e:
                    self._log.warning(f"Error getting next image: {e}, continuing...")
                    time.sleep((self._exposure_time_ms*1.1)/1000)
                    continue

                if raw_image is None:
                    time.sleep(0.001)
                    self._log.warning("Raw image is None, sleeping for 1ms")
                    continue

                if raw_image.IsIncomplete():
                    self._log.debug(f"Image incomplete with image status {raw_image.GetImageStatus()}")
                    raw_image.Release()
                    continue

                # Convert image to numpy array
                try:
                    if self.is_color:
                        pixel_format = self.get_pixel_format()
                        if pixel_format in [CameraPixelFormat.MONO10, CameraPixelFormat.MONO12, 
                                          CameraPixelFormat.MONO14, CameraPixelFormat.MONO16]:
                            rgb_image = self.processor.Convert(raw_image, PySpin.PixelFormat_RGB16)
                        else:
                            rgb_image = self.processor.Convert(raw_image, PySpin.PixelFormat_RGB8)
                        numpy_image = rgb_image.GetNDArray()
                    else:
                        numpy_image = raw_image.GetNDArray()
                        if self.get_pixel_format() == CameraPixelFormat.MONO12:
                            numpy_image = numpy_image << 4
                except PySpin.SpinnakerException as e:
                    self._log.warning(f"Error converting image: {e}, trying fallback")
                    converted_image = self.processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()

                raw_image.Release()

                # Process the raw frame
                processed_frame = self._process_raw_frame(numpy_image)

                # Create CameraFrame
                with self._frame_lock:
                    camera_frame = CameraFrame(
                        frame_id=self._current_frame.frame_id + 1 if self._current_frame else 1,
                        timestamp=time.time(),
                        frame=processed_frame,
                        frame_format=self.get_frame_format(),
                        frame_pixel_format=self.get_pixel_format(),
                    )
                    self._current_frame = camera_frame

                # Propagate frame to callbacks
                self._propogate_frame(camera_frame)
                self._trigger_sent.clear()

                time.sleep(0.001)

            # except PySpin.SpinnakerException as e:
            #     if self._read_thread_keep_running.is_set():
            #         self._log.debug(f"Exception in read loop: {e}, continuing...")
            #     time.sleep(0.001)
            # except Exception as e:
            #     if self._read_thread_keep_running.is_set():
            #         self._log.debug(f"Exception in read loop: {e}, continuing...")
            #     time.sleep(0.001)

        self._read_thread_running.clear()

    def start_fast_acquisition_frame_grabbing(self, frame_callback: Optional[Callable[[np.ndarray], None]] = None):
        """
        Start dedicated fast acquisition frame grabbing thread.
        
        This method should be called after:
        1. Setting camera to HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST mode
        2. Starting camera acquisition (BeginAcquisition)
        3. Before Firing DAQ waveforms
        
        The frame grabbing thread will continuously read frames from the camera
        using GetNextImage with minimal timeout for non-blocking operation.
        
        Args:
            frame_callback: Optional callback function to receive raw frame data (numpy array).
                           Frame IDs and timestamps will be determined from DAQ synchronization.
                           If None, frames are stored in self._current_frame only.
        """
        if self._is_streaming.is_set():
            self._log.warning("Camera is already streaming. Stop streaming before starting fast acquisition.")
            return
        # Only allow this for mono frames
        if self.is_color:
            raise CameraError("Fast acquisition is only supported for mono cameras")

        # Check that the acquisition mode is HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST
        acquisition_mode = self.get_acquisition_mode()
        self._log.info(f"Acquisition mode: {acquisition_mode}")
        if acquisition_mode not in [CameraAcquisitionMode.HARDWARE_TRIGGER, CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST]:
            raise CameraError("Fast acquisition is only supported for hardware triggering mode")
        
        if not self._camera.IsInitialized():
            self._camera.Init()
        
        # Start acquisition without starting the normal streaming thread
        try:
            self._camera.BeginAcquisition()
            self._log.info("Camera acquisition started for fast mode")
        except Exception as e:
            raise CameraError(f"Failed to start camera acquisition: {e}")
        
        # Start dedicated fast acquisition frame grabbing thread
        self._fast_acquisition_callback = frame_callback
        self._fast_acquisition_thread_keep_running = threading.Event()
        self._fast_acquisition_thread_keep_running.set()
        
        self._fast_acquisition_thread = threading.Thread(
            target=self._grab_frames_fast_acquisition,
            daemon=True
        )
        self._fast_acquisition_thread.start()
        self._log.info("Fast acquisition frame grabbing thread started")
    
    def stop_fast_acquisition_frame_grabbing(self):
        """Stop the fast acquisition frame grabbing thread and end camera acquisition."""
        if not hasattr(self, '_fast_acquisition_thread') or self._fast_acquisition_thread is None:
            return
        
        self._log.info("Stopping fast acquisition frame grabbing...")
        
        # Signal thread to stop
        self._fast_acquisition_thread_keep_running.clear()
        # Wait for thread to finish
        if self._fast_acquisition_thread.is_alive():
            # Abort acquisition to wake up GetNextImage if it's blocking
            try:
                if self._camera.IsStreaming():
                    self._camera.EndAcquisition()
                    self._log.info("Camera acquisition aborted")
            except Exception as e:
                self._log.warning(f"Failed to abort camera: {e}")
            
            self._fast_acquisition_thread.join(timeout=2.0)
            if self._fast_acquisition_thread.is_alive():
                self._log.warning("Fast acquisition thread refused to exit!")
        
        # End acquisition
        try:
            if self._camera.IsStreaming():
                self._camera.EndAcquisition()
        except Exception as e:
            self._log.warning(f"Failed to end acquisition: {e}")
        
        self._fast_acquisition_thread = None
        self._log.info("Fast acquisition frame grabbing stopped")
    
    def _grab_frames_fast_acquisition(self):
        """
        Dedicated thread function for fast acquisition frame grabbing.
        
        Uses GetNextImage with minimal timeout (1ms) for non-blocking operation.
        Frames are written directly to the provided callback or stored in _current_frame.
        """
        self._log.info("Starting fast acquisition frame grabbing thread.")
        
        while self._fast_acquisition_thread_keep_running.is_set():
            try:
                # Check if camera is still acquiring
                # if not self._camera.IsStreaming():
                #     time.sleep(0.001)
                #     continue
                
                # Get next image with minimal timeout for non-blocking operation
                try:
                    raw_image = self._camera.GetNextImage(self.fast_acquisition_timeout_ms)  # 1ms timeout
                except PySpin.SpinnakerException as e:
                    # Timeout is expected and normal - don't log it
                    # self._log.warning(f"GetNextImage error: {e}")
                    if "failed waiting" not in str(e).lower():
                        self._log.debug(f"GetNextImage error: {e}")
                    continue
                
                if raw_image is None:
                    continue
                
                if raw_image.IsIncomplete():
                    self._log.debug(f"Image incomplete with image status {raw_image.GetImageStatus()}")
                    raw_image.Release()
                    continue
                
                # Convert image to numpy array
                try:
                    numpy_image = raw_image.GetNDArray()
                    if self.get_pixel_format() == CameraPixelFormat.MONO12:
                        numpy_image = numpy_image << 4
                except PySpin.SpinnakerException as e:
                    self._log.warning(f"Error converting image: {e}, trying fallback")
                    converted_image = self.processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()
                
                raw_image.Release()
                
                # Process the raw frame
                processed_frame = self._process_raw_frame(numpy_image)
                # Call callback if provided (for fast acquisition buffer)
                # Pass only the raw frame data - IDs and timestamps come from DAQ
                if self._fast_acquisition_callback is not None:
                    try:
                        self._fast_acquisition_callback(processed_frame)
                    except Exception as e:
                        self._log.error(f"Error in fast acquisition callback: {e}")
            
            except Exception as e:
                if self._fast_acquisition_thread_keep_running.is_set():
                    self._log.debug(f"Exception in fast acquisition loop: {e}")
        
        self._log.info("Fast acquisition frame grabbing thread stopped")

    def set_pixel_format(self, pixel_format, convert_if_not_native=False):
        if self._is_streaming.is_set():
            was_streaming = True
            self.stop_streaming()
        else:
            was_streaming = False
        self.nodemap = self._camera.GetNodeMap()

        mode_mapping = {
            CameraPixelFormat.MONO8: "Mono8",
            CameraPixelFormat.MONO10: "Mono10",
            CameraPixelFormat.MONO12: "Mono12",
            CameraPixelFormat.MONO14: "Mono14",
            CameraPixelFormat.MONO16: "Mono16",
            CameraPixelFormat.BAYER_RG8: "BayerRG8",
            CameraPixelFormat.BAYER_RG12: "BayerRG12"
        }
        if pixel_format not in mode_mapping:
            raise ValueError(f"Pixel format {pixel_format} is not supported by this camera")
        pixel_format_name = mode_mapping[pixel_format]  
        set_enum_node(self.nodemap.GetNode("PixelFormat"), pixel_format_name)
        self._pixel_format = pixel_format
        self.is_color = "mono" not in pixel_format_name.lower()
        # Handle enforced relationships between pixel format and ADC bit depth? Maybe this is only on certain cameras?

        # node_adc_bit_depth, _ = get_node_ptr(self.nodemap.GetNode("AdcBitDepth"))
        # if PySpin.IsWritable(node_pixel_format) and PySpin.IsWritable(node_adc_bit_depth):
        #     pixel_selection = None
        #     pixel_size_byte = None
        #     adc_bit_depth = None
        #     fallback_pixel_selection = None
        #     conversion_pixel_format = None
        #     if pixel_format == "MONO8":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono8"))
        #         conversion_pixel_format = PySpin.PixelFormat_Mono8
        #         pixel_size_byte = 1
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit10"))
        #     if pixel_format == "MONO10":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono10"))
        #         fallback_pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono10p"))
        #         conversion_pixel_format = PySpin.PixelFormat_Mono8
        #         pixel_size_byte = 1
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit10"))
        #     if pixel_format == "MONO12":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono12"))
        #         fallback_pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono12p"))
        #         conversion_pixel_format = PySpin.PixelFormat_Mono16
        #         pixel_size_byte = 2
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit12"))
        #     if pixel_format == "MONO14":  # MONO14/16 are aliases of each other, since they both
        #         # do ADC at bit depth 14
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono16"))
        #         conversion_pixel_format = PySpin.PixelFormat_Mono16
        #         pixel_size_byte = 2
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit14"))
        #     if pixel_format == "MONO16":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("Mono16"))
        #         conversion_pixel_format = PySpin.PixelFormat_Mono16
        #         pixel_size_byte = 2
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit14"))
        #     if pixel_format == "BAYER_RG8":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("BayerRG8"))
        #         conversion_pixel_format = PySpin.PixelFormat_BayerRG8
        #         pixel_size_byte = 1
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit10"))
        #     if pixel_format == "BAYER_RG12":
        #         pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName("BayerRG12"))
        #         conversion_pixel_format = PySpin.PixelFormat_BayerRG12
        #         pixel_size_byte = 2
        #         adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName("Bit12"))

        #     if pixel_selection is not None and adc_bit_depth is not None:
        #         if PySpin.IsReadable(pixel_selection):
        #             node_pixel_format.SetIntValue(pixel_selection.GetValue())
        #             self.pixel_size_byte = pixel_size_byte
        #             self.pixel_format = pixel_format
        #             self.convert_pixel_format = False
        #             if PySpin.IsReadable(adc_bit_depth):
        #                 node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
        #         elif PySpin.IsReadable(fallback_pixel_selection):
        #             node_pixel_format.SetIntValue(fallback_pixel_selection.GetValue())
        #             self.pixel_size_byte = pixel_size_byte
        #             self.pixel_format = pixel_format
        #             self.conversion_pixel_format = conversion_pixel_format
        #             self.convert_pixel_format = True
        #             if PySpin.IsReadable(adc_bit_depth):
        #                 node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
        #         else:
        #             self.convert_pixel_format = convert_if_not_native
        #             if convert_if_not_native:
        #                 self.conversion_pixel_format = conversion_pixel_format
        #             print("Pixel format not available for this camera")
        #             if PySpin.IsReadable(adc_bit_depth):
        #                 node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
        #                 print("Still able to set ADC bit depth to " + adc_bit_depth.GetSymbolic())

        #     else:
        #         print("Pixel format not implemented for Squid")

        # else:
        #     print("pixel format is not writable")

        if was_streaming:
            self.start_streaming()

        # update the exposure delay and strobe delay
        self.exposure_delay_us = self.exposure_delay_us_8bit * self.pixel_size_byte
        self.strobe_delay_us = self.exposure_delay_us + self.row_period_us * self.pixel_size_byte * (
            self.row_numbers - 1
        )

    def set_continuous_acquisition(self):
        self.nodemap = self._camera.GetNodeMap()
        node_trigger_mode = PySpin.CEnumerationPtr(self.nodemap.GetNode("TriggerMode"))
        node_trigger_mode_off = PySpin.CEnumEntryPtr(node_trigger_mode.GetEntryByName("Off"))
        if not PySpin.IsWritable(node_trigger_mode) or not PySpin.IsReadable(node_trigger_mode_off):
            print("Cannot toggle TriggerMode")
            return
        node_trigger_mode.SetIntValue(node_trigger_mode_off.GetValue())
        self.trigger_mode = TriggerMode.CONTINUOUS
        self.update_camera_exposure_time()

    def set_triggered_acquisition_flir(self, source, activation=None):
        self.nodemap = self._camera.GetNodeMap()
        node_trigger_mode = PySpin.CEnumerationPtr(self.nodemap.GetNode("TriggerMode"))
        node_trigger_mode_on = PySpin.CEnumEntryPtr(node_trigger_mode.GetEntryByName("On"))
        if not PySpin.IsWritable(node_trigger_mode) or not PySpin.IsReadable(node_trigger_mode_on):
            print("Cannot toggle TriggerMode")
            return
        node_trigger_source = PySpin.CEnumerationPtr(self.nodemap.GetNode("TriggerSource"))
        node_trigger_source_option = PySpin.CEnumEntryPtr(node_trigger_source.GetEntryByName(str(source)))

        node_trigger_mode.SetIntValue(node_trigger_mode_on.GetValue())

        if not PySpin.IsWritable(node_trigger_source) or not PySpin.IsReadable(node_trigger_source_option):
            print("Cannot set Trigger source")
            return

        node_trigger_source.SetIntValue(node_trigger_source_option.GetValue())

        if source != "Software" and activation is not None:  # Set activation criteria for hardware trigger
            node_trigger_activation = PySpin.CEnumerationPtr(self.nodemap.GetNode("TriggerActivation"))
            node_trigger_activation_option = PySpin.CEnumEntryPtr(
                node_trigger_activation.GetEntryByName(str(activation))
            )
            if not PySpin.IsWritable(node_trigger_activation) or not PySpin.IsReadable(node_trigger_activation_option):
                print("Cannot set trigger activation mode")
                return
            node_trigger_activation.SetIntValue(node_trigger_activation_option.GetValue())

    def set_software_triggered_acquisition(self):

        self.set_triggered_acquisition_flir(source="Software")

        self.trigger_mode = TriggerMode.SOFTWARE
        self.update_camera_exposure_time()

    def set_hardware_triggered_acquisition(self, source="Line2", activation="RisingEdge"):
        self.set_triggered_acquisition_flir(source=source, activation=activation)
        self.frame_ID_offset_hardware_trigger = None
        self.trigger_mode = TriggerMode.HARDWARE
        self.update_camera_exposure_time()

    def send_trigger(self, illumination_time: Optional[float] = None):
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER and not self._hw_trigger_fn:
            raise CameraError("In HARDWARE_TRIGGER mode, but no hw trigger function given.")

        if not self.get_is_streaming():
            raise CameraError("Camera is not streaming, cannot send trigger.")

        if not self.get_ready_for_trigger():
            raise CameraError(
                f"Requested trigger too early (last trigger was {time.time() - self._last_trigger_timestamp} [s] ago), refusing."
            )

        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            self._hw_trigger_fn(illumination_time)
        elif self.get_acquisition_mode() == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            try:
                self.nodemap = self._camera.GetNodeMap()
                node_trigger = PySpin.CCommandPtr(self.nodemap.GetNode("TriggerSoftware"))
                if not PySpin.IsWritable(node_trigger):
                    raise CameraError("Trigger node not writable")
                node_trigger.Execute()
                self._last_trigger_timestamp = time.time()
                self._trigger_sent.set()
            except Exception as e:
                raise CameraError(f"Failed to send software trigger: {e}")

    def read_frame(self):
        if not self._camera.IsStreaming():
            print("Cannot read frame, camera not streaming")
            return np.zeros((self.Width, self.Height))
        callback_was_enabled = False
        if self.callback_is_enabled:  # need to disable callback to read stream manually
            callback_was_enabled = True
            self.disable_callback()
        raw_image = self._camera.GetNextImage(int(np.round(self._exposure_time_ms*1.1)))
        if raw_image.IsIncomplete():
            print("Image incomplete with image status %d ..." % raw_image.GetImageStatus())
            raw_image.Release()
            return np.zeros((self.Width, self.Height))

        if self.is_color and "mono" not in self.pixel_format.lower():
            if (
                "10" in self.pixel_format
                or "12" in self.pixel_format
                or "14" in self.pixel_format
                or "16" in self.pixel_format
            ):
                rgb_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_RGB16)
            else:
                rgb_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_RGB8)
            numpy_image = rgb_image.GetNDArray()
            if self.pixel_format == "BAYER_RG12":
                numpy_image = numpy_image << 4
        else:
            if self.convert_pixel_format:
                converted_image = self.one_frame_post_processor.Convert(raw_image, self.conversion_pixel_format)
                numpy_image = converted_image.GetNDArray()
                if self.conversion_pixel_format == PySpin.PixelFormat_Mono12:
                    numpy_image = numpy_image << 4
            else:
                try:
                    numpy_image = raw_image.GetNDArray()
                except PySpin.SpinnakerException:
                    print("Encountered problem getting ndarray, falling back to conversion to Mono8")
                    converted_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()
                if self.pixel_format == "MONO12":
                    numpy_image = numpy_image << 4
        # self.current_frame = numpy_image
        raw_image.Release()
        if callback_was_enabled:  # reenable callback if it was disabled
            self.enable_callback()
        return numpy_image

    def set_region_of_interest(self, offset_x=None, offset_y=None, width=None, height=None):

        # stop streaming if streaming is on
        if self._is_streaming.is_set():
            was_streaming = True
            self.stop_streaming()
        else:
            was_streaming = False

        self.nodemap = self._camera.GetNodeMap()
        node_width = PySpin.CIntegerPtr(self.nodemap.GetNode("Width"))
        node_height = PySpin.CIntegerPtr(self.nodemap.GetNode("Height"))
        node_width_max = PySpin.CIntegerPtr(self.nodemap.GetNode("WidthMax"))
        node_height_max = PySpin.CIntegerPtr(self.nodemap.GetNode("HeightMax"))
        node_offset_x = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetX"))
        node_offset_y = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetY"))


        if offset_x is not None:
            # update the camera setting
            if PySpin.IsWritable(node_offset_x):
                node_min = node_offset_x.GetMin()
                node_max = node_offset_x.GetMax()
                node_inc = node_offset_x.GetInc()
                diff = offset_x - node_min
                num_incs = diff // node_inc
                offset_x = node_min + num_incs * node_inc

                self.OffsetX = offset_x
                node_offset_x.SetValue(min(int(offset_x), node_max))
            else:
                print("OffsetX is not implemented or not writable")

        if offset_y is not None:
            # update the camera setting
            if PySpin.IsWritable(node_offset_y):
                node_min = node_offset_y.GetMin()
                node_max = node_offset_y.GetMax()
                node_inc = node_offset_y.GetInc()
                diff = offset_y - node_min
                num_incs = diff // node_inc
                offset_y = node_min + num_incs * node_inc

                self.OffsetY = offset_y
                node_offset_y.SetValue(min(int(offset_y), node_max))
            else:
                print("OffsetY is not implemented or not writable")
        
        if width is not None:
            # update the camera setting
            if PySpin.IsWritable(node_width):
                node_min = node_width.GetMin()
                node_inc = node_width.GetInc()
                diff = width - node_min
                num_incs = diff // node_inc
                width = node_min + num_incs * node_inc
                self.Width = width
                node_width.SetValue(min(max(int(width), 0), node_width_max.GetValue()))
            else:
                print("Width is not implemented or not writable")

        if height is not None:
            # update the camera setting
            if PySpin.IsWritable(node_height):
                node_min = node_height.GetMin()
                node_inc = node_height.GetInc()
                diff = height - node_min
                num_incs = diff // node_inc
                height = node_min + num_incs * node_inc

                self.Height = height
                node_height.SetValue(min(max(int(height), 0), node_height_max.GetValue()))
            else:
                print("Height is not implemented or not writable")

        # restart streaming if it was previously on
        if was_streaming == True:
            self.start_streaming()

    def reset_camera_acquisition_counter(self):
        self.nodemap = self._camera.GetNodeMap()
        node_counter_event_source = PySpin.CEnumerationPtr(self.nodemap.GetNode("CounterEventSource"))
        node_counter_event_source_line2 = PySpin.CEnumEntryPtr(node_counter_event_source.GetEntryByName("Line2"))
        if PySpin.IsWritable(node_counter_event_source) and PySpin.IsReadable(node_counter_event_source_line2):
            node_counter_event_source.SetIntValue(node_counter_event_source_line2.GetValue())
        else:
            print("CounterEventSource is not implemented or not writable, or Line 2 is not an option")

        node_counter_reset = PySpin.CCommandPtr(self.nodemap.GetNode("CounterReset"))

        if PySpin.IsImplemented(node_counter_reset) and PySpin.IsWritable(node_counter_reset):
            node_counter_reset.Execute()
        else:
            print("CounterReset is not implemented")

    def set_line3_to_strobe(self):  # FLIR cams don't have the right Line layout for this
        # self._camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        # self.nodemap = self._camera.GetNodeMap()

        # node_line_selector = PySpin.CEnumerationPtr(self.nodemap.GetNode('LineSelector'))

        # node_line3 = PySpin.CEnumEntryPtr(node_line_selector.GetEntryByName('Line3'))

        # self._camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        # self._camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        # self._camera.LineSource.set(gx.GxLineSourceEntry.STROBE)
        pass

    def set_line3_to_exposure_active(self):  # BlackFly cam has no output on Line 3
        # self._camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        # self._camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        # self._camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        # self._camera.LineSource.set(gx.GxLineSourceEntry.EXPOSURE_ACTIVE)
        pass

    # AbstractCamera method implementations - minimal templates
    def get_exposure_time(self) -> float:
        """Returns the current exposure time in milliseconds."""
        self.nodemap = self._camera.GetNodeMap()
        _, exposure_time = get_float_node(self.nodemap.GetNode("ExposureTime"))
        return exposure_time / 1000.0

    def get_exposure_limits(self) -> Tuple[float, float]:
        """Return the valid range of exposure times in inclusive milliseconds."""
        return 0.006, 30000 # from FLIR BFS-U3-23S3M manual

    def get_strobe_time(self) -> float:
        """Given the current exposure time we are using, what is the strobe time such that
        get_strobe_time() + get_exposure_time() == total frame time.  In milliseconds."""
        # TODO: Implement
        raise NotImplementedError("get_strobe_time not yet implemented")

    def set_frame_format(self, frame_format: CameraFrameFormat):
        """If this camera supports the given frame format, set it and make sure that
        all subsequent frames are in this format.

        If not, throw a ValueError.
        """
        # TODO: Implement
        if frame_format != CameraFrameFormat.RAW:
            raise ValueError("Only RAW frame format is supported by Retiga Electro.")

    def get_frame_format(self) -> CameraFrameFormat:
        """Returns the current frame format."""
        # TODO: Implement
        return CameraFrameFormat.RAW # TODO: Implement

    def get_pixel_format(self) -> CameraPixelFormat:
        """Returns the current pixel format."""
        mode_mapping = {
            "Mono8": CameraPixelFormat.MONO8,
            "Mono10": CameraPixelFormat.MONO10,
            "Mono12": CameraPixelFormat.MONO12,
            "Mono14": CameraPixelFormat.MONO14,
            "Mono16": CameraPixelFormat.MONO16,
            "BayerRG8": CameraPixelFormat.BAYER_RG8
        }
        _, pixel_format_name = get_enumeration_node_and_current_entry(self.nodemap.GetNode("PixelFormat"))
        return mode_mapping[pixel_format_name]

    def get_available_pixel_formats(self) -> Sequence[CameraPixelFormat]:
        """Returns the list of pixel formats supported by the camera."""
        # TODO: Implement
        raise NotImplementedError("get_available_pixel_formats not yet implemented")

    def set_binning(self, binning_factor_x: int, binning_factor_y: int):
        """Set the binning factor of the camera. Usually we set hardware binning here, so calling
        this may change buffer size, readout speed, etc. Update these settings as needed.
        """
        # TODO: Implement
        raise NotImplementedError("set_binning not yet implemented")

    def get_binning(self) -> Tuple[int, int]:
        self.nodemap = self._camera.GetNodeMap()
        _, binning_x = get_integer_node(self.nodemap.GetNode("BinningHorizontal"))
        _, binning_y = get_integer_node(self.nodemap.GetNode("BinningVertical"))
        return (binning_x, binning_y)

    def get_binning_options(self) -> Sequence[Tuple[int, int]]:
        """Return the list of binning options supported by the camera."""
        return [(1, 1), (2, 2), (3,3), (4, 4)] # Manual entries for BFS-U3-23S3M

    def get_resolution(self) -> Tuple[int, int]:
        """Returns the maximum resolution of the camera under the current binning setting."""
        self.nodemap = self._camera.GetNodeMap()
        _, binning_x = get_integer_node(self.nodemap.GetNode("BinningHorizontal"))
        _, binning_y = get_integer_node(self.nodemap.GetNode("BinningVertical"))
        _, max_width = get_integer_node(self.nodemap.GetNode("WidthMax"))
        _, max_height = get_integer_node(self.nodemap.GetNode("HeightMax"))
        return (int(max_width/binning_x), int(max_height/binning_y))

    def get_pixel_size_unbinned_um(self) -> float:
        """Returns the pixel size without binning in microns."""
        return CAMERA_PIXEL_SIZE_UM[self._sensor_type]

    def get_pixel_size_binned_um(self) -> float:
        """Returns the pixel size after binning in microns."""
        return CAMERA_PIXEL_SIZE_UM[self._sensor_type]*self.get_binning()[0]

    def get_analog_gain(self) -> float:
        """Returns gain in the same units as set_analog_gain."""
        # TODO: Implement
        raise NotImplementedError("get_analog_gain not yet implemented")

    def get_gain_range(self) -> CameraGainRange:
        """Returns the gain range, and minimum gain step, for this camera."""
        # TODO: Implement
        raise NotImplementedError("get_gain_range not yet implemented")

    def set_readout_mode(self, readout_mode: CameraReadoutMode):
        """
        Set the readout mode of the camera using GenICam SensorShutterMode property.
        
        Args:
            readout_mode: The desired readout mode (GLOBAL, ROLLING, or ROLLING_WITH_GLOBAL_RESET)
            
        Raises:
            ValueError: If the camera does not support the requested readout mode
            CameraError: If the camera property is not available or cannot be set
        """
        if not hasattr(self, '_camera') or self._camera is None:
            raise CameraError("Camera not initialized")
        
        self.nodemap = self._camera.GetNodeMap()
        
        # Try to get the SensorShutterMode node
        node_sensor_shutter_mode = self.nodemap.GetNode("SensorShutterMode")
        ptr, principal_interface_type = get_node_ptr(node_sensor_shutter_mode)
        if not PySpin.IsAvailable(node_sensor_shutter_mode):
            raise NotImplementedError("SensorShutterMode property not available on this camera")
        
        if not PySpin.IsWritable(node_sensor_shutter_mode):
            raise CameraError("SensorShutterMode property is not writable")
        
        # Map our enum to GenICam string values
        mode_mapping = {
            CameraReadoutMode.GLOBAL: "Global",
            CameraReadoutMode.ROLLING: "Rolling",
            CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET: "GlobalReset",
        }
        self._readout_mode = readout_mode
        genicam_mode_name = mode_mapping.get(readout_mode)
        if genicam_mode_name is None:
            raise ValueError(f"Unknown readout mode: {readout_mode}")
        
        # Check if it's an enumeration node
        if principal_interface_type == PySpin.intfIEnumeration:
            set_enum_node(node_sensor_shutter_mode, genicam_mode_name)
        else:
            # If it's a string node, try to set it as a string
            try:
                ptr.SetValue(genicam_mode_name)
            except:
                raise CameraError(f"Could not set SensorShutterMode to {genicam_mode_name}")
        self._log.info(f"Set readout mode to {readout_mode.value}")

    def get_readout_mode(self) -> CameraReadoutMode:
        """
        Get the current readout mode of the camera from GenICam SensorShutterMode property.
        
        Returns:
            The current readout mode
            
        Raises:
            NotImplementedError: If the camera does not support readout mode querying
            CameraError: If the camera property is not available
        """
        if not hasattr(self, '_camera') or self._camera is None:
            raise CameraError("Camera not initialized")
        
        self.nodemap = self._camera.GetNodeMap()
        
        # Try to get the SensorShutterMode node
        node_sensor_shutter_mode = self.nodemap.GetNode("SensorShutterMode")
        _, current_mode_str = get_enumeration_node_and_current_entry(node_sensor_shutter_mode)
        # Map GenICam string values to our enum
        mode_mapping = {
            "Global": CameraReadoutMode.GLOBAL,
            "Rolling": CameraReadoutMode.ROLLING,
            "GlobalReset": CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET,
            "RollingWithGlobalReset": CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET,
            "Rolling with Global Reset": CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET,
        }
        
        readout_mode = mode_mapping.get(current_mode_str)
        if readout_mode is None:
            # Default to GLOBAL if we can't map it
            self._log.warning(f"Unknown SensorShutterMode value: {current_mode_str}, defaulting to GLOBAL")
            return CameraReadoutMode.GLOBAL
        if readout_mode != self._readout_mode:
            self._log.warning(f"Readout mode mismatch (Hardware/software): {readout_mode} != {self._readout_mode}. Resetting")
            self._readout_mode = readout_mode        
        return readout_mode

    def get_available_readout_modes(self) -> Sequence[CameraReadoutMode]:
        """
        Get the list of readout modes supported by this camera by querying GenICam SensorShutterMode entries.
        
        Returns:
            A sequence of supported readout modes. May be empty if the camera does not support
            readout mode selection.
        """
        if not hasattr(self, '_camera') or self._camera is None:
            return []
        
        self.nodemap = self._camera.GetNodeMap()
        
        # Try to get the SensorShutterMode node
        node_sensor_shutter_mode = self.nodemap.GetNode("SensorShutterMode")
        if not PySpin.IsAvailable(node_sensor_shutter_mode):
            return []
        
        available_modes = []
        
        if PySpin.IsEnumeration(node_sensor_shutter_mode):
            node_enum = PySpin.CEnumerationPtr(node_sensor_shutter_mode)
            entries = node_enum.GetEntries()
            
            # Map GenICam string values to our enum
            mode_mapping = {
                "Global": CameraReadoutMode.GLOBAL,
                "Rolling": CameraReadoutMode.ROLLING,
                "RollingWithGlobalReset": CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET,
                "Rolling with Global Reset": CameraReadoutMode.ROLLING_WITH_GLOBAL_RESET,
            }
            
            for entry in entries:
                entry_ptr = PySpin.CEnumEntryPtr(entry)
                if PySpin.IsReadable(entry_ptr):
                    symbolic = entry_ptr.GetSymbolic()
                    if symbolic in mode_mapping:
                        mode = mode_mapping[symbolic]
                        if mode not in available_modes:
                            available_modes.append(mode)
        
        # If no modes found, default to GLOBAL (most cameras support at least this)
        if not available_modes:
            available_modes = [CameraReadoutMode.GLOBAL]
        
        return available_modes

    def get_is_streaming(self):
        """Returns True if the camera is currently streaming, False otherwise."""
        return self._is_streaming.is_set()

    def read_camera_frame(self) -> Optional[CameraFrame]:
        """This calls read_frame, but also fills in all the information such that you get a CameraFrame.  The
        frame in the CameraFrame will have had _process_raw_frame called on it already.

        Might return None if getting a frame timed out, or another error occurred.
        """
        if not self.get_is_streaming():
            self._log.error("Cannot read camera frame when not streaming.")
            return None

        if not self._read_thread_running.is_set():
            self._log.error("Fatal camera error: read thread not running!")
            return None

        starting_id = self.get_frame_id()
        timeout_s = (1.04 * self.get_total_frame_time() + 1000) / 1000.0
        timeout_time_s = time.time() + timeout_s

        while self.get_frame_id() == starting_id:
            if time.time() > timeout_time_s:
                self._log.warning(
                    f"Timed out after waiting {timeout_s=}[s] for frame ({starting_id=}), total_frame_time={self.get_total_frame_time()}."
                )
                return None
            time.sleep(0.001)

        with self._frame_lock:
            return self._current_frame

    def get_frame_id(self) -> int:
        """Returns the frame id of the current frame.  This should increase by 1 with every frame received
        from the camera
        """
        with self._frame_lock:
            return self._current_frame.frame_id if self._current_frame else -1

    def get_white_balance_gains(self) -> Tuple[float, float, float]:
        """Returns the (R, G, B) white balance gains"""
        # TODO: Implement
        raise NotImplementedError("get_white_balance_gains not yet implemented")

    def set_white_balance_gains(self, red_gain: float, green_gain: float, blue_gain: float):
        """Set the (R, G, B) white balance gains."""
        # TODO: Implement
        raise NotImplementedError("set_white_balance_gains not yet implemented")

    def set_auto_white_balance_gains(self, on: bool):
        """Turn auto white balance on or off."""
        # TODO: Implement
        raise NotImplementedError("set_auto_white_balance_gains not yet implemented")

    def set_black_level(self, black_level: float):
        """Sets the black level of captured images."""
        # TODO: Implement
        raise NotImplementedError("set_black_level not yet implemented")

    def get_black_level(self) -> float:
        """Gets the black level set on the camera."""
        # TODO: Implement
        raise NotImplementedError("get_black_level not yet implemented")

    def _set_acquisition_mode_imp(self, acquisition_mode: CameraAcquisitionMode):
        """Your subclass must implement this such that it switches the camera to this acquisition mode.  The top level
        set_acquisition_mode handles storing the self._hw_trigger_fn for you so you are guaranteed to have a valid
        callable self._hw_trigger_fn if in hardware trigger mode.

        If things like setting a remote strobe, or other settings, are needed when you change the mode you must
        handle that here.
        """
        self.nodemap = self._camera.GetNodeMap()
        # Get necessary acquisition mode and trigger nodes (need to make sure they match)
        acq_node = self.nodemap.GetNode("AcquisitionMode")
        trigger_on_node = self.nodemap.GetNode("TriggerMode")
        trigger_selector_node = self.nodemap.GetNode("TriggerSelector")
        trigger_source_node = self.nodemap.GetNode("TriggerSource")
        trigger_activation_node = self.nodemap.GetNode("TriggerActivation")
        trigger_overlap_node = self.nodemap.GetNode("TriggerOverlap")

        # try:
        if acquisition_mode == CameraAcquisitionMode.CONTINUOUS:
            set_enum_node(acq_node, "Continuous")
            set_enum_node(trigger_on_node, "Off")
            set_enum_node(trigger_source_node, "Software")
            set_enum_node(trigger_overlap_node, "Off")
            self.trigger_mode = TriggerMode.CONTINUOUS
        elif acquisition_mode == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            set_enum_node(acq_node, "Continuous")
            set_enum_node(trigger_on_node, "On")
            set_enum_node(trigger_selector_node, "FrameStart")
            set_enum_node(trigger_source_node, "Software")
            set_enum_node(trigger_activation_node, "RisingEdge")
            set_enum_node(trigger_overlap_node, "ReadOut")
            self.trigger_mode = TriggerMode.SOFTWARE
        elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
            set_enum_node(acq_node, "Continuous")
            set_enum_node(trigger_on_node, "On")
            set_enum_node(trigger_selector_node, "FrameStart")
            set_enum_node(trigger_source_node, "Line3")
            set_enum_node(trigger_activation_node, "RisingEdge")
            set_enum_node(trigger_overlap_node, "ReadOut")
            self.trigger_mode = TriggerMode.HARDWARE
        elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST:
            set_enum_node(acq_node, "Continuous")
            set_enum_node(trigger_on_node, "On")
            set_enum_node(trigger_selector_node, "AcquisitionStart")
            set_enum_node(trigger_source_node, "Line3")
            set_enum_node(trigger_activation_node, "RisingEdge")
            set_enum_node(trigger_overlap_node, "Off")
            # Not sure if this should be HARDWARE or CONTINUOUS currently
            self.trigger_mode = TriggerMode.HARDWARE
        else:
            raise ValueError(f"Unsupported acquisition mode: {acquisition_mode}")

        self._acquisition_mode = acquisition_mode
        self._set_exposure_time_imp(self._exposure_time_ms)

        # except Exception as e:
        #     raise CameraError(f"Failed to set acquisition mode: {e}")

    def get_acquisition_mode(self) -> CameraAcquisitionMode:
        """
        Get the current acquisition mode of the camera from acquisition mode and trigger mode nodes.
        
        Returns:
            The current acquisition mode
            
        Raises:
            CameraError: If the camera property is not available or cannot be set
        """
        acq_node = self.nodemap.GetNode("AcquisitionMode")
        _, acq_mode_str = get_enumeration_node_and_current_entry(acq_node)
        trigger_on_node = self.nodemap.GetNode("TriggerMode")
        _, trigger_on_str = get_enumeration_node_and_current_entry(trigger_on_node)
        trigger_selector_node = self.nodemap.GetNode("TriggerSelector")
        _, trigger_selector_str = get_enumeration_node_and_current_entry(trigger_selector_node)
        trigger_source_node = self.nodemap.GetNode("TriggerSource")
        _, trigger_source_str = get_enumeration_node_and_current_entry(trigger_source_node)

        if acq_mode_str == "Continuous" and trigger_on_str == "Off":
            return CameraAcquisitionMode.CONTINUOUS
        elif trigger_on_str == "On":
            if trigger_selector_str == "FrameStart" and trigger_source_str == "Software":
                return CameraAcquisitionMode.SOFTWARE_TRIGGER
            elif trigger_selector_str == "FrameStart" and trigger_source_str == "Line3":
                return CameraAcquisitionMode.HARDWARE_TRIGGER
            elif trigger_selector_str == "AcquisitionStart" and trigger_source_str == "Line3":
                return CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST
            else:
                raise ValueError(f"Unknown acquisition mode: Acquisition mode: {acq_mode_str}, Trigger on: {trigger_on_str}, Trigger selector: {trigger_selector_str}, Trigger source: {trigger_source_str}")
        else:
            raise ValueError(f"Unknown acquisition mode: Acquisition mode: {acq_mode_str}, Trigger on: {trigger_on_str}, Trigger selector: {trigger_selector_str}, Trigger source: {trigger_source_str}")

    def get_ready_for_trigger(self) -> bool:
        """Returns true if the camera is ready for another trigger, false otherwise.  Calling
        send_trigger when this is False will result in an exception from send_trigger.
        """
        if time.time() - self._last_trigger_timestamp > 1.5 * ((self.get_total_frame_time() + 4) / 1000.0):
            self._trigger_sent.clear()
        return not self._trigger_sent.is_set()

    def get_region_of_interest(self) -> Tuple[int, int, int, int]:
        """Returns the region of interest as a tuple of (x corner, y corner, width, height)"""
        self.nodemap = self._camera.GetNodeMap()
        node_width = PySpin.CIntegerPtr(self.nodemap.GetNode("Width"))
        node_height = PySpin.CIntegerPtr(self.nodemap.GetNode("Height"))
        node_offset_x = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetX"))
        node_offset_y = PySpin.CIntegerPtr(self.nodemap.GetNode("OffsetY"))
        return node_offset_x.GetValue(), node_offset_y.GetValue(), node_width.GetValue(), node_height.GetValue()

    def set_temperature(self, temperature_deg_c: Optional[float]):
        """Set the desired temperature of the camera in degrees C.  If None is given as input, use
        a sane default for the camera.
        """
        # TODO: Implement
        raise NotImplementedError("set_temperature not yet implemented")

    def get_temperature(self) -> float:
        """Get the current temperature of the camera in deg C."""
        # TODO: Implement
        raise NotImplementedError("get_temperature not yet implemented")

    def set_temperature_reading_callback(self, callback: Callable):
        """Set the callback to be called when the temperature reading changes."""
        # TODO: Implement
        raise NotImplementedError("set_temperature_reading_callback not yet implemented")

    def __del__(self):
        try:
            self.stop_streaming()
            self._camera.DeInit()
            del self.camera
        except AttributeError:
            pass
        self.camera_list.Clear()
        self.py_spin_system.ReleaseInstance()
