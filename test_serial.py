import serial
from serial.tools import list_ports
import time

SN = "3065377C3433"
for d in list_ports.comports():
    print(d.device, d.serial_number)
    if d.serial_number == SN:
        print("Device Found")
        port = d.device

serial_connection = serial.Serial(port, baudrate=9600, timeout=0.5)
serial_connection.close()
serial_connection.open()

command = "LAMS\r\n"
serial_connection.write(command.encode())
time.sleep(0.1)  # Wait for the command to be sent/executed

for i in range(4):
    response = serial_connection.read().decode().strip()
    print(response)
serial_connection.close()