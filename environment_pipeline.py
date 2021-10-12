import numpy as np
from preprocessing import find_clusters, TrackClusters
import cv2
import datetime
import vlc
import time
import serial
from settings import *
import matplotlib.pyplot as plt
import binascii
from collections import deque
import settings
import pyvisa as visa


class VideoStream:

    def __init__(self, url=STREAM_URL):

        # Define VLC instance
        instance = vlc.Instance()

        # Define VLC player
        self.player = instance.media_player_new()

        # Define VLC media
        self.media = instance.media_new(url)

        # Set player media
        self.player.set_media(self.media)
        self.player.play()
        time.sleep(2)

    def snap(self, f_name):

        # Snap an image of size (IMG_SIZE, IMG_SIZE)
        self.player.video_take_snapshot(0, f_name, IMG_SIZE, IMG_SIZE)


class Actuator:

    def __init__(self):

        # Initiate contact with arduino
        self.arduino = serial.Serial(port=SERIAL_PORT_ARDUINO, baudrate=BAUDRATE)
        print(f"Arduino: {self.arduino.readline().decode()}")
        time.sleep(1)  # give serial communication time to establish

        # Initiate status of 4 piëzo transducers
        self.arduino.write(b"9")  # Turn all outputs to LOW

    def move(self, action: int):

        self.arduino.write(b"9")  # Turn old piezo off
        self.arduino.write(f"{action}".encode())  # Turn new piezo on

    def close(self):

        self.arduino.write(b"9")


class Observer:

    def __init__(self, port=SERIAL_PORT_LEICA):

        self.observer = serial.Serial(port=port, baudrate=9600, timeout=2)
        print(f"Opened port {port}: {self.observer.isOpen()}")

    def reset(self):
        self.observer.write(bytearray([255, 82]))  # Reset device
        time.sleep(2)  # Give the device time to reset before issuing new commands
        self.pos = (0, 0)

    def close(self):
        self.observer.close()
        print(f"Closed serial connection")

    def get_status(self, motor: int):  # TODO --> use check_status to make sure the motor does not get command while still busy
        assert motor in [0, 1, 2]  # Check if device number is valid
        self.observer.write(bytearray([motor, 63, 58]))  # Ask for device status
        received = self.observer.read(1)  # Read response (1 byte)
        if received:
            print(f"Received: {received.decode()}")  # Print received message byte

    def write_target_pos(self, motor: int, target_pos: int):

        # Translate coordinate to a 3-byte message
        msg = self.coord_to_msg(int(target_pos))
        # print(f"Writing target pos bytes: {msg} to motor {motor}")

        # [device number, command, 3, message, stop signal]
        self.observer.write(bytearray([motor, 84, 3, msg[0], msg[1], msg[2], 58]))
        time.sleep(SLEEP_TIME)

    def get_motor_pos(self, motor):

        # Ask for position
        self.observer.write(bytearray([motor, 97, 58]))

        # Initialise received variable
        received = b''

        # For timeout functionality
        t0 = time.time()

        # Check for received message until timeout
        while not received:
            received = self.observer.read(3)  # Read response (3 bytes)
            if received == b'\x00\x00\x00':
                return 0
            if time.time() - t0 >= SLEEP_TIME:  # Check if it takes longer than a second
                print(f"No bytes received: {received}")
                return 0

        # Return translated message
        translated = self.msg_to_coord(received)
        # print(f"Motor pos: {translated}")  # Print received message as coordinate
        time.sleep(SLEEP_TIME)

        return translated

    def get_target_pos(self, motor: int):

        # Ask for position
        self.observer.write(bytearray([motor, 116, 58]))

        # Initialise received variable
        received = b''

        # For timeout functionality
        t0 = time.time()

        # Check for received message until timeout
        while not received:
            received = self.observer.read(3)  # Read response (3 bytes)
            if received == b'\x00\x00\x00':
                return 0
            if time.time() - t0 >= SLEEP_TIME:  # Check if it takes longer than a second
                print(f"No bytes received: {received}")
                return 0

        # Return translated message
        translated = self.msg_to_coord(received)
        # print(f"Target pos: {translated}")  # Print received message as coordinate
        time.sleep(SLEEP_TIME)

        return translated

    def move_to_target(self, motor: int, target_pos: int):  # TODO --> Coordinate boundaries so we don't overshoot the table and get the motor stuck, as we need to reset then

        """
        100k steps is 1 cm in real life
        """

        # Write target position
        self.write_target_pos(motor=motor, target_pos=target_pos)

        # Move motor to target coordinate
        self.observer.write(bytearray([motor, 71, 58]))

        # Give motor time to move
        time.sleep(SLEEP_TIME)

    def coord_to_msg(self, coord: int):

        # Convert from two's complement
        if np.sign(coord) == -1:
            coord += 16777216

        # Convert to hexadecimal and pad coordinate with zeros of necessary (len should be 6 if data length is 3)
        hexa = hex(coord).split("x")[-1].zfill(6)

        # Get the four digit binary code for each hex value
        four_digit_binaries = [bin(int(n, 16))[2:].zfill(4) for n in hexa]

        # Convert to 3-byte message
        eight_digit_binaries = [f"{four_digit_binaries[n] + four_digit_binaries[n + 1]}".encode() for n in range(0, 6, 2)][::-1]

        return [int(m, 2) for m in eight_digit_binaries]

    def msg_to_coord(self, msg: str):

        # Read LSB first
        msg = msg[::-1]

        # Convert incoming hex to readable hex
        hexa = binascii.hexlify(bytearray(msg))

        # Convert hex to decimal
        coord = int(hexa, 16)

        # Convert to negative coordinates if applicable
        if bin(coord).zfill(24)[2] == '1':
            coord -= 16777216
        return int(coord)

    def pixels_to_increment(self, pixels: np.array):
        return np.array([pixels[0]*PIXEL_MULTIPLIER_LEFT_RIGHT, pixels[1]*PIXEL_MULTIPLIER_UP_DOWN])

    def move_increment(self, offset_pixels: np.array):

        # Get increments and add to current position
        self.pos += self.pixels_to_increment(pixels=offset_pixels)

        # Move motors x and y
        print("Moving...")
        self.move_to_target(motor=1, target_pos=self.pos[0])  # Move left/right
        self.move_to_target(motor=2, target_pos=self.pos[1])  # Move up/down
        time.sleep(3)  # TODO --> check optimal sleep time


class FunctionGenerator:

    def __init__(self, instrument_descriptor=None):
        rm = visa.ResourceManager()
        if not instrument_descriptor:
            instrument_descriptor = rm.list_resources()[0]
        self.AFG3000 = rm.open_resource(instrument_descriptor)
        self.AFG3000.write('*RST')  # reset AFG

    def set_vpp(self, vpp: float):
        self.AFG3000.write(f'source1:voltage:amplitude {vpp}')  # Set vpp

    def set_frequency(self, frequency: float):
        self.AFG3000.write(f'source1:Frequency {frequency}')  # Set frequency


class SwarmEnvTrackBiggestCluster:

    def __init__(self,
                 source=VideoStream(),
                 actuator=Actuator(),
                 observer=Observer(),
                 function_generator=FunctionGenerator(),
                 target_points=TARGET_POINTS):

        # Initialize devices
        self.source = source  # Camera
        self.actuator = actuator  # Piezo's
        self.observer = observer  # Leica xy-platform
        self.function_generator = function_generator  # Function generator

        # Keep track of target point (idx in target_points)
        self.target_points = target_points
        self.target_idx = 0

    def reset(self):

        # Set env steps to 0
        self.step = 0

        # Initialize tracking algorithm
        self.tracker = TrackClusters()

        # Define file name
        filename = SAVE_DIR + f"{str(round(time.time(), 3))}" \
                              f"-{0}{0}" \
                              f"-{self.target_points[self.target_idx][0]}{self.target_points[self.target_idx][1]}" \
                              f"-reset.png"

        # Snap a frame from the video stream and save
        self.source.snap(f_name=filename)

        # Get the centroid of (biggest) swarm
        self.state = self.tracker.reset(img=cv2.imread(filename, cv2.IMREAD_GRAYSCALE))

        # Exception handling
        if not self.state:
            self.state = (0, 0)

        self.observer.reset()  # TODO --> check if necessary

        # Return centroids of n amount of swarms
        return self.state

    def env_step(self, action: int, vpp: float):

        # Give action to piezos after setting correct vpp
        self.function_generator.set_vpp(vpp=vpp)
        self.actuator.move(action)

        # Define file name
        filename = SAVE_DIR + f"{str(round(time.time(), 3))}" \
                              f"-{self.state[0]}{self.state[1]}" \
                              f"-{self.target_points[self.target_idx][0]}{self.target_points[self.target_idx][1]}" \
                              f"-{action}.png"

        # Snap a frame from the video stream
        self.source.snap(f_name=filename)

        # Get the centroid of biggest swarm
        self.state = self.tracker.update(img=cv2.imread(filename, cv2.IMREAD_GRAYSCALE),  # Read image
                                         target=self.target_points[self.target_idx],  # For verbose purposes
                                         verbose=True)

        # Exception handling
        if not self.state:
            self.state = (0, 0)

        # Calculate offset and move microscope to next point if offset goes out of bounds
        offset = np.linalg.norm((- (self.state[0] - self.target_points[self.target_idx][0]), (self.state[1] - self.target_points[self.target_idx][1])))
        if offset < OFFSET_BOUNDS:
            self.actuator.close()  # Stop piezos
            old_target = self.target_points[self.target_idx]
            self.target_idx += 1 % len(self.target_points)
            new_target = self.target_points[self.target_idx]
            offset_pixels = ( - (new_target[0] - old_target[0]), (new_target[1] - old_target[1]) )
            self.observer.move_increment(offset_pixels=np.array(offset_pixels))

            # Reset tracker
            self.tracker.bbox = np.array(self.tracker.bbox) + np.array(offset_pixels)  # TODO --> Test if this works

        # Add step
        self.step += 1



        # Return centroids of n amount of swarms
        return self.state

    def close(self):
        self.actuator.close()
        self.observer.close()
        self.source.player.stop()

