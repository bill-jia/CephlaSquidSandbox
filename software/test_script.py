import argparse
import cv2
import time
import numpy as np
import PySpin
from control._def import *
import matplotlib.pyplot as plt


system = PySpin.System.GetInstance()
cam_list = system.GetCameras()
cam = cam_list.GetByIndex(0)
cam.Init()
cam.BeginAcquisition()
result_image = cam.GetNextImage()
data = result_image.GetNDArray()
plt.imshow(data)
plt.show()