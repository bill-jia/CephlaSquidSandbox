import configparser
import gc
import os
import json
import yaml
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass
from types import SimpleNamespace

import psutil

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from control.core.memory_profiler import MemoryMonitor
    from control.microscope import Microscope

import squid.config
import squid.logging
from control.core.config import ConfigRepository
from control.core.core import TrackingController, LiveController
from control.core.fast_acquisition_controller import FastAcquisitionController
from control.core.multi_point_controller import MultiPointController
from control.core.downsampled_views import format_well_id
from control.core.geometry_utils import get_effective_well_size, calculate_well_coverage
from control.microcontroller import Microcontroller
from control.piezo import PiezoStage
import control.utils as utils
import control._def  # Import module for runtime access to MCP-modifiable settings
from squid.abc import AbstractStage, AbstractCamera, AbstractFilterWheelController, CameraAcquisitionMode
from squid.stage.utils import move_to_loading_position, move_to_scanning_position, move_z_axis_to_safety_position
from squid.config import CameraPixelFormat

# set QT_API environment variable
os.environ["QT_API"] = "pyqt5"

# qt libraries
import qtpy
from qtpy.QtCore import *
from qtpy.QtWidgets import *
from qtpy.QtGui import *

import pyqtgraph as pg
import pandas as pd
import napari
from napari.utils.colormaps import Colormap, AVAILABLE_COLORMAPS
import re
import cv2
import math
import locale
import time
from datetime import datetime
import itertools
import numpy as np
from scipy.spatial import Delaunay
import shutil
from control._def import *
from PIL import Image, ImageDraw, ImageFont
