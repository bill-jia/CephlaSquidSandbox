from pyvcam import pvc
from pyvcam.camera import Camera

pvc.init_pvcam()
cam = next(Camera.detect_camera())
cam.open()
print(cam.exp_modes)
print(cam.exp_out_modes)
print(cam.exp_resolutions)
print(cam.port_speed_gain_table)
cam.metadata_enabled = True
cam.exp_res = 0
cam.start_live(exp_time=50)
frame, _, _ = cam.poll_frame()
print(frame["meta_data"])
print(frame["meta_data"]["frame_header"]["timestampEofPs"]-frame["meta_data"]["frame_header"]["timestampBofPs"])
cam.close()