from pyvcam import pvc
from pyvcam.camera import Camera as PVCam
pvc.init_pvcam()
cameras = list(PVCam.detect_camera())
cam = cameras[0]
cam.open()
print(f"Camera opened: {cam.name}")
print(f"Sensor shape: {cam.shape(0)}")
print(f"Camera exposure output mode: {cam.exp_out_modes}")
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 0
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 1
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 2
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 3
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 5
print(f"Camera exp mode: {cam.exp_mode}")

cam.set_roi(176, 0, 1024, 1024)
cam.exp_mode = 0
print(f"Camera exp mode: {cam.exp_mode}")
cam.exp_mode = 0
print(f"Camera exp mode: {cam.exp_mode}")
cam.close()

