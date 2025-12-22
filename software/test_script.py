import argparse
import cv2
import time
import numpy as np
import PySpin
from control._def import *
import matplotlib.pyplot as plt

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

system = PySpin.System.GetInstance()
cam_list = system.GetCameras()
cam = cam_list.GetByIndex(0)
cam.Init()
nodemap = cam.GetNodeMap()
acquisition_mode_node = nodemap.GetNode("AcquisitionMode")
print(get_enumeration_node_and_current_entry(acquisition_mode_node))
cam.BeginAcquisition()
result_image = cam.GetNextImage()
data = result_image.GetNDArray()
plt.imshow(data)
plt.show()
set_enum_node(acquisition_mode_node, "")