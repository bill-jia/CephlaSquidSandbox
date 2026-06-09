# set QT_API environment variable
import os

os.environ["QT_API"] = "pyqt5"
import qtpy

# qt libraries
from qtpy.QtCore import *
from qtpy.QtWidgets import *
from qtpy.QtGui import *

import control.utils as utils
from control._def import *

import time
import numpy as np
import cv2


class DisplacementMeasurementController(QObject):

    signal_readings = Signal(list)
    signal_plots = Signal(np.ndarray, np.ndarray)

    def __init__(self, x_offset=0, y_offset=0, x_scaling=1, y_scaling=1, N_average=1, N=10000):

        QObject.__init__(self)
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.x_scaling = x_scaling
        self.y_scaling = y_scaling
        self.N_average = N_average
        self.N = N  # length of array to emit
        self.t_array = np.array([])
        self.x_array = np.array([])
        self.y_array = np.array([])

    def update_measurement(self, image):

        t = time.time()

        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

        # Intensity-weighted centroid via 1D projections. This is mathematically
        # equivalent to the meshgrid+multiply form (sum(x*I)/sum(I)) but does
        # O(h*w) work and one h*w allocation instead of three. At full sensor
        # FOV (3088x2064) the previous form took 100s of ms per frame and
        # blocked the Qt event loop; this stays in single-digit ms.
        I = image.astype(np.float32)
        Imax = float(I.max())
        if Imax <= 0:
            return  # blank frame, nothing to measure
        Imin = float(I.min())
        I -= Imin
        # Threshold at 20% of peak (post-background-subtraction the original
        # divided by amax; here we threshold against the equivalent constant).
        I[I < 0.2 * (Imax - Imin)] = 0
        total = float(I.sum())
        if total <= 0:
            return

        h, w = I.shape
        col_sum = I.sum(axis=0)  # length w
        row_sum = I.sum(axis=1)  # length h
        x = float((col_sum * np.arange(w, dtype=np.float32)).sum() / total)
        y = float((row_sum * np.arange(h, dtype=np.float32)).sum() / total)

        x = (x - self.x_offset) * self.x_scaling
        y = (y - self.y_offset) * self.y_scaling

        # Trim on append so the underlying arrays don't grow without bound —
        # the original kept appending forever and only sliced [-N:] at emit
        # time, so memory and append cost climbed every frame.
        self.t_array = np.append(self.t_array, t)[-self.N :]
        self.x_array = np.append(self.x_array, x)[-self.N :]
        self.y_array = np.append(self.y_array, y)[-self.N :]

        self.signal_plots.emit(self.t_array, np.vstack((self.x_array, self.y_array)))
        self.signal_readings.emit([float(self.x_array[-self.N_average :].mean()), float(self.y_array[-self.N_average :].mean())])

    def update_settings(self, x_offset, y_offset, x_scaling, y_scaling, N_average, N):
        self.N = N
        self.N_average = N_average
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.x_scaling = x_scaling
        self.y_scaling = y_scaling
